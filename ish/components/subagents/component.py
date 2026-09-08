"""Specialized LLM settings as open definitions for workflow nodes and tools."""

from ish.components.base import Component


class SubagentComponent(Component):
    name = "subagents"
    directory = "subagents"
