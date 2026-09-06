from typing import Optional
import asyncio
from dataclasses import field
from ish.compat import dataclass
from copy import deepcopy
from collections.abc import Callable

from ish.core.models import Project, Task, TaskStatus, new_id
from ish.core.paths import ProjectPaths, TaskPaths
from .conversation import ConversationStore, conversation_store
from .conversation_context import ConversationContextBuilder
from .access import ProjectAccess
from .deletion import remove_owned_tree
from .logging import log_event
from .storage import atomic_json, child, read_json, record


@dataclass(slots=True)
class TaskRuntime:
    """Runtime-only state, owned and scheduled exclusively by RunManager."""

    project: Project
    task: Task
    queue: asyncio.Queue[Optional[str]] = field(default_factory=asyncio.Queue)
    worker: Optional[asyncio.Task[None]] = None
    execution: Optional[asyncio.Task[None]] = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False


class TaskRepository:
    """Task metadata and paths beneath the owning Project's tasks root."""

    def paths(self, project: Project, task_id: str) -> TaskPaths:
        return TaskPaths(child(project.paths.tasks, task_id))

    def initialize(self, project: Project) -> None:
        project.paths.tasks.mkdir(parents=True, exist_ok=True)
        log_event(project.paths.logs, "tasks.initialized", entity_id=project.id)

    def save(self, task: Task) -> None:
        atomic_json(task.paths.root / "task.json", record(task))
        log_event(task.paths.logs, "task.saved", entity_id=task.id, status=task.status)

    def load(self, project: Project, task_id: str) -> Task:
        return self._load(self.paths(project, task_id), task_id, project.id)

    def reload(self, task: Task) -> Task:
        return self._load(task.paths, task.id, task.project_id)

    def _load(self, paths: TaskPaths, task_id: str, project_id: str) -> Task:
        data = read_json(paths.root / "task.json")
        if data["id"] != task_id or data["project_id"] != project_id:
            raise ValueError("Task ownership mismatch")
        log_event(paths.logs, "task.loaded", entity_id=task_id)
        return Task(**{**data, "status": TaskStatus(data["status"]), "paths": paths})

    def delete(self, task: Task) -> None:
        """Remove the Task subtree only after validating layout and ownership."""
        root = task.paths.root
        if (root.name != task.id or root.parent.name != "tasks"
                or root.parent.parent.name != task.project_id):
            raise ValueError("Task path ownership mismatch")
        self.reload(task)
        remove_owned_tree(root.parent, root, task.id)
        log_event(ProjectPaths(root.parent.parent).logs, "task.deleted",
                  entity_id=task.id, related_id=task.project_id, permanent=True)

    def list(self, project: Project, *, include_deleted: bool = False) -> list[Task]:
        tasks = [self.load(project, path.parent.name)
                 for path in project.paths.tasks.glob("*/task.json")]
        return sorted((task for task in tasks
                       if include_deleted or task.status != TaskStatus.DELETED),
                      key=lambda task: (task.created_at, task.id))


