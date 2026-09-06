"""Deterministic test engine with controllable streaming and failure points."""
from typing import Optional

import asyncio
from collections.abc import AsyncIterator

from ish.core.models import new_id
from ish.engines.base import EngineContext, EngineEvent, EngineEventType


class FakeStreamingEngine:
    def __init__(self, chunks: tuple[str, ...] = ("Hello", " ", "world"), *,
                 delay: float = 0, gate: Optional[asyncio.Event] = None,
                 fail_after: Optional[int] = None,
                 fail_inputs: Optional[frozenset[str]] = None) -> None:
        if delay < 0 or (fail_after is not None and not 0 <= fail_after <= len(chunks)):
            raise ValueError("Invalid fake engine timing or failure point")
        self.chunks = chunks
        self.delay = delay
        self.gate = gate
        self.fail_after = fail_after
        self.fail_inputs = fail_inputs
        self.contexts: list[EngineContext] = []
        self.active = 0
        self.max_active = 0
        self.cancelled = 0

    async def execute(self, context: EngineContext) -> AsyncIterator[EngineEvent]:
        self.contexts.append(context)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        step_id = new_id()
        should_fail = self.fail_inputs is None or context.messages[-1].content in self.fail_inputs
        try:
            yield EngineEvent(EngineEventType.STEP_STARTED, step_id=step_id,
                              kind="llm", name="Fake streaming completion")
            for index in range(len(self.chunks) + 1):
                if should_fail and self.fail_after == index:
                    yield EngineEvent(EngineEventType.STEP_FAILED, step_id=step_id,
                                      error="Intentional fake engine failure")
                    raise RuntimeError("Intentional fake engine failure")
                if index == len(self.chunks):
                    break
                yield EngineEvent(EngineEventType.TEXT_DELTA, text=self.chunks[index])
                if index == 0 and self.gate is not None:
                    await self.gate.wait()
                await asyncio.sleep(self.delay)
            yield EngineEvent(EngineEventType.STEP_COMPLETED, step_id=step_id)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
