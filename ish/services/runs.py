from typing import Optional
import asyncio
from collections.abc import Callable
from copy import deepcopy

from ish.core.models import (
    Message, MessageRole, MessageStatus, Project, Run, RunStatus, StepStatus,
    Task, TaskStatus, new_id, now,
)
from ish.core.paths import RunPaths
from ish.engines.base import EngineContext, EngineEvent, EngineEventType, EngineRegistry
from .conversation import ConversationStore
from .conversation_context import ConversationContextBuilder
from .events import RunEventPublisher
from ish.components.registry import CapabilityResolver, ComponentRegistry
from .steps import StepEventRecorder, StepManager
from .storage import atomic_json, child, read_json, record
from .tasks import TaskManager, TaskRuntime
from .logging import log_event


class RunRepository:
    """Run metadata and paths beneath the owning Task's runs root."""

    def paths(self, task: Task, run_id: str) -> RunPaths:
        return RunPaths(child(task.paths.runs, run_id))

    def save(self, run: Run) -> None:
        atomic_json(run.paths.root / "run.json", record(run))
        log_event(run.paths.logs, "run.saved", entity_id=run.id, status=run.status)

    def load(self, task: Task, run_id: str) -> Run:
        paths = self.paths(task, run_id)
        data = read_json(paths.root / "run.json")
        if data["id"] != run_id or data["task_id"] != task.id:
            raise ValueError("Run ownership mismatch")
        log_event(paths.logs, "run.loaded", entity_id=run_id)
        return Run(**{**data, "status": RunStatus(data["status"]), "paths": paths})

    def list(self, task: Task) -> list[Run]:
        return sorted((self.load(task, path.parent.name)
                       for path in task.paths.runs.glob("*/run.json")),
                      key=lambda run: (run.created_at, run.id))


