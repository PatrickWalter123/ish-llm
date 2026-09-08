"""Lifecycle-checked component data access without domain-specific knowledge."""

from copy import deepcopy
from typing import Optional

from ish.core.models import Project
from ish.components.registry import ComponentRegistry
from .access import ProjectAccess
from .locking import workspace_locked
from .logging import log_event


class ComponentData:
    """A reusable handle; every operation reloads Project state under its lock."""

    def __init__(self, access: ProjectAccess, registry: ComponentRegistry,
                 project: Project, name: str) -> None:
        self.access, self.registry = access, registry
        self.project, self.name = deepcopy(project), name
        self.ownership = access.repository.ownership

    def _current(self):
        project = self.access.require(self.project)
        self.registry.validate(project.components)
        if self.name not in project.components:
            raise ValueError("Component is not enabled for this Project")
        return project, self.registry.get(self.name)

    @workspace_locked
    def configuration(self) -> dict:
        project, component = self._current()
        return component.configuration(project)

    @workspace_locked
    def configure(self, data: dict) -> None:
        project, component = self._current()
        component.configure(project, data)
        log_event(project.paths.logs, "component.configured", entity_id=project.id)

    @workspace_locked
    def create(self, data: dict, *, identifier: Optional[str] = None) -> str:
        project, component = self._current()
        identifier = component.create(project, data, identifier=identifier)
        log_event(project.paths.logs, "component.record_created", entity_id=project.id)
        return identifier

    @workspace_locked
    def load(self, identifier: str) -> dict:
        project, component = self._current()
        return component.load(project, identifier)

    @workspace_locked
    def list(self) -> dict[str, dict]:
        project, component = self._current()
        return component.list(project)

    @workspace_locked
    def save(self, identifier: str, data: dict) -> None:
        project, component = self._current()
        component.save(project, identifier, data)
        log_event(project.paths.logs, "component.record_saved", entity_id=project.id)

    @workspace_locked
    def update(self, identifier: str, changes: dict) -> dict:
        project, component = self._current()
        data = component.update(project, identifier, changes)
        log_event(project.paths.logs, "component.record_updated", entity_id=project.id)
        return data

    @workspace_locked
    def delete(self, identifier: str) -> None:
        project, component = self._current()
        component.delete(project, identifier)
        log_event(project.paths.logs, "component.record_deleted", entity_id=project.id)
