from ish.core.models import Run, RunStatus, Task
from ish.core.paths import RunPaths
from .storage import atomic_json, child, read_json, record


class RunRepository:
    def paths(self, task: Task, run_id: str) -> RunPaths:
        return RunPaths(child(task.paths.runs, run_id))

    def save(self, run: Run) -> None:
        atomic_json(run.paths.root / "run.json", record(run))

    def load(self, task: Task, run_id: str) -> Run:
        paths = self.paths(task, run_id)
        data = read_json(paths.root / "run.json")
        if data["id"] != run_id or data["task_id"] != task.id:
            raise ValueError("Run ownership mismatch")
        return Run(**{**data, "status": RunStatus(data["status"]), "paths": paths})

    def list(self, task: Task) -> list[Run]:
        return sorted((self.load(task, path.parent.name)
                       for path in task.paths.runs.glob("*/run.json")),
                      key=lambda run: (run.created_at, run.id))
