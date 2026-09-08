from typing import Optional, Union
import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict

from ish.core.models import (
    Message, MessageRole, MessageStatus, Project, Run, RunStatus, StepStatus,
    Task, TaskStatus, new_id, now,
)
from ish.core.paths import RunPaths
from ish.engines.base import EngineContext, EngineEvent, EngineEventType, EngineRegistry
from .conversation import ConversationStore
from .context import ConversationContextBuilder
from ish.components.registry import ComponentRegistry
from ish.components.tools.resolver import CapabilityResolver, ComponentToolResolver
from .steps import StepEventRecorder, StepManager
from .storage import (
    StorageIO, atomic_json, child, drain_on_cancel, read_json, record,
)
from .tasks import TaskManager, TaskRuntime
from .logging import log_event
from .results import RunResultQuery
from ish.core.results import CompletionResult
from ish.compat import dataclass, StrEnum


class RunRepository:
    """Run metadata and paths beneath the owning Task's runs root."""

    def paths(self, task: Task, run_id: str) -> RunPaths:
        return RunPaths(child(task.paths.runs, run_id))

    def save(self, run: Run) -> None:
        if run.status not in (RunStatus.PENDING, RunStatus.RUNNING) and "completions" in run.metadata:
            run.metadata["completions"] = [asdict(CompletionResult.for_run(data, run))
                                           for data in run.metadata["completions"]]
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


class RunEventPublisher:
    def __init__(self, callback: Optional[Callable[[Run, EngineEvent], None]] = None) -> None:
        self.callback = callback

    def publish(self, run: Run, event: EngineEvent) -> None:
        # UI callbacks must be synchronous and nonblocking. Persistence has
        # already succeeded; a display exception must not fail the Engine.
        if self.callback is not None:
            try:
                self.callback(deepcopy(run), deepcopy(event))
            except Exception:
                log_event(run.paths.logs, "observer.failed", entity_id=run.id)


class RunErrorCode(StrEnum):
    ENGINE_NOT_REGISTERED = "engine_not_registered"
    COMPONENT_NOT_REGISTERED = "component_not_registered"
    CAPABILITY_FAILED = "capability_failed"
    ENGINE_FAILED = "engine_failed"
    INTERRUPTED = "interrupted"
    PROCESS_RESTART = "process_restart"


