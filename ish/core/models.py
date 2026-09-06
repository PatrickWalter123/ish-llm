from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from .paths import ProjectPaths, RunPaths, StepPaths, TaskPaths


def new_id() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


@dataclass(slots=True)
class ProjectConfig:
    model: str = ""
    temperature: float | None = 0.7
    default_engine: str = "fake"
    # Only a reference may be stored here; actual credentials need SecretManager.
    credential_ref: str | None = None
    api_base: str | None = None


@dataclass(slots=True)
class Project:
    id: str
    title: str
    paths: ProjectPaths
    config: ProjectConfig = field(default_factory=ProjectConfig)
    created_at: str = field(default_factory=now)
    deleted: bool = False


@dataclass(slots=True)
class Task:
    id: str
    project_id: str
    title: str
    paths: TaskPaths
    status: TaskStatus = TaskStatus.IDLE
    default_engine: str | None = None
    current_run_id: str | None = None
    created_at: str = field(default_factory=now)
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class Message:
    id: str
    role: MessageRole
    content: str
    status: MessageStatus
    run_id: str | None = None
    created_at: str = field(default_factory=now)
    metadata: dict = field(default_factory=dict)


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
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class Step:
    id: str
    run_id: str
    kind: str
    name: str
    paths: StepPaths
    status: StepStatus = StepStatus.PENDING
    created_at: str = field(default_factory=now)
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)
