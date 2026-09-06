import asyncio
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from ish.core.models import (
    MessageRole, MessageStatus, Run, RunStatus, StepStatus, TaskStatus, new_id,
)
from ish.engines.base import EngineEvent, EngineEventType, EngineRegistry
from ish.engines.fake import FakeStreamingEngine
from ish.services.conversation import ConversationStore
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.run_manager import RunManager
from ish.services.tasks import TaskManager


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(self.root / "projects"), self.tasks)
        self.project = self.projects.create("Project")
        self.task = self.tasks.create(self.project, "Task")
        self.store = ConversationStore(self.task.paths.conversation)
        self.engine = FakeStreamingEngine()
        self.registry = EngineRegistry()
        self.registry.register("fake", self.engine)
        self.manager = self.make_manager()

    def make_manager(self) -> RunManager:
        manager = RunManager(self.tasks, self.registry)
        self.addAsyncCleanup(manager.shutdown)
        return manager

    async def idle(self, task=None, manager=None) -> None:
        await asyncio.wait_for((manager or self.manager).wait_idle(
            self.project, task or self.task), timeout=10)

    async def until(self, predicate) -> None:
        async with asyncio.timeout(10):
            while not predicate():
                await asyncio.sleep(0.001)

    async def test_single_request_is_durable_before_execution(self) -> None:
        request = await self.manager.submit(self.project, self.task, "hello")
        self.assertEqual(self.store.get(request.id).status, MessageStatus.QUEUED)
        self.assertEqual(self.manager.runs.list(self.task), [])
        await self.idle()
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.input_message_id, request.id)
        self.assertEqual(self.store.get(request.id).run_id, run.id)
        assistant = self.store.get(run.assistant_message_id)
        self.assertEqual(assistant.content, "Hello world")
        self.assertEqual(assistant.status, MessageStatus.COMPLETED)
        step, = self.manager.steps.list(run)
        self.assertEqual(step.status, StepStatus.COMPLETED)
        self.assertIsNotNone(step.started_at)
        self.assertIsNotNone(step.ended_at)
        loaded = self.tasks.load(self.project, self.task.id)
        self.assertEqual(loaded.status, TaskStatus.IDLE)
        self.assertIsNone(loaded.current_run_id)

    async def test_streaming_and_second_input_remains_queued(self) -> None:
        self.engine.gate = asyncio.Event()
        await self.manager.submit(self.project, self.task, "first")
        await self.until(lambda: any(message.content == "Hello" for message in self.store.list()))
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.RUNNING)
        self.assertEqual(self.store.get(run.assistant_message_id).status, MessageStatus.STREAMING)
        self.assertEqual(self.manager.steps.list(run)[0].status, StepStatus.RUNNING)
        second = await self.manager.submit(self.project, self.task, "second")
        prefix = self.store.path.read_bytes()
        self.assertEqual(self.store.get(second.id).status, MessageStatus.QUEUED)
        self.assertEqual(len(self.engine.contexts), 1)
        self.assertEqual([message.content for message in self.engine.contexts[0].messages], ["first"])
        self.engine.gate.set()
        await self.idle()
        self.assertTrue(self.store.path.read_bytes().startswith(prefix))
        self.assertEqual([message.content for message in self.engine.contexts[1].messages],
                         ["first", "Hello world", "second"])
        self.assertEqual(self.engine.max_active, 1)

    async def test_burst_queue_context_pairs_answers_with_prior_inputs(self) -> None:
        for content in ("first", "second", "third"):
            await self.manager.submit(self.project, self.task, content)
        await self.idle()
        self.assertEqual([[message.content for message in context.messages]
                          for context in self.engine.contexts], [
            ["first"], ["first", "Hello world", "second"],
            ["first", "Hello world", "second", "Hello world", "third"],
        ])
        self.assertEqual(self.engine.max_active, 1)
        self.assertEqual(len(self.manager.runs.list(self.task)), 3)

    async def test_interrupt_preserves_queue_and_worker_continues(self) -> None:
        self.engine.gate = asyncio.Event()
        await self.manager.submit(self.project, self.task, "first")
        await self.until(lambda: self.engine.active == 1)
        second = await self.manager.submit(self.project, self.task, "second")
        third = await self.manager.submit(self.project, self.task, "third")
        self.assertTrue(await self.manager.interrupt(self.project, self.task))
        first_run = self.manager.runs.list(self.task)[0]
        self.assertEqual(first_run.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.store.get(first_run.assistant_message_id).content, "Hello")
        self.assertEqual(self.store.get(first_run.assistant_message_id).status, MessageStatus.INTERRUPTED)
        self.assertEqual(self.manager.steps.list(first_run)[0].status, StepStatus.INTERRUPTED)
        self.assertEqual(self.store.get(third.id).status, MessageStatus.QUEUED)
        self.engine.gate.set()
        await self.idle()
        self.assertEqual(self.store.get(second.id).status, MessageStatus.COMMITTED)
        self.assertEqual([run.status for run in self.manager.runs.list(self.task)],
                         [RunStatus.INTERRUPTED, RunStatus.COMPLETED, RunStatus.COMPLETED])
        self.assertEqual(self.engine.cancelled, 1)
        self.assertFalse(await self.manager.interrupt(self.project, self.task))

    async def test_cancel_before_engine_first_instruction_finalizes_run(self) -> None:
        await self.manager.submit(self.project, self.task, "first")
        # The worker begins and schedules its child behind this test continuation.
        await asyncio.sleep(0)
        self.assertEqual(len(self.engine.contexts), 0)
        self.assertTrue(await self.manager.interrupt(self.project, self.task))
        await self.idle()
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.store.get(run.assistant_message_id).status, MessageStatus.INTERRUPTED)
        self.assertIsNone(self.tasks.load(self.project, self.task.id).current_run_id)

    async def test_independent_tasks_really_run_concurrently(self) -> None:
        self.engine.gate = asyncio.Event()
        other = self.tasks.create(self.project, "Other")
        await self.manager.submit(self.project, self.task, "first")
        await self.manager.submit(self.project, other, "other")
        await self.until(lambda: self.engine.active == 2)
        self.assertEqual(self.engine.max_active, 2)
        self.assertTrue(await self.manager.interrupt(self.project, self.task))
        other_run, = self.manager.runs.list(other)
        self.assertEqual(other_run.status, RunStatus.RUNNING)
        self.engine.gate.set()
        await asyncio.gather(self.idle(), self.idle(other))
        self.assertEqual(self.manager.runs.list(other)[0].status, RunStatus.COMPLETED)

    async def test_engine_failure_preserves_partial_text_and_processes_next_request(self) -> None:
        self.engine.fail_after = 1
        self.engine.fail_inputs = frozenset({"fail"})
        await self.manager.submit(self.project, self.task, "fail")
        await self.manager.submit(self.project, self.task, "success")
        await self.idle()
        failed, completed = self.manager.runs.list(self.task)
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(self.store.get(failed.assistant_message_id).content, "Hello")
        self.assertEqual(self.store.get(failed.assistant_message_id).status, MessageStatus.FAILED)
        self.assertEqual(self.manager.steps.list(failed)[0].status, StepStatus.FAILED)
        self.assertEqual(self.engine.active, 0)

    async def test_unknown_engine_fails_one_run_without_dropping_queue(self) -> None:
        await self.manager.submit(self.project, self.task, "bad", engine="missing")
        await self.manager.submit(self.project, self.task, "good")
        await self.idle()
        self.assertEqual([run.status for run in self.manager.runs.list(self.task)],
                         [RunStatus.FAILED, RunStatus.COMPLETED])

    async def test_engine_overrides_are_durable(self) -> None:
        alternate = FakeStreamingEngine(("alternate",))
        self.registry.register("alternate", alternate)
        self.task.default_engine = "alternate"
        self.tasks.save(self.task)
        await self.manager.submit(self.project, self.task, "task-default")
        await self.manager.submit(self.project, self.task, "override", engine="fake")
        await self.idle()
        self.assertEqual([run.engine for run in self.manager.runs.list(self.task)], ["alternate", "fake"])
        self.assertEqual(len(alternate.contexts), 1)

    async def test_shutdown_preserves_queue_for_new_manager(self) -> None:
        self.engine.gate = asyncio.Event()
        await self.manager.submit(self.project, self.task, "active")
        await self.until(lambda: self.engine.active == 1)
        queued = await self.manager.submit(self.project, self.task, "queued")
        await self.manager.shutdown()
        self.assertEqual(self.engine.active, 0)
        self.assertEqual(self.store.get(queued.id).status, MessageStatus.QUEUED)
        self.assertEqual(self.manager.runs.list(self.task)[0].status, RunStatus.INTERRUPTED)
        before = self.store.path.read_bytes()
        with self.assertRaises(RuntimeError):
            await self.manager.submit(self.project, self.task, "rejected")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.engine.gate.set()
        recovered = self.make_manager()
        await recovered.start(self.project, self.task)
        await self.idle(manager=recovered)
        self.assertEqual([context.messages[-1].content for context in self.engine.contexts], ["active", "queued"])

    async def test_shutdown_before_worker_starts_keeps_all_requests_queued(self) -> None:
        await self.manager.submit(self.project, self.task, "queued")
        await self.manager.shutdown()
        self.assertEqual(self.manager.runs.list(self.task), [])
        self.assertEqual(self.store.list()[0].status, MessageStatus.QUEUED)
        recovered = self.make_manager()
        await recovered.start(self.project, self.task)
        await self.idle(manager=recovered)
        self.assertEqual(len(self.engine.contexts), 1)

    async def test_recovery_interrupts_stale_runs_steps_and_orphan_streams(self) -> None:
        stale_runs = []
        for status in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.COMPLETED):
            message = self.store.create(MessageRole.USER, f"old-{status}", MessageStatus.COMMITTED)
            run_id = new_id()
            assistant = self.store.create(MessageRole.ASSISTANT, "partial", MessageStatus.STREAMING,
                                          run_id=run_id)
            run = Run(run_id, self.task.id, message.id, assistant.id, "fake",
                      self.manager.runs.paths(self.task, run_id), status=status)
            self.manager.runs.save(run)
            self.store.bind_run(message.id, run.id)
            self.manager.steps.create(run, "tool", "pending")
            step = self.manager.steps.create(run, "llm", "running")
            self.manager.steps.start(step)
            stale_runs.append(run)
        orphan = self.store.create(MessageRole.ASSISTANT, "orphan", MessageStatus.STREAMING)
        self.task.status = TaskStatus.RUNNING
        self.task.current_run_id = stale_runs[1].id
        self.tasks.save(self.task)
        queued = self.store.create(MessageRole.USER, "recover-me", MessageStatus.QUEUED)
        await self.manager.start(self.project, self.task)
        await self.manager.start(self.project, self.task)
        await self.idle()
        self.assertEqual(len(self.engine.contexts), 1)
        self.assertEqual(self.engine.contexts[0].messages[-1].id, queued.id)
        for run in stale_runs:
            expected = RunStatus.COMPLETED if run.status == RunStatus.COMPLETED else RunStatus.INTERRUPTED
            self.assertEqual(self.manager.runs.load(self.task, run.id).status, expected)
            self.assertTrue(all(step.status == StepStatus.INTERRUPTED
                                for step in self.manager.steps.list(run)))
            self.assertEqual(self.store.get(run.assistant_message_id).status, MessageStatus.INTERRUPTED)
        self.assertEqual(self.store.get(orphan.id).status, MessageStatus.INTERRUPTED)

    async def test_crash_between_run_creation_and_commit_does_not_replay_claimed_input(self) -> None:
        claimed = self.store.create(MessageRole.USER, "claimed", MessageStatus.QUEUED)
        run_id = new_id()
        run = Run(run_id, self.task.id, claimed.id, new_id(), "fake",
                  self.manager.runs.paths(self.task, run_id))
        self.manager.runs.save(run)
        self.store.create(MessageRole.USER, "future", MessageStatus.QUEUED)
        await self.manager.start(self.project, self.task)
        await self.idle()
        self.assertEqual([context.messages[-1].content for context in self.engine.contexts], ["future"])
        self.assertEqual(self.store.get(claimed.id).status, MessageStatus.COMMITTED)
        self.assertEqual(self.store.get(claimed.id).run_id, run.id)
        self.assertEqual(self.manager.runs.load(self.task, run.id).status, RunStatus.INTERRUPTED)
        await self.manager.shutdown()
        recovered = self.make_manager()
        await recovered.start(self.project, self.task)
        await self.idle(manager=recovered)
        self.assertEqual(len(self.engine.contexts), 1)

    async def test_real_process_restart_preserves_partial_output_and_queued_input(self) -> None:
        code = textwrap.dedent('''
            import asyncio, os, sys
            from pathlib import Path
            from ish.engines.base import EngineRegistry
            from ish.engines.fake import FakeStreamingEngine
            from ish.services.projects import ProjectManager, ProjectRepository
            from ish.services.tasks import TaskManager
            from ish.services.run_manager import RunManager
            async def main():
                tasks = TaskManager()
                projects = ProjectManager(ProjectRepository(Path(sys.argv[1])), tasks)
                project = projects.load(sys.argv[2])
                task = tasks.load(project, sys.argv[3])
                engine = FakeStreamingEngine(gate=asyncio.Event())
                registry = EngineRegistry()
                registry.register("fake", engine)
                manager = RunManager(tasks, registry)
                await manager.submit(project, task, "crashed")
                while not engine.active:
                    await asyncio.sleep(0)
                await manager.submit(project, task, "survived")
                os._exit(23)
            asyncio.run(main())
        ''')
        result = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", code, str(self.root / "projects"),
                             self.project.id, self.task.id],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 23, result.stderr)
        stale, = self.manager.runs.list(self.task)
        self.assertEqual(stale.status, RunStatus.RUNNING)
        self.assertEqual(self.store.get(stale.assistant_message_id).content, "Hello")
        await self.manager.start(self.project, self.task)
        await self.idle()
        self.assertEqual([context.messages[-1].content for context in self.engine.contexts], ["survived"])
        self.assertEqual(self.manager.runs.load(self.task, stale.id).status, RunStatus.INTERRUPTED)
        self.assertEqual(self.manager.steps.list(stale)[0].status, StepStatus.INTERRUPTED)
        self.assertEqual(self.store.get(stale.assistant_message_id).status, MessageStatus.INTERRUPTED)

    async def test_provider_error_text_and_objects_are_not_persisted(self) -> None:
        class FailingEngine:
            async def execute(self, context):
                yield EngineEvent(EngineEventType.STEP_STARTED, step_id=new_id())
                raise RuntimeError("Authorization: Bearer private-provider-token")
        self.registry.register("provider-error", FailingEngine())
        await self.manager.submit(self.project, self.task, "request", engine="provider-error")
        await self.idle()
        for path in self.project.paths.root.rglob("*.json*"):
            self.assertNotIn("private-provider-token", path.read_text(encoding="utf-8"))
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(self.manager.steps.list(run)[0].status, StepStatus.FAILED)

    async def test_unfinished_step_fails_run_and_closes_generator(self) -> None:
        closed = asyncio.Event()
        class IncompleteEngine:
            async def execute(self, context):
                try:
                    yield EngineEvent(EngineEventType.STEP_STARTED, step_id=new_id())
                    yield EngineEvent(EngineEventType.TEXT_DELTA, text="partial")
                finally:
                    closed.set()
        self.registry.register("incomplete", IncompleteEngine())
        await self.manager.submit(self.project, self.task, "request", engine="incomplete")
        await self.idle()
        self.assertTrue(closed.is_set())
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(self.manager.steps.list(run)[0].status, StepStatus.FAILED)

    async def test_engine_receives_snapshot_not_mutable_service_state(self) -> None:
        class MutatingEngine:
            async def execute(self, context):
                context.project.title = "mutated"
                context.task.title = "mutated"
                context.run.engine = "mutated"
                context.messages[-1].content = "mutated"
                yield EngineEvent(EngineEventType.TEXT_DELTA, text="answer")
        self.registry.register("mutating", MutatingEngine())
        request = await self.manager.submit(self.project, self.task, "original", engine="mutating")
        await self.idle()
        self.assertEqual(self.store.get(request.id).content, "original")
        self.assertEqual(self.tasks.load(self.project, self.task.id).title, "Task")
        self.assertEqual(self.manager.runs.list(self.task)[0].engine, "mutating")

    async def test_deleted_task_and_wrong_project_are_rejected(self) -> None:
        self.tasks.soft_delete(self.task)
        with self.assertRaises(ValueError):
            await self.manager.submit(self.project, self.task, "rejected")
        self.assertEqual(self.store.list(), [])
        self.tasks.restore(self.task)
        other = self.projects.create("Other")
        with self.assertRaises(ValueError):
            await self.manager.submit(other, self.task, "rejected")

    async def test_empty_response_and_failure_before_first_delta(self) -> None:
        self.registry.register("empty", FakeStreamingEngine(()))
        self.registry.register("early-failure", FakeStreamingEngine(fail_after=0))
        await self.manager.submit(self.project, self.task, "empty", engine="empty")
        await self.manager.submit(self.project, self.task, "fail", engine="early-failure")
        await self.idle()
        completed, failed = self.manager.runs.list(self.task)
        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(failed.status, RunStatus.FAILED)
        self.assertEqual(self.store.get(completed.assistant_message_id).content, "")
        self.assertEqual(self.store.get(failed.assistant_message_id).content, "")

    async def test_plain_async_iterator_engine_is_supported(self) -> None:
        class Events:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        class IteratorEngine:
            def execute(self, context):
                return Events()

        self.registry.register("iterator", IteratorEngine())
        await self.manager.submit(self.project, self.task, "request", engine="iterator")
        await self.idle()
        self.assertEqual(self.manager.runs.list(self.task)[0].status, RunStatus.COMPLETED)

    async def test_cloned_burst_history_preserves_turn_order(self) -> None:
        await self.manager.submit(self.project, self.task, "first")
        await self.manager.submit(self.project, self.task, "second")
        await self.idle()
        await self.manager.shutdown()
        clone = self.tasks.clone(self.tasks.load(self.project, self.task.id), self.project)
        recovered = self.make_manager()
        await recovered.submit(self.project, clone, "third")
        await self.idle(task=clone, manager=recovered)
        self.assertEqual([message.content for message in self.engine.contexts[-1].messages],
                         ["first", "Hello world", "second", "Hello world", "third"])

    async def test_wait_idle_during_shutdown_does_not_hang_on_preserved_queue(self) -> None:
        self.engine.gate = asyncio.Event()
        await self.manager.submit(self.project, self.task, "active")
        await self.until(lambda: self.engine.active == 1)
        await self.manager.submit(self.project, self.task, "queued")
        waiting = asyncio.create_task(self.manager.wait_idle(self.project, self.task))
        await asyncio.sleep(0)
        await self.manager.shutdown()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            await asyncio.wait_for(waiting, timeout=1)
