"""Project-scoped tool selection; Python handlers remain application-owned."""

from pathlib import Path
from typing import Optional
from ish.compat import dataclass
from ish.core.models import Project
from ish.components.base import Component
from .registry import Tool, ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolPaths:
    root: Path

    @classmethod
    def for_project(cls, project: Project) -> "ToolPaths":
        return cls(project.paths.root / ToolComponent.directory)

    @property
    def configuration(self) -> Path:
        return self.root / "component.json"


class ToolComponent(Component):
    name = "tools"
    directory = "tools"

    def __init__(self, catalog: ToolRegistry) -> None:
        self.catalog = catalog

    def default_configuration(self) -> dict:
        return {"enabled": []}

    def validate_configuration(self, configuration: dict) -> None:
        if "enabled" not in configuration:
            raise ValueError("Tool configuration requires enabled names")
        names = configuration["enabled"]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("Enabled tools must be a list of names")
        self.catalog.select(tuple(names))  # validate before replacing configuration

    def _bind(self, identifier: str, data: dict) -> Tool:
        handler = self.catalog.get(identifier).handler
        function = data.get("function")
        if (data.get("type") != "function" or not isinstance(function, dict)
                or function.get("name") != identifier
                or not isinstance(function.get("parameters"), dict)
                or not isinstance(function.get("description", ""), str)):
            raise ValueError("Expected a named function tool definition")
        tool = Tool(identifier, function.get("description", ""), function["parameters"], handler, data)
        ToolRegistry((tool,))  # validate schema and native definition together
        return tool

    def validate_record(self, identifier: str, data: dict) -> None:
        self._bind(identifier, data)

    def create(self, project: Project, data: dict, *, identifier: Optional[str] = None) -> str:
        if identifier is None and isinstance(data, dict) and isinstance(data.get("function"), dict):
            identifier = data["function"].get("name")
        return super().create(project, data, identifier=identifier)

    def delete(self, project: Project, identifier: str) -> None:
        if identifier in self.configuration(project)["enabled"]:
            raise ValueError("Disable the tool before deleting its definition")
        super().delete(project, identifier)

    def resolve_tools(self, project: Project) -> ToolRegistry:
        names = self.configuration(project)["enabled"]
        tools = ToolRegistry()
        for name in names:
            try:
                data = self.load(project, name)
            except FileNotFoundError:
                # Existing enabled-name configurations use catalog definitions.
                tool = self.catalog.get(name)
            else:
                tool = self._bind(name, data)
            tools.register(tool)
        return tools

    def exports(self, project: Project) -> dict:
        return {"tools": self.resolve_tools(project)}
