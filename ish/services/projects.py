from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from ish.core.models import Project, ProjectConfig, new_id
from ish.core.paths import ProjectPaths
from .storage import atomic_json, child, read_json, record
from .tasks import TaskManager
from .deletion import remove_owned_tree
from .logging import log_event


class ProjectInitializer(Protocol):
    def initialize(self, project: Project) -> None: ...


class ProjectRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def paths(self, project_id: str) -> ProjectPaths:
        return ProjectPaths(child(self.root, project_id))

    def save(self, project: Project) -> None:
        data = record(project)
        data["config"] = asdict(project.config)
        atomic_json(project.paths.root / "project.json", data)
        log_event(project.paths.logs, "project.saved", entity_id=project.id)

    def load(self, project_id: str) -> Project:
        paths = self.paths(project_id)
        data = read_json(paths.root / "project.json")
        if data["id"] != project_id:
            raise ValueError("Project ID mismatch")
        log_event(paths.logs, "project.loaded", entity_id=project_id)
        return Project(**{**data, "config": ProjectConfig(**data["config"]), "paths": paths})

    def delete(self, project: Project) -> None:
        """Remove an owned Project tree; lifecycle checks belong to the manager."""
        if project.paths.root.absolute() != self.paths(project.id).root.absolute():
            raise ValueError("Project path mismatch")
        self.load(project.id)
        remove_owned_tree(self.root, project.paths.root, project.id)
        log_event(self.root / "logs", "project.deleted", entity_id=project.id,
                  permanent=True)

    def list(self, *, include_deleted: bool = False) -> list[Project]:
        projects = [self.load(path.parent.name) for path in self.root.glob("*/project.json")]
        return sorted((project for project in projects if include_deleted or not project.deleted),
                      key=lambda project: (project.created_at, project.id))


class ProjectManager:
    def __init__(self, repository: ProjectRepository, tasks: TaskManager,
                 initializers: tuple[ProjectInitializer, ...] = ()) -> None:
        self.repository = repository
        self.tasks = tasks
        self.initializers = (tasks, *initializers)

    def create(self, title: str, *, config: ProjectConfig | None = None) -> Project:
        project_id = new_id()
        project = Project(project_id, title, self.repository.paths(project_id),
                          config=deepcopy(config) if config else ProjectConfig())
        self.save(project)
        for initializer in self.initializers:
            initializer.initialize(project)
        log_event(project.paths.logs, "project.created", entity_id=project.id)
        return project

    def save(self, project: Project) -> None:
        self.repository.save(project)

    def load(self, project_id: str) -> Project:
        return self.repository.load(project_id)

    def list(self, *, include_deleted: bool = False) -> list[Project]:
        return self.repository.list(include_deleted=include_deleted)

    def delete(self, project: Project, *, permanent: bool = False) -> None:
        """Mark deleted by default; permanent=True removes the entire owned tree."""
        if type(permanent) is not bool:
            raise TypeError("permanent must be a bool")
        current = self.load(project.id)
        if current.paths.root.absolute() != project.paths.root.absolute():
            raise ValueError("Project path mismatch")
        for task in self.tasks.list(current, include_deleted=True):
            self.tasks.require_inactive(task)
        if permanent:
            self.repository.delete(current)
        else:
            current.deleted = True
            self.save(current)
            log_event(current.paths.logs, "project.deleted", entity_id=current.id,
                      permanent=False)
        project.deleted = True

    def restore(self, project: Project) -> None:
        current = self.load(project.id)
        current.deleted = False
        self.save(current)
        project.deleted = False
        log_event(current.paths.logs, "project.restored", entity_id=current.id)

    def clone(self, source: Project, *, title: str | None = None) -> Project:
        tasks = self.tasks.list(source)
        for task in tasks:
            self.tasks.require_inactive(task)
        clone = self.create(title if title is not None else source.title, config=source.config)
        for task in tasks:
            self.tasks.clone(task, clone)
        log_event(clone.paths.logs, "project.cloned", entity_id=clone.id,
                  related_id=source.id)
        return clone