class RunRequestError(ValueError):
    """An actionable request rejection before queue admission."""

    def __init__(self, code: RunErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class RunEventType(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class RunEvent:
    type: RunEventType
    run: Run


class RunManager:
    """One Task-bound scheduler; separate instances execute separate Tasks."""

    def __init__(self, tasks: TaskManager, engines: EngineRegistry, *, task: Task,
                 runs: Optional[RunRepository] = None,
                 steps: Optional[StepManager] = None,
                 on_event: Optional[Callable[[Run, EngineEvent], None]] = None,
                 on_run_event: Optional[Callable[[RunEvent], None]] = None,
                 repository: Optional[RunRepository] = None,
                 capabilities: Optional[Union[CapabilityResolver, ComponentRegistry]] = None,
                 conversations: Optional[Callable[[Task], ConversationStore]] = None,
                 context_builder: Optional[ConversationContextBuilder] = None) -> None:
        if repository is not None and runs is not None:
            raise ValueError("Specify repository or runs, not both")
        if not isinstance(task, Task):
            raise TypeError("RunManager requires a Task")
        self._task = deepcopy(task)
        self.tasks = tasks
        self.engines = engines
        self.repository = repository if repository is not None else (
            runs if runs is not None else RunRepository())
        self.steps = steps if steps is not None else StepManager()
        self.recorder = StepEventRecorder(self.steps)
        self.results = RunResultQuery(tasks, self.repository)
        self.events = RunEventPublisher(on_event)
        self.on_run_event = on_run_event
        if capabilities is None:
            capabilities = ComponentRegistry()
        self.capabilities = (ComponentToolResolver(capabilities)
                             if isinstance(capabilities, ComponentRegistry) else capabilities)
        self.conversations = conversations if conversations is not None else tasks.conversations
        self.context_builder = context_builder if context_builder is not None else tasks.context_builder
        self._active: Optional[TaskRuntime] = None
        self._closed = False
        self._control = None
        self._io = StorageIO(tasks.ownership)
        self._store_instance: Optional[ConversationStore] = None

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

    def _control_lock(self):
        if self._control is None:
            self._control = asyncio.Lock()
        return self._control

    @property
    def task(self) -> Task:
        return deepcopy(self._task)

    def _publish_run(self, run: Run) -> None:
        if self.on_run_event is not None:
            event = RunEvent(RunEventType(run.status.value if run.status != RunStatus.RUNNING else "started"),
                             deepcopy(run))
            try:
                self.on_run_event(event)
            except Exception:
                log_event(run.paths.logs, "observer.failed", entity_id=run.id)

    def _store(self, task: Task) -> ConversationStore:
        if (task.project_id, task.id) != (self._task.project_id, self._task.id):
            raise ValueError("Task does not match this RunManager")
        if self._store_instance is None:
            self._store_instance = self.conversations(task)
        return self._store_instance

    def _prepare(self):
        project = self.tasks._owner(self._task)
        current = self.tasks.load(project, self._task.id)
        if current.status == TaskStatus.DELETED:
            raise ValueError("Task is deleted")
        return project, current

    def _validate_request(self, engine: Optional[str]) -> str:
        project, task = self._prepare()
        selected = engine if engine is not None else task.default_engine or project.config.default_engine
        if not isinstance(selected, str) or selected not in self.engines.names():
            raise RunRequestError(RunErrorCode.ENGINE_NOT_REGISTERED, "Requested Engine is not registered")
        if isinstance(self.capabilities, ComponentToolResolver):
            try:
                self.capabilities.components.validate(project.components)
            except ValueError as error:
                raise RunRequestError(RunErrorCode.COMPONENT_NOT_REGISTERED, str(error)) from error
        return selected

    async def _runtime(self) -> TaskRuntime:
        # Caller holds _control. OS ownership is retained before recovery; the
        # worker and all pending storage finish before that ownership is released.
        if self._closed:
            raise RuntimeError("RunManager is shut down")
        project, current = await self._io.run(self._prepare)
        if self._active is not None:
            runtime = self._active
            if runtime.worker is not None and runtime.worker.done():
                runtime.worker.result()
                raise RuntimeError("Task worker has stopped")
            return runtime

        def recover():
            # Revalidate under the same ownership scope as attachment/recovery.
            owner, fresh = self._prepare()
            self.tasks.attach_runtime(fresh)
            try:
                recovered = self._recover(fresh)
                queued = [message.id for message in self._store(fresh).list()
                          if message.role == MessageRole.USER
                          and message.status == MessageStatus.QUEUED]
                log_event(fresh.paths.logs, "runtime.started", entity_id=fresh.id,
                          count=len(queued))
                return owner, fresh, queued, recovered
            except BaseException:
                self.tasks.detach_runtime(fresh)
                raise

        project, current, queued, recovered = await self._io.run(recover)
        for run in recovered:
            self._publish_run(run)
        runtime = TaskRuntime(deepcopy(project), current)
        for message_id in queued:
            runtime.queue.put_nowait(message_id)
        self._active = runtime
        runtime.worker = asyncio.create_task(self._worker(runtime), name=f"ish-task-{current.id}")
        return runtime

    async def _start(self) -> TaskRuntime:
        async with self._control_lock():
            return await self._runtime()

    async def start(self) -> None:
        """Recover a Task, then schedule only its durable queued requests."""
        await drain_on_cancel(self._start())

    async def submit(self, content: str, *,
                     engine: Optional[str] = None) -> Message:
        return await drain_on_cancel(self._submit(content, engine))

    async def _submit(self, content: str,
                      engine: Optional[str]) -> Message:
        async with self._control_lock():
            if self._closed:
                raise RuntimeError("RunManager is shut down")
            selected = await self._io.run(self._validate_request, engine)
            runtime = await self._runtime()
            def persist():
                message = self._store(runtime.task).create(
                    MessageRole.USER, content, MessageStatus.QUEUED,
                    metadata={"engine": selected})
                log_event(runtime.task.paths.logs, "request.queued", entity_id=message.id,
                          related_id=runtime.task.id)
                return message

            message = await self._io.run(persist)
            # Accepted submission is shielded through this insertion, including
            # caller cancellation and concurrent shutdown. QUEUED is fsynced first.
            runtime.queue.put_nowait(message.id)
            return message

    async def interrupt(self) -> bool:
        runtime = self._active
        if runtime is None:
            return False
        if not runtime.preparing and (runtime.execution is None or runtime.execution.done()):
            return False
        finished = runtime.finished
        runtime.interrupt_requested = True
        if runtime.execution is not None:
            runtime.execution.cancel()
        await finished.wait()
        await self._io.run(log_event, runtime.task.paths.logs, "runtime.interrupted", entity_id=runtime.task.id)
        return True

    async def wait_idle(self) -> None:
        runtime = await drain_on_cancel(self._start())
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
        await drain_on_cancel(self._shutdown())

    async def _shutdown(self) -> None:
        async with self._control_lock():
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        """Stop accepting input, interrupt active work, and preserve the durable queue."""
        self._closed = True
        runtime = self._active
        if runtime is None:
            return
        runtime.closed = True
        if runtime.execution is not None:
            runtime.execution.cancel()
        runtime.queue.put_nowait(None)
        try:
            if runtime.worker is not None:
                await runtime.worker
        finally:
            if runtime.worker is None or runtime.worker.done():
                await self._io.run(self._detach, runtime.task)
                self._store_instance = None
                self._active = None

    def _detach(self, task: Task) -> None:
        log_event(task.paths.logs, "runtime.stopped", entity_id=task.id)
        self.tasks.detach_runtime(task)

    def _recover(self, task: Task) -> list[Run]:
        recovered = []
        store = self._store(task)
        messages = {message.id: message for message in store.list()}
        for run in self.repository.list(task):
            self.steps.recover(run)
            if run.status in (RunStatus.PENDING, RunStatus.RUNNING):
                run.status = RunStatus.INTERRUPTED
                run.error_code = RunErrorCode.PROCESS_RESTART
                run.ended_at = now()
                self.repository.save(run)
                recovered.append(deepcopy(run))
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
        return recovered

    def _begin(self, runtime: TaskRuntime, message: Message) -> Run:
        runtime.project = self.tasks.require_project(runtime.project)
        task = runtime.task
        store = self._store(task)
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
            self._store(runtime.task).list(), run.input_message_id)
        # Copy domain state, but do not deepcopy Python handler closures or live
        # capabilities. The resolver returns a fresh registry for this Run.
        return EngineContext(deepcopy(runtime.project), deepcopy(runtime.task),
                             deepcopy(run), history,
                             self.capabilities.resolve_tools(deepcopy(runtime.project)))

    async def _consume(self, runtime: TaskRuntime, run: Run) -> None:
        try:
            engine = self.engines.resolve(run.engine)
        except KeyError as error:
            raise RunRequestError(RunErrorCode.ENGINE_NOT_REGISTERED, "Requested Engine is not registered") from error
        store = self._store(runtime.task)
        try:
            context = await self._io.run(self._context, runtime, run)
        except Exception as error:
            raise RunRequestError(RunErrorCode.CAPABILITY_FAILED, str(error)) from error
        events = engine.execute(context)
        try:
            async for event in events:
                if event.type == EngineEventType.TEXT_DELTA:
                    await self._io.run(store.delta, run.assistant_message_id, event.text)
                elif event.type == EngineEventType.COMPLETION:
                    await self._io.run(self._record_completion, run, event)
                else:
                    await self._io.run(self.recorder.record, run, event)
                self.events.publish(run, event)
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                await close()
        if any(step.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.FAILED)
               for step in await self._io.run(self.steps.list, run)):
            raise RuntimeError("Engine ended with unfinished or failed Steps")

    def _record_completion(self, run: Run, event: EngineEvent) -> None:
        if event.completion is None:
            raise ValueError("Completion event requires a result")
        result = asdict(event.completion)
        child(run.paths.state, result["id"])
        entries = run.metadata.setdefault("completions", [])
        for index, previous in enumerate(entries):
            if previous["id"] == result["id"]:
                entries[index] = result
                break
        else:
            entries.append(result)
        self.repository.save(run)

    def _finish(self, runtime: TaskRuntime, run: Run, status: RunStatus,
                error: Optional[str] = None, error_code: Optional[str] = None) -> None:
        for step in self.steps.list(run):
            if step.status in (StepStatus.PENDING, StepStatus.RUNNING):
                if status == RunStatus.FAILED:
                    self.steps.fail(step, error or "Run failed")
                else:
                    self.steps.interrupt(step)
        store = self._store(runtime.task)
        store.set_status(run.assistant_message_id, MessageStatus(status.value))
        run.status = status
        run.error = error
        run.error_code = error_code
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
                runtime.preparing = True
                runtime.interrupt_requested = False
                runtime.finished = asyncio.Event()
                message = await self._io.run(self._store(runtime.task).get, message_id)
                if message.status != MessageStatus.QUEUED:
                    continue
                # Set up persistent state before creating the cancellable child.
                # Finalization belongs to the worker, so cancellation before the
                # child's first instruction still leaves a terminal Run.
                if runtime.closed:
                    return
                run = await self._io.run(self._begin, runtime, message)
                self._publish_run(run)
                runtime.preparing = False
                runtime.execution = asyncio.create_task(self._consume(runtime, run))
                if runtime.closed or runtime.interrupt_requested:
                    runtime.execution.cancel()
                status, error, error_code = RunStatus.COMPLETED, None, None
                try:
                    await runtime.execution
                except asyncio.CancelledError:
                    status = RunStatus.INTERRUPTED
                    error_code = RunErrorCode.INTERRUPTED
                except Exception as failure:
                    status, error = RunStatus.FAILED, str(failure)
                    error_code = failure.code if isinstance(failure, RunRequestError) else RunErrorCode.ENGINE_FAILED
                finally:
                    try:
                        await self._io.run(self._finish, runtime, run, status, error, error_code)
                        self._publish_run(run)
                    finally:
                        runtime.execution = None
                        runtime.finished.set()
            finally:
                runtime.preparing = False
                runtime.finished.set()
                runtime.queue.task_done()
