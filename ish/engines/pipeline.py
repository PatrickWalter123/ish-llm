"""Compose developer-supplied preparation and Engines inside a single Run."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Optional

from .base import BaseEngine, Engine, EngineContext, EngineEvent, EngineEventType


class PipelineError(RuntimeError):
    """Preparation/stage lifecycle failure."""


class PreparationStep(BaseEngine):
    """Convenience BaseEngine for async preparation; defaults to a 60s deadline."""

    def __init__(self, name: str, action: Callable[[EngineContext], Awaitable[None]], *,
                 kind: str = "preparation", timeout_seconds: Optional[float] = 60.0) -> None:
        if not callable(action):
            raise TypeError("Preparation action must be callable")
        super().__init__(name, kind=kind, action=action, timeout_seconds=timeout_seconds,
                         error_message="Preparation failed")


class PipelineEngine:
    """Ordered Engine composition; all stages share the same Run context."""

    def __init__(self, stages: Sequence[Engine]) -> None:
        self.stages = tuple(stages)
        if not self.stages or any(not callable(getattr(stage, "execute", None)) for stage in self.stages):
            raise ValueError("Pipeline requires at least one Engine stage")

    async def execute(self, context: EngineContext) -> AsyncIterator[EngineEvent]:
        for stage in self.stages:
            active = set()
            events = stage.execute(context)
            try:
                async for event in events:
                    if event.type == EngineEventType.STEP_STARTED:
                        active.add(event.step_id)
                    elif event.type == EngineEventType.STEP_COMPLETED:
                        active.discard(event.step_id)
                    yield event
                    if event.type in (EngineEventType.STEP_FAILED,
                                      EngineEventType.STEP_INTERRUPTED,
                                      EngineEventType.STEP_CANCELLED):
                        raise PipelineError("Pipeline stage did not complete")
            finally:
                close = getattr(events, "aclose", None)
                if close is not None:
                    await close()
            if active:
                raise PipelineError("Pipeline stage left unfinished Steps")
