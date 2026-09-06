"""Best-effort display notifications, separate from execution success/failure."""

from copy import deepcopy
from typing import Callable, Optional

from ish.core.models import Run
from ish.engines.base import EngineEvent
from .logging import log_event


class RunEventPublisher:
    def __init__(self, callback: Optional[Callable[[Run, EngineEvent], None]] = None) -> None:
        self.callback = callback

    def publish(self, run: Run, event: EngineEvent) -> None:
        # UI callbacks must be synchronous and nonblocking. Persistence has
        # already succeeded; a display exception must not fail the Engine.
        if self.callback is not None:
            try:
                self.callback(deepcopy(run), deepcopy(event))
            except Exception:
                log_event(run.paths.logs, "observer.failed", entity_id=run.id)
