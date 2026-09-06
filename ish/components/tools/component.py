"""Project-scoped tool selection; Python handlers remain application-owned."""

from pathlib import Path
from ish.compat import dataclass
from ish.core.models import Project
from ish.services.storage import atomic_json, read_json
from .registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolPaths:
    root: Path

    @classmethod
    def for_project(cls, project: Project) -> "ToolPaths":
        return cls(project.paths.root / "tools")

    @property
    def configuration(self) -> Path:
        return self.root / "component.json"


class ToolComponent:
    name = "tools"

    def __init__(self, catalog: ToolRegistry) -> None:
        self.catalog = catalog

    def initialize(self, project: Project) -> None:
        paths = ToolPaths.for_project(project)
        if not paths.configuration.exists():
            atomic_json(paths.configuration, {"enabled": []})

    def configure(self, project: Project, configuration: dict) -> None:
        if not isinstance(configuration, dict) or set(configuration) != {"enabled"}:
            raise ValueError("Tool configuration requires enabled names only")
        names = configuration["enabled"]
        if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) for name in names):
            raise ValueError("Enabled tools must be a list of names")
        self.catalog.select(tuple(names))  # validate before replacing configuration
        atomic_json(ToolPaths.for_project(project).configuration, {"enabled": list(names)})

    def resolve_tools(self, project: Project) -> ToolRegistry:
        data = read_json(ToolPaths.for_project(project).configuration)
        names = data["enabled"]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("Invalid persisted tool selection")
        return self.catalog.select(tuple(names))

    def clone(self, source: Project, destination: Project) -> None:
        self.configure(destination, read_json(ToolPaths.for_project(source).configuration))
