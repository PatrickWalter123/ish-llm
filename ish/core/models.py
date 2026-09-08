from typing import Optional
import json
from copy import deepcopy
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
# Project: extensible, JSON-only configuration passed to Tasks and Engines
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ProjectConfig:
    default_engine: str = "loop"
    completion: dict = field(default_factory=dict)
    engines: dict = field(default_factory=dict)
    task_defaults: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    @staticmethod
    def validate_settings(value: dict) -> None:
        """Reject runtime objects, lossy JSON keys and persisted credentials."""
        if not isinstance(value, dict):
            raise TypeError("Settings must be a dictionary")

        def check(item):
            if isinstance(item, dict):
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise TypeError("Settings keys must be strings")
                    if key.lower() in {"api_key", "authorization", "password", "credentials",
                                      "access_token", "secret_key", "api_token"}:
                        raise ValueError("Credentials belong in the environment or runtime arguments")
                    check(nested)
            elif isinstance(item, list):
                for nested in item:
                    check(nested)
            elif item is not None and not isinstance(item, (str, bool, int, float)):
                raise TypeError("Settings must contain only JSON values")

        check(value)
        json.dumps(value, allow_nan=False)

    def validate(self) -> None:
        if not isinstance(self.default_engine, str) or not self.default_engine.strip():
            raise ValueError("Default Engine must be a nonempty string")
        for section in (self.completion, self.engines, self.task_defaults, self.data):
            self.validate_settings(section)
        if any(not isinstance(options, dict) for options in self.engines.values()):
            raise TypeError("Each Engine configuration must be a dictionary")
        self.validate_task(self.task_defaults)

    @classmethod
    def validate_task(cls, config: dict) -> None:
        cls.validate_settings(config)
        for name in ("completion", "engines", "data"):
            if name in config and not isinstance(config[name], dict):
                raise TypeError("Task configuration sections must be dictionaries")
        if any(not isinstance(options, dict) for options in config.get("engines", {}).values()):
            raise TypeError("Each Engine configuration must be a dictionary")

    @staticmethod
    def merge(defaults: dict, overrides: dict) -> dict:
        """Recursively merge dictionaries; lists/scalars replace the default."""
        result = deepcopy(defaults)
        for key, value in overrides.items():
            result[key] = (ProjectConfig.merge(result[key], value)
                           if isinstance(result.get(key), dict) and isinstance(value, dict)
                           else deepcopy(value))
        return result

    def for_engine(self, name: str, task_config: Optional[dict] = None) -> dict:
        """Return an isolated Project + Task settings snapshot for one Engine."""
        task_config = task_config if task_config is not None else {}
        self.validate_task(task_config)
        return {
            "completion": self.merge(self.completion, task_config.get("completion", {})),
            "engine": self.merge(self.engines.get(name, {}), task_config.get("engines", {}).get(name, {})),
            "data": self.merge(self.data, task_config.get("data", {})),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        """Load current settings and migrate the former flat provider fields."""
        data = deepcopy(data)
        completion = data.pop("completion", {})
        legacy = {key: data.pop(key) for key in ("model", "temperature", "api_base") if key in data}
        data.pop("credential_ref", None)  # Obsolete references are never resolved.
        known = {key: data.pop(key) for key in ("default_engine", "engines", "task_defaults") if key in data}
        custom = data.pop("data", {})
        return cls(completion=cls.merge(legacy, completion), data=cls.merge(data, custom), **known)


@dataclass(slots=True)
class Project:
    id: str
    title: str
    paths: ProjectPaths
    config: ProjectConfig = field(default_factory=ProjectConfig)
    created_at: str = field(default_factory=now)
    deleted: bool = False
    # Component identities only. Each component owns its configuration and paths.
    components: tuple[str, ...] = ()


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
    config: dict = field(default_factory=dict)


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
