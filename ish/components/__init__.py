"""Project-owned component data and optional runtime capabilities."""

from .base import Component, ProjectComponent
from .registry import ComponentRegistry

__all__ = ["Component", "ProjectComponent", "ComponentRegistry"]
