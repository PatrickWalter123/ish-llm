"""Read Run-owned execution observations from Project or Task scope."""

from typing import Optional, Protocol, Union

from ish.core.models import Project, Run, RunStatus, Task, TaskStatus
from ish.core.results import ExecutionResult
from .locking import WorkspaceOwnership, workspace_locked


class TaskReader(Protocol):
    @property
    def ownership(self) -> WorkspaceOwnership: ...
    def _owner(self, task: Task, *, allow_deleted: bool = False) -> Project: ...
    def require_project(self, project: Project, *, allow_deleted: bool = False) -> Project: ...
    def load(self, project: Project, task_id: str) -> Task: ...
    def list(self, project: Project, *, include_deleted: bool = False) -> list[Task]: ...


class RunReader(Protocol):
    def load(self, task: Task, run_id: str) -> Run: ...
    def list(self, task: Task) -> list[Run]: ...


class RunResultQuery:
    """No result files or cache: run.json is the sole execution source of truth.

    Project lookups traverse Tasks. Pass a Task for a direct Run lookup. Custom
    Run repositories can be injected; RunManager uses its configured repository.
    """

    def __init__(self, tasks: TaskReader, runs: Optional[RunReader] = None) -> None:
        if runs is None:
            # Runtime import keeps the query protocol independent of orchestration.
            from .runs import RunRepository
            runs = RunRepository()
        self.tasks = tasks
        self.runs = runs

    @property
    def ownership(self):
        return self.tasks.ownership

    def _scope(self, owner: Union[Project, Task], include_deleted: bool):
        if isinstance(owner, Task):
            project = self.tasks._owner(owner, allow_deleted=True)
            task = self.tasks.load(project, owner.id)
            if task.status == TaskStatus.DELETED and not include_deleted:
                return project, []
            return project, [task]
        project = self.tasks.require_project(owner, allow_deleted=True)
        return project, self.tasks.list(project, include_deleted=include_deleted)

    @workspace_locked
    def load(self, owner: Union[Project, Task], run_id: str) -> ExecutionResult:
        project, tasks = self._scope(owner, include_deleted=True)
        for task in tasks:
            try:
                run = self.runs.load(task, run_id)
            except FileNotFoundError:
                continue
            return ExecutionResult.from_run(project.id, run)
        raise FileNotFoundError("Run does not exist in this scope")

    @workspace_locked
    def list(self, owner: Union[Project, Task], *, include_deleted: bool = False,
             include_running: bool = False) -> list[ExecutionResult]:
        project, tasks = self._scope(owner, include_deleted)
        results = [ExecutionResult.from_run(project.id, run)
                   for task in tasks for run in self.runs.list(task)
                   if include_running or run.status not in (RunStatus.PENDING, RunStatus.RUNNING)]
        return sorted(results, key=lambda result: (result.ended_at or result.started_at or "", result.run_id))