class TaskManager:
    """Task lifecycle only. Call lifecycle mutations while runtime is detached."""

    def __init__(self, repository: Optional[TaskRepository] = None, *,
                 project_access: Optional[ProjectAccess] = None,
                 conversations: Callable[[Task], ConversationStore] = conversation_store,
                 context_builder: Optional[ConversationContextBuilder] = None) -> None:
        self.repository = repository if repository is not None else TaskRepository()
        self._attached: set[tuple[str, str]] = set()
        self.project_access = project_access
        self.conversations = conversations
        self.context_builder = context_builder if context_builder is not None else ConversationContextBuilder()

    def bind_project_access(self, access: ProjectAccess) -> None:
        if self.project_access is not None and self.project_access.repository is not access.repository:
            raise ValueError("TaskManager is already bound to a Project repository")
        self.project_access = access

    def require_project(self, project: Project, *, allow_deleted: bool = False) -> Project:
        if self.project_access is None:
            raise RuntimeError("Bind TaskManager through ProjectManager or inject project_access")
        return self.project_access.require(project, allow_deleted=allow_deleted)

    def _owner(self, task: Task, *, allow_deleted: bool = False) -> Project:
        if self.project_access is None:
            raise RuntimeError("TaskManager needs project_access")
        project = self.project_access.load(task.project_id, allow_deleted=allow_deleted)
        expected = self.repository.paths(project, task.id)
        if task.paths.root.absolute() != expected.root.absolute():
            raise ValueError("Task path ownership mismatch")
        return project

    def attach_runtime(self, task: Task) -> None:
        self._owner(task)
        key = (task.project_id, task.id)
        if key in self._attached:
            raise ValueError("Task already has an attached runtime")
        self._attached.add(key)

    def detach_runtime(self, task: Task) -> None:
        self._attached.discard((task.project_id, task.id))

    def initialize(self, project: Project) -> None:
        project = self.require_project(project)
        self.repository.initialize(project)

    def create(self, project: Project, title: str, *,
               default_engine: Optional[str] = None) -> Task:
        project = self.require_project(project)
        self.initialize(project)
        task_id = new_id()
        task = Task(task_id, project.id, title, self.repository.paths(project, task_id),
                    default_engine=default_engine)
        self.repository.save(task)
        log_event(task.paths.logs, "task.created", entity_id=task.id,
                  related_id=project.id)
        return task

    def save(self, task: Task) -> None:
        self._owner(task)
        current = self.require_inactive(task)
        if current.status == TaskStatus.DELETED:
            raise ValueError("Task is deleted")
        if task.status != current.status or task.current_run_id != current.current_run_id:
            raise ValueError("Task execution state is managed by RunManager")
        current.title = task.title
        current.default_engine = task.default_engine
        current.metadata = deepcopy(task.metadata)
        self.repository.save(current)

    def _save_runtime(self, task: Task) -> None:
        """Internal RunManager state transitions; public save edits configuration only."""
        self._owner(task)
        if (task.project_id, task.id) not in self._attached:
            raise ValueError("Runtime state requires an attached Task")
        if self.repository.reload(task).status == TaskStatus.DELETED:
            raise ValueError("Task is deleted")
        self.repository.save(task)

    def load(self, project: Project, task_id: str) -> Task:
        project = self.require_project(project, allow_deleted=True)
        return self.repository.load(project, task_id)

    def list(self, project: Project, *, include_deleted: bool = False) -> list[Task]:
        project = self.require_project(project, allow_deleted=True)
        return self.repository.list(project, include_deleted=include_deleted)

    def delete(self, task: Task, *, permanent: bool = False) -> None:
        """Mark deleted by default; permanent=True removes history and artifacts."""
        if type(permanent) is not bool:
            raise TypeError("permanent must be a bool")
        current = self.require_inactive(task)
        if permanent:
            self.repository.delete(current)
        else:
            current.status = TaskStatus.DELETED
            self.repository.save(current)
            log_event(current.paths.logs, "task.deleted", entity_id=current.id,
                      permanent=False)
        task.status = TaskStatus.DELETED

    def restore(self, task: Task) -> None:
        self._owner(task)
        current = self.require_inactive(task)
        if current.status != TaskStatus.DELETED:
            raise ValueError("Task is not deleted")
        current.status = TaskStatus.IDLE
        self.repository.save(current)
        task.status = TaskStatus.IDLE
        log_event(current.paths.logs, "task.restored", entity_id=current.id)

    def clone(self, source: Task, project: Project, *, title: Optional[str] = None) -> Task:
        """Copy conversation/configuration, with no execution history or queue replay."""
        self._owner(source)
        source = self.require_inactive(source)
        if source.status == TaskStatus.DELETED:
            raise ValueError("Task is deleted")
        clone = self.create(project, title if title is not None else source.title,
                            default_engine=source.default_engine)
        clone.metadata = deepcopy(source.metadata)
        destination = self.conversations(clone)
        messages = self.context_builder.for_clone(self.conversations(source).list())
        for message in messages:
            destination.create(message.role, message.content, message.status,
                               metadata=message.metadata)
        self.save(clone)
        log_event(clone.paths.logs, "task.cloned", entity_id=clone.id,
                  related_id=source.id)
        return clone

    def require_inactive(self, task: Task) -> Task:
        self._owner(task, allow_deleted=True)
        if (task.project_id, task.id) in self._attached:
            raise ValueError("Task runtime is attached; shut down RunManager first")
        # Reload to avoid trusting a stale handle held by a caller.
        current = self.repository.reload(task)
        if current.status == TaskStatus.RUNNING or current.current_run_id is not None:
            raise ValueError("Task has an active Run")
        return current
