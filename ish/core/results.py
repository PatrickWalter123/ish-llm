"""Persistable execution observations, separate from Project configuration."""

from dataclasses import field
from copy import deepcopy
from datetime import datetime
from typing import Optional

from ish.compat import dataclass
from .models import Run, RunStatus, new_id, now


def duration_seconds(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if start is None or end is None:
        return None
    return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


@dataclass(slots=True)
class CompletionResult:
    id: str = field(default_factory=new_id)
    step_id: Optional[str] = None
    model: Optional[str] = None
    response_id: Optional[str] = None
    finish_reason: Optional[str] = None
    status: RunStatus = RunStatus.RUNNING
    started_at: str = field(default_factory=now)
    ended_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    usage: dict = field(default_factory=dict)
    usage_complete: bool = False

    @classmethod
    def for_run(cls, data: dict, run: Run) -> "CompletionResult":
        result = cls(**{**deepcopy(data), "status": RunStatus(data["status"])})
        if (run.status not in (RunStatus.PENDING, RunStatus.RUNNING)
                and result.status in (RunStatus.PENDING, RunStatus.RUNNING)):
            result.status = run.status if run.status != RunStatus.COMPLETED else RunStatus.INTERRUPTED
            result.ended_at = run.ended_at
            result.duration_seconds = duration_seconds(result.started_at, result.ended_at)
        return result


@dataclass(slots=True)
class ExecutionResult:
    """A query view of one Run, never a second persisted copy."""
    project_id: str
    task_id: str
    run_id: str
    engine: str
    status: RunStatus
    started_at: Optional[str]
    ended_at: Optional[str]
    duration_seconds: Optional[float]
    completions: list[CompletionResult] = field(default_factory=list)
    # None means unknown/partial, never an invented zero for missing usage.
    usage: dict = field(default_factory=dict)
    error: Optional[str] = None
    error_code: Optional[str] = None

    @classmethod
    def from_run(cls, project_id: str, run: Run) -> "ExecutionResult":
        completions = [CompletionResult.for_run(data, run)
                       for data in run.metadata.get("completions", [])]
        usage = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [item.usage.get(key) for item in completions]
            usage[key] = (sum(values) if values and all(type(value) is int for value in values)
                          and all(item.usage_complete for item in completions) else None)
        return cls(project_id, run.task_id, run.id, run.engine, run.status,
                   run.started_at, run.ended_at, duration_seconds(run.started_at, run.ended_at),
                   completions=completions, usage=usage, error=run.error, error_code=run.error_code)

    @property
    def total_tokens(self) -> Optional[int]:
        return self.usage.get("total_tokens")

    @property
    def finish_reasons(self) -> list[Optional[str]]:
        return [completion.finish_reason for completion in self.completions]
