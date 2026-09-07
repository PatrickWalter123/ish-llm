"""Execution strategies emit events and never persist domain state."""

from .base import BaseEngine, EngineContext, EngineRegistry

__all__ = ["EngineContext", "EngineRegistry", "BaseEngine"]
