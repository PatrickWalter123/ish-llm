from ish.compat import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    root: Path

    @property
    def memory(self) -> Path:
        return self.root / "memory"

    @property
    def tasks(self) -> Path:
        return self.root / "tasks"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"


@dataclass(frozen=True, slots=True)
class TaskPaths:
    root: Path

    @property
    def conversation(self) -> Path:
        return self.root / "conversation.jsonl"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def attachments(self) -> Path:
        return self.root / "attachments"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path

    @property
    def steps(self) -> Path:
        return self.root / "steps"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"


@dataclass(frozen=True, slots=True)
class StepPaths:
    root: Path

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"
