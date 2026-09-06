from ish.core.models import Run, Step, StepStatus, new_id, now
from ish.core.paths import StepPaths
from ish.engines.base import EngineEvent, EngineEventType
from .storage import atomic_json, child, read_json, record
from .logging import log_event


class StepRepository:
    """Step metadata and paths beneath the owning Run's steps root."""

    def paths(self, run: Run, step_id: str) -> StepPaths:
        return StepPaths(child(run.paths.steps, step_id))

    def exists(self, run: Run, step_id: str) -> bool:
        return (self.paths(run, step_id).root / "step.json").exists()

    def save(self, step: Step) -> None:
        atomic_json(step.paths.root / "step.json", record(step))
        log_event(step.paths.logs, "step.saved", entity_id=step.id, status=step.status)

    def load(self, run: Run, step_id: str) -> Step:
        paths = self.paths(run, step_id)
        data = read_json(paths.root / "step.json")
        if data["id"] != step_id or data["run_id"] != run.id:
            raise ValueError("Step ownership mismatch")
        log_event(paths.logs, "step.loaded", entity_id=step_id)
        return Step(**{**data, "status": StepStatus(data["status"]), "paths": paths})

    def list(self, run: Run) -> list[Step]:
        return sorted((self.load(run, path.parent.name)
                       for path in run.paths.steps.glob("*/step.json")),
                      key=lambda step: (step.created_at, step.id))


class StepManager:
    """Step lifecycle; storage is delegated to StepRepository."""

    def __init__(self, repository: StepRepository | None = None) -> None:
        self.repository = repository if repository is not None else StepRepository()

    def create(self, run: Run, kind: str, name: str, *,
               step_id: str | None = None, metadata: dict | None = None) -> Step:
        identifier = step_id or new_id()
        if self.repository.exists(run, identifier):
            raise ValueError("Duplicate Step ID")
        step = Step(identifier, run.id, kind, name, self.repository.paths(run, identifier),
                    metadata=metadata or {})
        self.save(step)
        log_event(step.paths.logs, "step.created", entity_id=step.id, related_id=run.id)
        return step

    def save(self, step: Step) -> None:
        self.repository.save(step)

    def load(self, run: Run, step_id: str) -> Step:
        return self.repository.load(run, step_id)

    def list(self, run: Run) -> list[Step]:
        return self.repository.list(run)

    def start(self, step: Step) -> None:
        if step.status != StepStatus.PENDING:
            raise ValueError("Only pending Steps can start")
        step.status = StepStatus.RUNNING
        step.started_at = now()
        self.save(step)
        log_event(step.paths.logs, "step.started", entity_id=step.id)

    def _finish(self, step: Step, status: StepStatus, error: str | None = None) -> None:
        if step.status not in (StepStatus.PENDING, StepStatus.RUNNING):
            raise ValueError("Step is already terminal")
        if status == StepStatus.COMPLETED and step.status != StepStatus.RUNNING:
            raise ValueError("Only running Steps can complete")
        step.status = status
        step.ended_at = now()
        step.error = error
        self.save(step)
        log_event(step.paths.logs, f"step.{status.value}", entity_id=step.id, status=status)

    def complete(self, step: Step) -> None:
        self._finish(step, StepStatus.COMPLETED)

    def fail(self, step: Step, error: str) -> None:
        self._finish(step, StepStatus.FAILED, error)

    def interrupt(self, step: Step) -> None:
        self._finish(step, StepStatus.INTERRUPTED)

    def cancel(self, step: Step) -> None:
        self._finish(step, StepStatus.CANCELLED)

    def recover(self, run: Run) -> None:
        for step in self.list(run):
            if step.status in (StepStatus.PENDING, StepStatus.RUNNING):
                self.interrupt(step)
                log_event(step.paths.logs, "step.recovered", entity_id=step.id)


class StepEventRecorder:
    def __init__(self, manager: StepManager) -> None:
        self.manager = manager

    def record(self, run: Run, event: EngineEvent) -> None:
        if event.type == EngineEventType.TEXT_DELTA:
            return
        if event.step_id is None:
            raise ValueError("Step events require an ID")
        if event.type == EngineEventType.STEP_STARTED:
            step = self.manager.create(run, event.kind, event.name,
                                       step_id=event.step_id, metadata=event.metadata)
            self.manager.start(step)
            return
        step = self.manager.load(run, event.step_id)
        match event.type:
            case EngineEventType.STEP_COMPLETED:
                self.manager.complete(step)
            case EngineEventType.STEP_FAILED:
                self.manager.fail(step, event.error or "Step failed")
            case EngineEventType.STEP_INTERRUPTED:
                self.manager.interrupt(step)
            case EngineEventType.STEP_CANCELLED:
                self.manager.cancel(step)
            case _:
                raise ValueError("Unknown Step event")
