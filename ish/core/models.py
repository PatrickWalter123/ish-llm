from typing import Optional
from dataclasses import field
from ish.compat import dataclass
from datetime import datetime, timezone
from ish.compat import StrEnum
from uuid import uuid4

from .paths import ProjectPaths, RunPaths, StepPaths, TaskPaths


# ---------------------------------------------------------------------------
# Shared identity and UTC timestamp helpers
# ---------------------------------------------------------------------------
def new_id() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Persisted roles and lifecycle states
# ---------------------------------------------------------------------------

class TaskStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    DELETED = "deleted"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    COMMITTED = "committed"
    STREAMING = "streaming"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Project: persistent workspace and provider configuration
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ProjectConfig:
    model: str = ""
    temperature: Optional[float] = 0.7
    default_engine: str = "loop"
    # Only a reference may be stored here; actual credentials need SecretManager.
    credential_ref: Optional[str] = None
    api_base: Optional[str] = None


@dataclass(slots=True)
class Project:
    id: str
    title: str
    paths: ProjectPaths
    config: ProjectConfig = field(default_factory=ProjectConfig)
    created_at: str = field(default_factory=now)
    deleted: bool = False


# ---------------------------------------------------------------------------
# Task: long-lived session state (runtime asyncio objects live in services)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Task:
    id: str
    project_id: str
    title: str
    paths: TaskPaths
    status: TaskStatus = TaskStatus.IDLE
    default_engine: Optional[str] = None
    current_run_id: Optional[str] = None
    created_at: str = field(default_factory=now)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Message: Task conversation state reconstructed from JSONL events
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Message:
    id: str
    role: MessageRole
    content: str
    status: MessageStatus
    run_id: Optional[str] = None
    created_at: str = field(default_factory=now)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Run: one Engine execution for one committed request
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Run:
    id: str
    task_id: str
    input_message_id: str
    assistant_message_id: str
    engine: str
    paths: RunPaths
    status: RunStatus = RunStatus.PENDING
    created_at: str = field(default_factory=now)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Step: observable execution unit inside a Run
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Step:
    id: str
    run_id: str
    kind: str
    name: str
    paths: StepPaths
    status: StepStatus = StepStatus.PENDING
    created_at: str = field(default_factory=now)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)