class RunManager:
    """Single-process, single-event-loop owner of a workspace's active Tasks."""

    def __init__(self, tasks: TaskManager, engines: EngineRegistry, *,
                 runs: Optional[RunRepository] = None,
                 steps: Optional[StepManager] = None,
                 on_event: Optional[Callable[[Run, EngineEvent], None]] = None,
                 repository: Optional[RunRepository] = None,
                 capabilities: Optional[CapabilityResolver] = None,
                 conversations: Optional[Callable[[Task], ConversationStore]] = None,
                 context_builder: Optional[ConversationContextBuilder] = None) -> None:
        if repository is not None and runs is not None:
            raise ValueError("Specify repository or runs, not both")
        self.tasks = tasks
        self.engines = engines
        self.repository = repository if repository is not None else (
            runs if runs is not None else RunRepository())
        self.steps = steps if steps is not None else StepManager()
        self.recorder = StepEventRecorder(self.steps)
        self.events = RunEventPublisher(on_event)
        self.capabilities = capabilities if capabilities is not None else ComponentRegistry()
        self.conversations = conversations if conversations is not None else tasks.conversations
        self.context_builder = context_builder if context_builder is not None else tasks.context_builder
        self._runtimes: dict[tuple[str, str], TaskRuntime] = {}
        self._closed = False

    @property
    def on_event(self) -> Optional[Callable[[Run, EngineEvent], None]]:
        return self.events.callback

    @on_event.setter
    def on_event(self, callback: Optional[Callable[[Run, EngineEvent], None]]) -> None:
        self.events.callback = callback

    @property
    def runs(self) -> RunRepository:
        """Compatibility alias for the repository attribute."""
        return self.repository

    def _runtime(self, project: Project, task: Task) -> TaskRuntime:
        if self._closed:
            raise RuntimeError("RunManager is shut down")
        project = self.tasks.require_project(project)
        if task.project_id != project.id:
            raise ValueError("Task requires its active owning Project")
        current = self.tasks.load(project, task.id)
        if task.paths.root.absolute() != current.paths.root.absolute():
            raise ValueError("Task path ownership mismatch")
        if current.status == TaskStatus.DELETED:
            raise ValueError("Task is deleted")
        key = (project.id, task.id)
        if key in self._runtimes:
            runtime = self._runtimes[key]
            if runtime.worker is not None and runtime.worker.done():
                runtime.worker.result()
                raise RuntimeError("Task worker has stopped")
            runtime.project = deepcopy(project)
            return runtime
        # Recovery and queue population are synchronous: submit cannot overtake
        # recovered messages, and duplicate start calls cannot duplicate workers.
        self.tasks.attach_runtime(current)
        try:
            self._recover(current)
            runtime = TaskRuntime(deepcopy(project), current)
            for message in self.conversations(current).list():
                if message.role == MessageRole.USER and message.status == MessageStatus.QUEUED:
                    runtime.queue.put_nowait(message.id)
        except BaseException:
            self.tasks.detach_runtime(current)
            raise
        self._runtimes[key] = runtime
        runtime.worker = asyncio.create_task(self._worker(runtime), name=f"ish-task-{task.id}")
        log_event(current.paths.logs, "runtime.started", entity_id=current.id,
                  count=runtime.queue.qsize())
        return runtime

    async def start(self, project: Project, task: Task) -> None:
        """Recover a Task, then schedule only its durable queued requests."""
        self._runtime(project, task)

    async def submit(self, project: Project, task: Task, content: str, *,
                     engine: Optional[str] = None) -> Message:
        runtime = self._runtime(project, task)
        selected = engine or runtime.task.default_engine or runtime.project.config.default_engine
        store = self.conversations(runtime.task)
        message = store.create(MessageRole.USER, content, MessageStatus.QUEUED,
                               metadata={"engine": selected})
        # There is no await between durable creation and queue insertion.
        runtime.queue.put_nowait(message.id)
        log_event(runtime.task.paths.logs, "request.queued", entity_id=message.id,
                  related_id=task.id)
        return message

    async def interrupt(self, project: Project, task: Task) -> bool:
        runtime = self._runtimes.get((project.id, task.id))
        if runtime is None or runtime.execution is None or runtime.execution.done():
            return False
        finished = runtime.finished
        runtime.execution.cancel()
        await finished.wait()
        log_event(runtime.task.paths.logs, "runtime.interrupted", entity_id=task.id)
        return True

    async def wait_idle(self, project: Project, task: Task) -> None:
        runtime = self._runtime(project, task)
        assert runtime.worker is not None
        joined = asyncio.create_task(runtime.queue.join())
        try:
            done, _ = await asyncio.wait((joined, runtime.worker),
                                         return_when=asyncio.FIRST_COMPLETED)
            if runtime.worker in done:
                runtime.worker.result()
                if not joined.done():
                    raise RuntimeError("Task worker stopped before draining its queue")
            await joined
        finally:
            joined.cancel()
            await asyncio.gather(joined, return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop accepting input, interrupt active work, and preserve the durable queue."""
        self._closed = True
        workers = []
        for runtime in self._runtimes.values():
            runtime.closed = True
            if runtime.execution is not None:
                runtime.execution.cancel()
            runtime.queue.put_nowait(None)
            if runtime.worker is not None:
                workers.append(runtime.worker)
        try:
            results = await asyncio.gather(*workers, return_exceptions=True)
        finally:
            for key, runtime in list(self._runtimes.items()):
                if runtime.worker is None or runtime.worker.done():
                    self.tasks.detach_runtime(runtime.task)
                    log_event(runtime.task.paths.logs, "runtime.stopped",
                              entity_id=runtime.task.id)
                    del self._runtimes[key]
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def _recover(self, task: Task) -> None:
        store = self.conversations(task)
        messages = {message.id: message for message in store.list()}
        for run in self.repository.list(task):
            self.steps.recover(run)
            if run.status in (RunStatus.PENDING, RunStatus.RUNNING):
                run.status = RunStatus.INTERRUPTED
                run.ended_at = now()
                self.repository.save(run)
                log_event(run.paths.logs, "run.recovered", entity_id=run.id,
                          status=run.status)
            # The Run is written before committing its input. A crash in that
            # gap must not replay even a pending Run: it is already claimed.
            message = messages.get(run.input_message_id)
            if message is not None and message.status == MessageStatus.QUEUED:
                store.bind_run(message.id, run.id)
                store.set_status(message.id, MessageStatus.COMMITTED)
                message.status = MessageStatus.COMMITTED
        for message in messages.values():
            if message.role == MessageRole.ASSISTANT and message.status == MessageStatus.STREAMING:
                store.set_status(message.id, MessageStatus.INTERRUPTED)
        task.current_run_id = None
        task.status = TaskStatus.IDLE
        self.tasks._save_runtime(task)

    def _begin(self, runtime: TaskRuntime, message: Message) -> Run:
        runtime.project = self.tasks.require_project(runtime.project)
        task = runtime.task
        store = self.conversations(task)
        run_id = new_id()
        run = Run(run_id, task.id, message.id, new_id(),
                  message.metadata.get("engine") or task.default_engine
                  or runtime.project.config.default_engine,
                  self.repository.paths(task, run_id))
        self.repository.save(run)
        store.bind_run(message.id, run.id)
        store.set_status(message.id, MessageStatus.COMMITTED)
        store.create(MessageRole.ASSISTANT, "", MessageStatus.STREAMING,
                     message_id=run.assistant_message_id, run_id=run.id)
        task.current_run_id = run.id
        task.status = TaskStatus.RUNNING
        self.tasks._save_runtime(task)
        run.status = RunStatus.RUNNING
        run.started_at = now()
        self.repository.save(run)
        log_event(run.paths.logs, "run.started", entity_id=run.id,
                  related_id=task.id, status=run.status)
        return run

    def _context(self, runtime: TaskRuntime, run: Run) -> EngineContext:
        history = self.context_builder.for_run(
            self.conversations(runtime.task).list(), run.input_message_id)
        # Copy domain state, but do not deepcopy Python handler closures or live
        # capabilities. The resolver returns a fresh registry for this Run.
        return EngineContext(deepcopy(runtime.project), deepcopy(runtime.task),
                             deepcopy(run), history,
                             self.capabilities.resolve_tools(deepcopy(runtime.project)))

    async def _consume(self, runtime: TaskRuntime, run: Run) -> None:
        engine = self.engines.resolve(run.engine)
        store = self.conversations(runtime.task)
        events = engine.execute(self._context(runtime, run))
        try:
            async for event in events:
                if event.type == EngineEventType.TEXT_DELTA:
                    store.delta(run.assistant_message_id, event.text)
                else:
                    self.recorder.record(run, event)
                self.events.publish(run, event)
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                await close()
        if any(step.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.FAILED)
               for step in self.steps.list(run)):
            raise RuntimeError("Engine ended with unfinished or failed Steps")

    def _finish(self, runtime: TaskRuntime, run: Run, status: RunStatus,
                error: Optional[str] = None) -> None:
        for step in self.steps.list(run):
            if step.status in (StepStatus.PENDING, StepStatus.RUNNING):
                if status == RunStatus.FAILED:
                    self.steps.fail(step, error or "Run failed")
                else:
                    self.steps.interrupt(step)
        store = self.conversations(runtime.task)
        store.set_status(run.assistant_message_id, MessageStatus(status.value))
        run.status = status
        run.error = error
        run.ended_at = now()
        self.repository.save(run)
        log_event(run.paths.logs, f"run.{status.value}", entity_id=run.id,
                  status=status)
        runtime.task.current_run_id = None
        runtime.task.status = TaskStatus.IDLE
        self.tasks._save_runtime(runtime.task)

    async def _worker(self, runtime: TaskRuntime) -> None:
        while not runtime.closed:
            message_id = await runtime.queue.get()
            try:
                if runtime.closed or message_id is None:
                    return
                message = self.conversations(runtime.task).get(message_id)
                if message.status != MessageStatus.QUEUED:
                    continue
                # Set up persistent state before creating the cancellable child.
                # Finalization belongs to the worker, so cancellation before the
                # child's first instruction still leaves a terminal Run.
                run = self._begin(runtime, message)
                runtime.finished = asyncio.Event()
                runtime.execution = asyncio.create_task(self._consume(runtime, run))
                status, error = RunStatus.COMPLETED, None
                try:
                    await runtime.execution
                except asyncio.CancelledError:
                    status = RunStatus.INTERRUPTED
                except Exception:
                    # Provider exception strings can contain credentials. Do not
                    # persist raw exception messages or provider response objects.
                    status, error = RunStatus.FAILED, "Engine execution failed"
                finally:
                    try:
                        self._finish(runtime, run, status, error)
                    finally:
                        runtime.execution = None
                        runtime.finished.set()
            finally:
                runtime.queue.task_done()
