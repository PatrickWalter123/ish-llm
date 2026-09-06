"""Component extension contract; components own their Project subdirectories."""

from typing import Protocol
from ish.core.models import Project
from .tools import ToolRegistry


class ProjectComponent(Protocol):
    name: str

    def initialize(self, project: Project) -> None: ...
    def configure(self, project: Project, configuration: dict) -> None: ...
    def resolve_tools(self, project: Project) -> ToolRegistry: ...
    def clone(self, source: Project, destination: Project) -> None: ...


class Component:
    """Defaults for components that do not yet expose tools or configuration."""

    def configure(self, project: Project, configuration: dict) -> None:
        raise ValueError("Component does not support configuration")

    def resolve_tools(self, project: Project) -> ToolRegistry:
        return ToolRegistry()

    def clone(self, source: Project, destination: Project) -> None:
        # Default cloning copies selection only, not artifacts or live resources.
        pass
