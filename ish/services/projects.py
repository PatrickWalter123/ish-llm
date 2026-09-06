from typing import Optional
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from ish.core.models import Project, ProjectConfig, new_id
from ish.core.paths import ProjectPaths
from .storage import atomic_json, child, read_json, record, remove_owned_tree
from .tasks import TaskManager
from .logging import log_event
from .access import ProjectAccess
from .locking import WorkspaceOwnership, workspace_locked
from ish.components.registry import ComponentRegistry


class ProjectInitializer(Protocol):
    def initialize(self, project: Project) -> None: ...


class ProjectRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ownership = WorkspaceOwnership(root)

    def paths(self, project_id: str) -> ProjectPaths:
        return ProjectPaths(child(self.root, project_id))

    @workspace_locked
    def save(self, project: Project) -> None:
        data = record(project)
        data["config"] = asdict(project.config)
        atomic_json(project.paths.root / "project.json", data)
        log_event(project.paths.logs, "project.saved", entity_id=project.id)

    @workspace_locked
    def load(self, project_id: str) -> Project:
        paths = self.paths(project_id)
        data = read_json(paths.root / "project.json")
        if data["id"] != project_id:
            raise ValueError("Project ID mismatch")
        log_event(paths.logs, "project.loaded", entity_id=project_id)
        selected = data.get("components", [])
        if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
            raise ValueError("Invalid Project component selection")
        return Project(**{**data, "components": tuple(selected),
                          "config": ProjectConfig(**data["config"]), "paths": paths})

    @workspace_locked
    def delete(self, project: Project) -> None:
        """Remove an owned Project tree; lifecycle checks belong to the manager."""
        if project.paths.root.absolute() != self.paths(project.id).root.absolute():
            raise ValueError("Project path mismatch")
        self.load(project.id)
        remove_owned_tree(self.root, project.paths.root, project.id)
        log_event(self.root / "logs", "project.deleted", entity_id=project.id,
                  permanent=True)

    @workspace_locked
    def list(self, *, include_deleted: bool = False) -> list[Project]:
        projects = [self.load(path.parent.name) for path in self.root.glob("*/project.json")]
        return sorted((project for project in projects if include_deleted or not project.deleted),
                      key=lambda project: (project.created_at, project.id))


class ProjectManager:
    def __init__(self, repository: ProjectRepository, tasks: TaskManager,
                 initializers: tuple[ProjectInitializer, ...] = (), *,
                 components: Optional[ComponentRegistry] = None) -> None:
        self.repository = repository
        self.tasks = tasks
        self.access = ProjectAccess(repository)
        self.ownership = repository.ownership
        self.tasks.bind_project_access(self.access)
        self.components = components if components is not None else ComponentRegistry()
        self.initializers = (tasks, *initializers)

    @workspace_locked
    def create(self, title: str, *, config: Optional[ProjectConfig] = None,
               components: tuple[str, ...] = ()) -> Project:
        selected = self.components.validate(components)
        project_id = new_id()
        project = Project(project_id, title, self.repository.paths(project_id),
                          config=deepcopy(config) if config else ProjectConfig(), components=selected)
        self.repository.save(project)
        try:
            for initializer in self.initializers:
                initializer.initialize(deepcopy(project))
            self.components.initialize(project)
        except Exception:
            project.deleted = True
            self.repository.save(project)
            log_event(project.paths.logs, "project.initialization_failed", entity_id=project.id)
            raise
        log_event(project.paths.logs, "project.created", entity_id=project.id)
        return project

    @workspace_locked
    def save(self, project: Project) -> None:
        current = self.access.require(project)
        if project.deleted != current.deleted or project.components != current.components:
            raise ValueError("Use lifecycle or component APIs to change managed Project state")
        current.title, current.config = project.title, deepcopy(project.config)
        self.repository.save(current)

    @workspace_locked
    def set_components(self, project: Project, names: tuple[str, ...]) -> None:
        current = self.access.require(project)
        current.components = self.components.validate(names)
        # Initialization is idempotent. Failed initialization does not publish
        # the changed selection; existing component data is never removed.
        self.components.initialize(current)
        self.repository.save(current)
        project.components = current.components

    @workspace_locked
    def configure_component(self, project: Project, name: str, configuration: dict) -> None:
        current = self.access.require(project)
        self.components.configure(current, name, configuration)

    @workspace_locked
    def load(self, project_id: str) -> Project:
        return self.repository.load(project_id)

    @workspace_locked
    def list(self, *, include_deleted: bool = False) -> list[Project]:
        return self.repository.list(include_deleted=include_deleted)

    @workspace_locked
    def delete(self, project: Project, *, permanent: bool = False) -> None:
        """Mark deleted by default; permanent=True removes the entire owned tree."""
        if type(permanent) is not bool:
            raise TypeError("permanent must be a bool")
        current = self.access.require(project, allow_deleted=True)
        for task in self.tasks.list(current, include_deleted=True):
            self.tasks.require_inactive(task)
        if permanent:
            self.repository.delete(current)
        else:
            current.deleted = True
            self.repository.save(current)
            log_event(current.paths.logs, "project.deleted", entity_id=current.id,
                      permanent=False)
        project.deleted = True

    @workspace_locked
    def restore(self, project: Project) -> None:
        current = self.access.require(project, allow_deleted=True)
        # Re-run selected, idempotent components before activating a Project
        # whose initialization may previously have failed.
        self.components.initialize(current)
        current.deleted = False
        self.repository.save(current)
        for initializer in self.initializers:
            try:
                initializer.initialize(deepcopy(current))
            except Exception:
                current.deleted = True
                self.repository.save(current)
                raise
        project.deleted = False
        log_event(current.paths.logs, "project.restored", entity_id=current.id)

    @workspace_locked
    def clone(self, source: Project, *, title: Optional[str] = None) -> Project:
        source = self.access.require(source)
        self.components.validate(source.components)
        tasks = self.tasks.list(source)
        for task in tasks:
            self.tasks.require_inactive(task)
        clone = self.create(title if title is not None else source.title, config=source.config,
                            components=source.components)
        try:
            self.components.clone(source, clone)
            for task in tasks:
                self.tasks.clone(task, clone)
        except Exception:
            clone.deleted = True
            self.repository.save(clone)
            raise
        log_event(clone.paths.logs, "project.cloned", entity_id=clone.id,
                  related_id=source.id)
        return clone
