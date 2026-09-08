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

class ProjectConfig(dict):
    """Open JSON workspace settings with mapping access and attribute shortcuts.

    Unknown top-level keys round-trip unchanged. Reserved sections are validated
    when constructing or saving; nested mutation is allowed between saves.
    """

    def __init__(self, values: Optional[dict] = None, **settings) -> None:
        defaults = {"default_engine": "loop", "completion": {}, "engines": {},
                    "task_defaults": {}, "data": {}}
        if values is not None:
            defaults.update(deepcopy(dict(values)))
        defaults.update(deepcopy(settings))
        super().__init__(defaults)
        self.validate()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self[name] = value

    @staticmethod
    def validate_settings(value: dict) -> None:
        """Check JSON compatibility without restricting application field names."""
        if not isinstance(value, dict):
            raise TypeError("Settings must be a dictionary")
        def check(item):
            if isinstance(item, dict):
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise TypeError("Settings keys must be strings")
                    check(nested)
            elif isinstance(item, list):
                for nested in item:
                    check(nested)
            elif item is not None and not isinstance(item, (str, bool, int, float)):
                raise TypeError("Settings must contain only JSON values")
        check(value)
        json.dumps(value, allow_nan=False)

    def validate(self) -> None:
        self.validate_settings(self)
        if not isinstance(self.get("default_engine"), str) or not self["default_engine"].strip():
            raise ValueError("Default Engine must be a nonempty string")
        for section in ("completion", "engines", "task_defaults", "data"):
            if not isinstance(self.get(section), dict):
                raise TypeError(f"{section} must be a dictionary")
        self.validate_task(self)
        self.validate_task(self.task_defaults)

    @classmethod
    def validate_task(cls, config: dict) -> None:
        cls.validate_settings(config)
        for name in ("completion", "engines", "data"):
            if name in config and not isinstance(config[name], dict):
                raise TypeError("Task configuration sections must be dictionaries")
        if any(not isinstance(options, dict) for options in config.get("engines", {}).values()):
            raise TypeError("Each Engine configuration must be a dictionary")

    def to_dict(self) -> dict:
        self.validate()
        return deepcopy(dict(self))

    def serialize(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False)

    @classmethod
    def deserialize(cls, value: str) -> "ProjectConfig":
        return cls.from_dict(json.loads(value))

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
        """Detached settings including arbitrary workspace and Task keys."""
        self.validate()
        task_config = task_config if task_config is not None else {}
        self.validate_task(task_config)
        result = self.merge(dict(self), task_config)
        result["engine"] = self.merge(self.engines.get(name, {}), task_config.get("engines", {}).get(name, {}))
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        cls.validate_settings(data)
        data = deepcopy(data)
        # Preserve the historical flat model settings while leaving new keys in place.
        if "completion" not in data:
            legacy = {key: data.pop(key) for key in ("model", "temperature", "api_base") if key in data}
            data["completion"] = legacy
        return cls(data)


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
    error_code: Optional[str] = None


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
