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
                 repository: Optional[RunRepository] = None) -> None:
        if repository is not None and runs is not None:
            raise ValueError("Specify repository or runs, not both")
        self.tasks = tasks
        self.engines = engines
        self.repository = repository if repository is not None else (
            runs if runs is not None else RunRepository())
        self.steps = steps if steps is not None else StepManager()
        self.recorder = StepEventRecorder(self.steps)
        # Synchronous observer, invoked after persistence. Keep it nonblocking;
        # observer errors fail the execution rather than silently losing output.
        self.on_event = on_event
        self._runtimes: dict[tuple[str, str], TaskRuntime] = {}
        self._closed = False

    @property
    def runs(self) -> RunRepository:
        """Compatibility alias for the repository attribute."""
        return self.repository

    def _runtime(self, project: Project, task: Task) -> TaskRuntime:
        if self._closed:
            raise RuntimeError("RunManager is shut down")
        if project.deleted or task.project_id != project.id:
            raise ValueError("Task requires its active owning Project")
        key = (project.id, task.id)
        if key in self._runtimes:
            runtime = self._runtimes[key]
            if runtime.worker is not None and runtime.worker.done():
                runtime.worker.result()
                raise RuntimeError("Task worker has stopped")
            return runtime
        current = self.tasks.load(project, task.id)
        if current.status == TaskStatus.DELETED:
            raise ValueError("Task is deleted")
        # Recovery and queue population are synchronous: submit cannot overtake
        # recovered messages, and duplicate start calls cannot duplicate workers.
        self.tasks.attach_runtime(current)
        try:
            self._recover(current)
            runtime = TaskRuntime(deepcopy(project), current)
            for message in ConversationStore(current.paths.conversation).list():
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
        store = ConversationStore(runtime.task.paths.conversation)
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
        store = ConversationStore(task.paths.conversation)
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
        self.tasks.save(task)

    def _begin(self, runtime: TaskRuntime, message: Message) -> Run:
        task = runtime.task
        store = ConversationStore(task.paths.conversation)
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
        self.tasks.save(task)
        run.status = RunStatus.RUNNING
        run.started_at = now()
        self.repository.save(run)
        log_event(run.paths.logs, "run.started", entity_id=run.id,
                  related_id=task.id, status=run.status)
        return run

    def _context(self, runtime: TaskRuntime, run: Run) -> EngineContext:
        messages = ConversationStore(runtime.task.paths.conversation).list()
        answers = {message.run_id: message for message in messages
                   if message.role == MessageRole.ASSISTANT and message.run_id
                   and message.status in (MessageStatus.COMPLETED, MessageStatus.INTERRUPTED,
                                          MessageStatus.FAILED)}
        history = []
        for message in messages:
            if message.id == run.input_message_id:
                history.append(message)
                break
            if message.role == MessageRole.USER and message.status == MessageStatus.COMMITTED:
                history.append(message)
                if message.run_id in answers:
                    history.append(answers[message.run_id])
            elif message.role != MessageRole.USER and message.run_id is None and message.status in (
                MessageStatus.COMPLETED, MessageStatus.INTERRUPTED, MessageStatus.FAILED,
            ):
                history.append(message)
        # Engines receive snapshots, so engine code cannot mutate service-owned
        # domain state. Pair by Run because answers may be appended after queues.
        return deepcopy(EngineContext(runtime.project, runtime.task, run, tuple(history)))

    async def _consume(self, runtime: TaskRuntime, run: Run) -> None:
        engine = self.engines.resolve(run.engine)
        store = ConversationStore(runtime.task.paths.conversation)
        events = engine.execute(self._context(runtime, run))
        try:
            async for event in events:
                if event.type == EngineEventType.TEXT_DELTA:
                    store.delta(run.assistant_message_id, event.text)
                else:
                    self.recorder.record(run, event)
                if self.on_event is not None:
                    self.on_event(deepcopy(run), deepcopy(event))
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
        store = ConversationStore(runtime.task.paths.conversation)
        store.set_status(run.assistant_message_id, MessageStatus(status.value))
        run.status = status
        run.error = error
        run.ended_at = now()
        self.repository.save(run)
        log_event(run.paths.logs, f"run.{status.value}", entity_id=run.id,
                  status=status)
        runtime.task.current_run_id = None
        runtime.task.status = TaskStatus.IDLE
        self.tasks.save(runtime.task)

    async def _worker(self, runtime: TaskRuntime) -> None:
        while not runtime.closed:
            message_id = await runtime.queue.get()
            try:
                if runtime.closed or message_id is None:
                    return
                message = ConversationStore(runtime.task.paths.conversation).get(message_id)
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
