from copy import deepcopy

from ish.core.models import (
    MessageRole, MessageStatus, Project, Task, TaskStatus, new_id,
)
from ish.core.paths import TaskPaths
from .conversation import ConversationStore
from .storage import atomic_json, child, read_json, record


class TaskManager:
    """Task lifecycle only. Call lifecycle mutations while runtime is detached."""

    def initialize(self, project: Project) -> None:
        project.paths.tasks.mkdir(parents=True, exist_ok=True)

    def create(self, project: Project, title: str, *,
               default_engine: str | None = None) -> Task:
        if project.deleted:
            raise ValueError("Project is deleted")
        self.initialize(project)
        task_id = new_id()
        task = Task(task_id, project.id, title,
                    TaskPaths(child(project.paths.tasks, task_id)),
                    default_engine=default_engine)
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        atomic_json(task.paths.root / "task.json", record(task))

    def load(self, project: Project, task_id: str) -> Task:
        paths = TaskPaths(child(project.paths.tasks, task_id))
        data = read_json(paths.root / "task.json")
        if data["id"] != task_id or data["project_id"] != project.id:
            raise ValueError("Task ownership mismatch")
        return Task(**{**data, "status": TaskStatus(data["status"]), "paths": paths})

    def list(self, project: Project, *, include_deleted: bool = False) -> list[Task]:
        tasks = [self.load(project, path.parent.name)
                 for path in project.paths.tasks.glob("*/task.json")]
        return sorted((task for task in tasks
                       if include_deleted or task.status != TaskStatus.DELETED),
                      key=lambda task: (task.created_at, task.id))

    def soft_delete(self, task: Task) -> None:
        self.require_inactive(task)
        task.status = TaskStatus.DELETED
        self.save(task)

    def restore(self, task: Task) -> None:
        if task.status != TaskStatus.DELETED:
            raise ValueError("Task is not deleted")
        task.status = TaskStatus.IDLE
        self.save(task)

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
        return clone

    @staticmethod
    def require_inactive(task: Task) -> None:
        # Reload to avoid trusting a stale handle held by a caller.
        data = read_json(task.paths.root / "task.json")
        if data["status"] == TaskStatus.RUNNING or data["current_run_id"] is not None:
            raise ValueError("Task has an active Run")
