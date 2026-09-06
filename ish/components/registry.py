"""Resolve selected component identities without owning their path layouts."""

import re
from copy import deepcopy
from typing import Protocol

from ish.core.models import Project
from ish.services.logging import log_event
from .base import ProjectComponent
from .tools import ToolRegistry


class CapabilityResolver(Protocol):
    def resolve_tools(self, project: Project) -> ToolRegistry: ...


class ComponentRegistry:
    def __init__(self, components: tuple[ProjectComponent, ...] = ()) -> None:
        self._components: dict[str, ProjectComponent] = {}
        for component in components:
            self.register(component)

    def register(self, component: ProjectComponent) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", component.name) or component.name in self._components:
            raise ValueError("Invalid or duplicate component identity")
        self._components[component.name] = component

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

    def resolve_tools(self, project: Project) -> ToolRegistry:
        tools = ToolRegistry()
        for name in self.validate(project.components):
            tools.extend(self._components[name].resolve_tools(deepcopy(project)))
        return tools

    def clone(self, source: Project, destination: Project) -> None:
        for name in self.validate(source.components):
            self._components[name].clone(deepcopy(source), deepcopy(destination))
