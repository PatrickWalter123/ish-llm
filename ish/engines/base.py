from typing import Optional
from collections.abc import AsyncIterator
from dataclasses import field
from ish.compat import dataclass
from ish.compat import StrEnum
from typing import Protocol

from ish.core.models import Message, Project, Run, Task
from ish.components.tools import ToolRegistry


class EngineEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_INTERRUPTED = "step_interrupted"
    STEP_CANCELLED = "step_cancelled"


@dataclass(frozen=True, slots=True)
class EngineEvent:
    type: EngineEventType
    text: str = ""
    step_id: Optional[str] = None
    kind: str = "llm"
    name: str = ""
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class EngineContext:
    project: Project
    task: Task
    run: Run
    messages: tuple[Message, ...]
    # Per-Run snapshot of Project capabilities, never part of persisted models.
    tools: ToolRegistry = field(default_factory=ToolRegistry)


class Engine(Protocol):
    def execute(self, context: EngineContext) -> AsyncIterator[EngineEvent]: ...


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}

    def register(self, name: str, engine: Engine) -> None:
        if name in self._engines:
            raise ValueError(f"Engine already registered: {name}")
        self._engines[name] = engine

    def resolve(self, name: str) -> Engine:
        return self._engines[name]
