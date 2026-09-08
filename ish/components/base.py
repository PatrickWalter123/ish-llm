"""Project-owned JSON definitions and directories, independent of execution."""

import json
import re
from pathlib import Path
from typing import Any, Optional, Protocol

from ish.compat import is_junction
from ish.core.models import Project, ProjectConfig, new_id
from ish.services.storage import atomic_json, remove_named_tree, sync_directory


def validate_name(value: str) -> str:
    """A portable single path segment, including Windows device-name checks."""
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
            or value.upper() in {"CON", "PRN", "AUX", "NUL",
                                *(f"COM{i}" for i in range(10)),
                                *(f"LPT{i}" for i in range(10))}):
        raise ValueError("Invalid component directory or record ID")
    return value


class ProjectComponent(Protocol):
    name: str
    directory: str

    def initialize(self, project: Project) -> None: ...
    def configure(self, project: Project, configuration: dict) -> None: ...
    def configuration(self, project: Project) -> dict: ...
    def create(self, project: Project, data: dict, *, identifier: Optional[str] = None) -> str: ...
    def load(self, project: Project, identifier: str) -> dict: ...
    def list(self, project: Project) -> dict[str, dict]: ...
    def save(self, project: Project, identifier: str, data: dict) -> None: ...
    def update(self, project: Project, identifier: str, changes: dict) -> dict: ...
    def delete(self, project: Project, identifier: str) -> None: ...
    def delete_directory(self, project: Project) -> None: ...
    def exports(self, project: Project) -> dict[str, Any]: ...
    def clone(self, source: Project, destination: Project) -> None: ...


class Component:
    """Subclass with explicit name/directory; JSON keys belong to the component.

    component.json holds configuration; records/<id>.json holds named definitions.
    Direct use requires workspace ownership; ProjectManager.component provides a
    lifecycle-checked, locked CRUD handle. Override validation/exports for domain
    semantics, and clone for artifacts outside this common JSON layout.
    """

    name: str
    directory: str

    def root(self, project: Project) -> Path:
        directory = validate_name(self.directory)
        if directory.lower() in {"tasks", "logs", "state", "cache"}:
            raise ValueError("Component directory conflicts with core storage")
        return self._checked(project.paths.root / directory)

    @staticmethod
    def _checked(path: Path) -> Path:
        for entry in (path, *path.parents):
            if entry.is_symlink() or is_junction(entry):
                raise ValueError("Component storage cannot follow linked paths")
        return path

    def _configuration_path(self, project: Project) -> Path:
        return self._checked(self.root(project) / "component.json")

    def _record_path(self, project: Project, identifier: str) -> Path:
        return self._checked(self.root(project) / "records" / f"{validate_name(identifier)}.json")

    @staticmethod
    def serialize(data: dict) -> str:
        """Encode an open JSON object without dropping unknown keys."""
        ProjectConfig.validate_settings(data)
        return json.dumps(data, ensure_ascii=False, allow_nan=False)

    @staticmethod
    def deserialize(value: str) -> dict:
        data = json.loads(value)
        ProjectConfig.validate_settings(data)
        return data

    def default_configuration(self) -> dict:
        return {}

    def validate_configuration(self, data: dict) -> None:
        pass

    def validate_record(self, identifier: str, data: dict) -> None:
        pass

    def initialize(self, project: Project) -> None:
        path = self._configuration_path(project)
        self._checked(self.root(project) / "records").mkdir(parents=True, exist_ok=True)
        if not path.exists():
            self.configure(project, self.default_configuration())

    def configure(self, project: Project, configuration: dict) -> None:
        data = self.deserialize(self.serialize(configuration))
        self.validate_configuration(data)
        atomic_json(self._configuration_path(project), data)

    def configuration(self, project: Project) -> dict:
        data = self.deserialize(self._configuration_path(project).read_text(encoding="utf-8"))
        self.validate_configuration(data)
        return data

    def create(self, project: Project, data: dict, *, identifier: Optional[str] = None) -> str:
        identifier = new_id() if identifier is None else identifier
        path = self._record_path(project, identifier)
        if path.exists():
            raise FileExistsError("Component record already exists")
        self._write(project, identifier, data)
        return identifier

    def _write(self, project: Project, identifier: str, data: dict) -> None:
        value = self.deserialize(self.serialize(data))
        self.validate_record(identifier, value)
        atomic_json(self._record_path(project, identifier), value)

    def load(self, project: Project, identifier: str) -> dict:
        data = self.deserialize(self._record_path(project, identifier).read_text(encoding="utf-8"))
        self.validate_record(identifier, data)
        return data

    def list(self, project: Project) -> dict[str, dict]:
        root = self._checked(self.root(project) / "records")
        return {path.stem: self.load(project, path.stem) for path in sorted(root.glob("*.json"))}

    def save(self, project: Project, identifier: str, data: dict) -> None:
        """Replace an existing definition; create is explicit."""
        if not self._record_path(project, identifier).is_file():
            raise FileNotFoundError("Component record does not exist")
        self._write(project, identifier, data)

    def update(self, project: Project, identifier: str, changes: dict) -> dict:
        """Shallow key update; nested values are replaced as complete values."""
        changes = self.deserialize(self.serialize(changes))
        data = self.load(project, identifier)
        data.update(changes)
        self.save(project, identifier, data)
        return data

    def delete(self, project: Project, identifier: str) -> None:
        path = self._record_path(project, identifier)
        path.unlink()
        sync_directory(path.parent)

    def delete_directory(self, project: Project) -> None:
        """Explicit permanent removal, including component-owned artifacts."""
        root = self.root(project)
        if root.exists():
            remove_named_tree(project.paths.root, root, self.directory)

    def exports(self, project: Project) -> dict[str, Any]:
        """Optional runtime capabilities; the base has no Tool/Engine dependency."""
        return {}

    def clone(self, source: Project, destination: Project) -> None:
        self.configure(destination, self.configuration(source))
        for identifier, data in self.list(source).items():
            self.create(destination, data, identifier=identifier)
