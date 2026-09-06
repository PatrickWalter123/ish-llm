"""Explicitly registered async tools; no automatic shell or filesystem access."""

import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from ish.compat import dataclass
from typing import Any

from jsonschema import Draft202012Validator


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]


class ToolRegistry:
    def __init__(self, tools: tuple[Tool, ...] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name) or tool.name in self._tools:
            raise ValueError("Invalid or duplicate tool name")
        schema = deepcopy(tool.parameters)
        if schema.get("type") != "object":
            raise ValueError("Tool parameters must describe an object")
        # Tool validation must not fetch remote schemas.
        def check_refs(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("$ref", "$dynamicRef") and not str(item).startswith("#"):
                        raise ValueError("Only local schema references are supported")
                    check_refs(item)
            elif isinstance(value, list):
                for item in value:
                    check_refs(item)
        check_refs(schema)
        Draft202012Validator.check_schema(schema)
        self._tools[tool.name] = Tool(tool.name, tool.description, schema, tool.handler)

    def definitions(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": tool.name, "description": tool.description,
            "parameters": deepcopy(tool.parameters),
        }} for tool in self._tools.values()]

    def prepare(self, name: str, arguments: str) -> tuple[Tool, dict[str, Any]]:
        """Validate before executing any tool in a returned batch."""
        try:
            tool = self._tools[name]
            def reject_constant(value: str) -> None:
                raise ValueError("Non-finite JSON constant")
            values = json.loads(arguments, parse_constant=reject_constant)
            if not isinstance(values, dict):
                raise ValueError("Expected argument object")
            json.dumps(values, allow_nan=False)
            Draft202012Validator(tool.parameters).validate(values)
        except Exception:
            raise ValueError("Unknown tool or invalid tool arguments") from None
        return tool, values
