"""Adapt generic component exports to one Run's tool snapshot."""

from typing import Protocol
from ish.core.models import Project
from ish.components.registry import ComponentRegistry
from .registry import ToolRegistry


class CapabilityResolver(Protocol):
    def resolve_tools(self, project: Project) -> ToolRegistry: ...


class ComponentToolResolver:
    def __init__(self, components: ComponentRegistry) -> None:
        self.components = components

    def resolve_tools(self, project: Project) -> ToolRegistry:
        tools = ToolRegistry()
        for exported in self.components.resolve(project, "tools"):
            if not isinstance(exported, ToolRegistry):
                raise TypeError("Tool capability must export a ToolRegistry")
            tools.extend(exported)
        return tools
