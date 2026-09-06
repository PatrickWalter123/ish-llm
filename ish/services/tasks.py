import asyncio
from dataclasses import dataclass, field
from copy import deepcopy

from ish.core.models import (
    MessageRole, MessageStatus, Project, Task, TaskStatus, new_id,
)
from ish.core.paths import ProjectPaths, TaskPaths
from .conversation import ConversationStore
from .deletion import remove_owned_tree
from .logging import log_event
from .storage import atomic_json, child, read_json, record


@dataclass(slots=True)
class TaskRuntime:
    """Runtime-only state, owned and scheduled exclusively by RunManager."""

    project: Project
    task: Task
    queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None
    execution: asyncio.Task[None] | None = None
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

    def __init__(self, repository: TaskRepository | None = None) -> None:
        self.repository = repository if repository is not None else TaskRepository()
        self._attached: set[tuple[str, str]] = set()

    def attach_runtime(self, task: Task) -> None:
        key = (task.project_id, task.id)
        if key in self._attached:
            raise ValueError("Task already has an attached runtime")
        self._attached.add(key)

    def detach_runtime(self, task: Task) -> None:
        self._attached.discard((task.project_id, task.id))

    def initialize(self, project: Project) -> None:
        self.repository.initialize(project)

    def create(self, project: Project, title: str, *,
               default_engine: str | None = None) -> Task:
        if project.deleted:
            raise ValueError("Project is deleted")
        self.initialize(project)
        task_id = new_id()
        task = Task(task_id, project.id, title, self.repository.paths(project, task_id),
                    default_engine=default_engine)
        self.save(task)
        log_event(task.paths.logs, "task.created", entity_id=task.id,
                  related_id=project.id)
        return task

    def save(self, task: Task) -> None:
        self.repository.save(task)

    def load(self, project: Project, task_id: str) -> Task:
        return self.repository.load(project, task_id)

    def list(self, project: Project, *, include_deleted: bool = False) -> list[Task]:
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
            self.save(current)
            log_event(current.paths.logs, "task.deleted", entity_id=current.id,
                      permanent=False)
        task.status = TaskStatus.DELETED

    def restore(self, task: Task) -> None:
        current = self.require_inactive(task)
        if current.status != TaskStatus.DELETED:
            raise ValueError("Task is not deleted")
        current.status = TaskStatus.IDLE
        self.save(current)
        task.status = TaskStatus.IDLE
        log_event(current.paths.logs, "task.restored", entity_id=current.id)

    def clone(self, source: Task, project: Project, *, title: str | None = None) -> Task:
        """Copy conversation/configuration, with no execution history or queue replay."""
        self.require_inactive(source)
        clone = self.create(project, title if title is not None else source.title,
                            default_engine=source.default_engine)
        clone.metadata = deepcopy(source.metadata)
        destination = ConversationStore(clone.paths.conversation)
        messages = ConversationStore(source.paths.conversation).list()
        # Queued inputs may have been created before prior Assistant messages.
        # Normalize turns before dropping execution links in the clone.
        input_positions = {message.run_id: index for index, message in enumerate(messages)
                           if message.role == MessageRole.USER and message.run_id}
        ordered = sorted(enumerate(messages), key=lambda item: (
            input_positions.get(item[1].run_id, item[0]),
            item[1].role == MessageRole.ASSISTANT, item[0],
        ))
        for _, message in ordered:
            status = message.status
            if status == MessageStatus.QUEUED:
                status = MessageStatus.CANCELLED
            elif status == MessageStatus.STREAMING:
                status = MessageStatus.INTERRUPTED
            destination.create(message.role, message.content, status,
                               metadata=message.metadata)
        self.save(clone)
        log_event(clone.paths.logs, "task.cloned", entity_id=clone.id,
                  related_id=source.id)
        return clone

    def require_inactive(self, task: Task) -> Task:
        if (task.project_id, task.id) in self._attached:
            raise ValueError("Task runtime is attached; shut down RunManager first")
        # Reload to avoid trusting a stale handle held by a caller.
        current = self.repository.reload(task)
        if current.status == TaskStatus.RUNNING or current.current_run_id is not None:
            raise ValueError("Task has an active Run")
        return current
