"""Resolve selected component identities without owning their path layouts."""

import re
from copy import deepcopy
from typing import Any

from ish.core.models import Project
from ish.services.logging import log_event
from .base import ProjectComponent, validate_name


class ComponentRegistry:
    def __init__(self, components: tuple[ProjectComponent, ...] = ()) -> None:
        self._components: dict[str, ProjectComponent] = {}
        for component in components:
            self.register(component)

    def register(self, component: ProjectComponent) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", component.name) or component.name in self._components:
            raise ValueError("Invalid or duplicate component identity")
        directory = validate_name(getattr(component, "directory", None)).lower()
        if directory in {"tasks", "logs", "state", "cache"} or any(
                item.directory.lower() == directory for item in self._components.values()):
            raise ValueError("Component directory is reserved or already owned")
        capabilities = component.capabilities
        if (not isinstance(capabilities, tuple)
                or any(not isinstance(name, str) or not name for name in capabilities)
                or len(set(capabilities)) != len(capabilities)):
            raise ValueError("Capabilities must be a tuple of distinct names")
        self._components[component.name] = component

    def get(self, name: str) -> ProjectComponent:
        try:
            return self._components[name]
        except KeyError:
            raise ValueError("Unavailable Project component") from None

    def validate(self, names: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(names, (tuple, list)) or any(not isinstance(name, str) for name in names):
            raise ValueError("Component selection must be a sequence of identities")
        if len(set(names)) != len(names) or any(name not in self._components for name in names):
            raise ValueError("Duplicate or unavailable Project component")
        return tuple(names)

    def initialize(self, project: Project) -> None:
        for name in self.validate(project.components):
            self._components[name].initialize(deepcopy(project))
        log_event(project.paths.logs, "components.initialized", entity_id=project.id,
                  count=len(project.components))

    def configure(self, project: Project, name: str, configuration: dict) -> None:
        self.validate(project.components)
        if name not in project.components:
            raise ValueError("Component is not enabled for this Project")
        self._components[name].configure(deepcopy(project), deepcopy(configuration))
        log_event(project.paths.logs, "component.configured", entity_id=project.id)

    def resolve(self, project: Project, capability: str) -> tuple[Any, ...]:
        """Collect optional exports without knowing their domain or value types."""
        values = []
        for name in self.validate(project.components):
            component = self._components[name]
            if capability in component.capabilities:
                values.append(component.resolve(deepcopy(project), capability))
        return tuple(values)

    def clone(self, source: Project, destination: Project) -> None:
        for name in self.validate(source.components):
            self._components[name].clone(deepcopy(source), deepcopy(destination))
