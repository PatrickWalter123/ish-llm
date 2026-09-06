import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ish.core.models import MessageRole, MessageStatus, ProjectConfig, RunStatus
from ish.engines.base import EngineRegistry
from ish.services.conversation import ConversationStore
from ish.services.io import StorageIO
from ish.services.locking import WorkspaceBusyError, WorkspaceOwnership
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager
from tests.support.fake_engine import FakeStreamingEngine


class LockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_nested_ownership_releases_on_exception_and_keeps_lock_file(self):
        first, second = WorkspaceOwnership(self.root), WorkspaceOwnership(self.root)
        with self.assertRaisesRegex(ValueError, "test"):
            with first.scope():
                with first.scope():
                    with self.assertRaises(WorkspaceBusyError):
                        second.retain()
                    raise ValueError("test")
        with second.scope():
            self.assertTrue((self.root / ".ish.lock").exists())

    def test_real_process_crash_releases_os_lock_without_deleting_file(self):
        code = textwrap.dedent('''
            import os, sys
            from pathlib import Path
            from ish.services.locking import WorkspaceOwnership
            owner = WorkspaceOwnership(Path(sys.argv[1]))
            owner.retain()
            print("ready", flush=True)
            sys.stdin.readline()
            os._exit(23)
        ''')
        child = subprocess.Popen([sys.executable, "-c", code, str(self.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            owner = WorkspaceOwnership(self.root)
            with self.assertRaises(WorkspaceBusyError):
                owner.retain()
            child.communicate("crash\n", timeout=10)
            self.assertEqual(child.returncode, 23)
            with owner.scope():
                self.assertTrue(owner.path.exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_existing_lock_file_does_not_imply_ownership(self):
        (self.root / ".ish.lock").write_text("stale PID information")
        with WorkspaceOwnership(self.root).scope():
            pass


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "conversation.jsonl"
        self.store = ConversationStore(self.path)
        self.message = self.store.create(MessageRole.ASSISTANT, "", MessageStatus.STREAMING)

    def test_streaming_decodes_only_new_events_and_returns_isolated_snapshots(self):
        with patch("ish.services.conversation.json.loads", wraps=json.loads) as decode:
            for _ in range(100):
                self.store.delta(self.message.id, "한")
            # Each written event is decoded once; no growing-history replay.
            self.assertEqual(decode.call_count, 100)
            snapshot = self.store.get(self.message.id)
            snapshot.content = "tampered"
            self.store.list()[0].metadata["tampered"] = True
            self.assertEqual(decode.call_count, 100)
        self.assertEqual(self.store.get(self.message.id).content, "한" * 100)
        self.assertEqual(self.store.get(self.message.id).metadata, {})
        self.assertEqual(ConversationStore(self.path).list(), self.store.list())

    def test_another_store_append_is_incrementally_visible(self):
        reader = ConversationStore(self.path)
        reader.list()
        self.store.delta(self.message.id, "new")
        with patch("ish.services.conversation.json.loads", wraps=json.loads) as decode:
            self.assertEqual(reader.get(self.message.id).content, "new")
            self.assertEqual(decode.call_count, 1)

    def test_replacement_truncation_and_incomplete_tail_invalidate_cache(self):
        original = self.path.read_bytes()
        self.store.delta(self.message.id, "old")
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(original)
        os.replace(replacement, self.path)
        self.assertEqual(self.store.get(self.message.id).content, "")
        with self.path.open("ab") as stream:
            stream.write(b'{"incomplete":')
        self.assertEqual(self.store.get(self.message.id).content, "")
        self.store.delta(self.message.id, "repaired")
        self.assertEqual(ConversationStore(self.path).get(self.message.id).content, "repaired")
        self.path.write_bytes(b"")
        self.assertEqual(self.store.list(), [])

    def test_corrupt_complete_event_repeatedly_raises(self):
        with self.path.open("ab") as stream:
            stream.write(b'not json\n')
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.store.list()

    def test_fsync_failure_does_not_acknowledge_or_cache_unwritten_delta(self):
        with patch("ish.services.conversation.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.store.delta(self.message.id, "uncertain")
        # A write may have reached the OS before fsync failed; retrying it would
        # duplicate text. Re-read the actual log instead of trusting stale cache.
        self.assertEqual(self.store.list(), ConversationStore(self.path).list())


class RuntimeIOTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(self.root), self.tasks)
        self.project = self.projects.create("Project", config=ProjectConfig(default_engine="fake"))
        self.task = self.tasks.create(self.project, "Task")
        self.engines = EngineRegistry()
        self.engine = FakeStreamingEngine()
        self.engines.register("fake", self.engine)
        self.manager = RunManager(self.tasks, self.engines)
        self.addAsyncCleanup(self.manager.shutdown)

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), 5)

    async def test_other_process_cannot_recover_or_mutate_attached_workspace(self):
        await self.manager.start(self.project, self.task)
        code = textwrap.dedent('''
            import asyncio, sys
            from pathlib import Path
            from ish.services.projects import ProjectManager, ProjectRepository
            from ish.services.tasks import TaskManager
            from ish.services.runs import RunManager
            from ish.services.locking import WorkspaceBusyError
            from ish.engines.base import EngineRegistry
            from ish.core.models import Project, Task
            from ish.core.paths import TaskPaths
            root = Path(sys.argv[1])
            tasks = TaskManager()
            projects = ProjectManager(ProjectRepository(root), tasks)
            project = Project(sys.argv[2], "stale", projects.repository.paths(sys.argv[2]))
            task = Task(sys.argv[3], project.id, "stale", TaskPaths(project.paths.tasks / sys.argv[3]))
            async def main():
                manager = RunManager(tasks, EngineRegistry())
                try:
                    for operation in (lambda: projects.create("bad"),
                                      lambda: projects.delete(project, permanent=True),
                                      lambda: tasks.delete(task, permanent=True),
                                      lambda: tasks.save(task)):
                        try:
                            operation()
                        except WorkspaceBusyError:
                            continue
                        raise AssertionError("mutation was allowed")
                    try:
                        await manager.start(project, task)
                    except WorkspaceBusyError:
                        print("blocked")
                    else:
                        raise AssertionError("recovery was allowed")
                finally:
                    await manager.shutdown()
            asyncio.run(main())
        ''')
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-c", code, str(self.root), self.project.id, self.task.id],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "blocked")
        await self.manager.shutdown()
        self.assertEqual(ProjectRepository(self.root).load(self.project.id).title, "Project")

    async def test_shared_repository_cannot_attach_same_task_through_other_task_manager(self):
        await self.manager.start(self.project, self.task)
        other_tasks = TaskManager(project_access=self.projects.access)
        other = RunManager(other_tasks, self.engines)
        self.addAsyncCleanup(other.shutdown)
        with self.assertRaises(ValueError):
            await other.start(self.project, self.task)
        with self.assertRaises(ValueError):
            other_tasks.delete(self.task)

    async def test_slow_storage_keeps_loop_responsive_and_cancellation_drains_write(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        io = StorageIO(self.projects.ownership)
        calls = []

        def slow():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release missing")
            calls.append("persisted")

        pending = asyncio.create_task(io.run(slow))
        await self.until(entered.is_set)
        pending.cancel()
        pending.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(pending.done())
        with self.assertRaises(WorkspaceBusyError):
            WorkspaceOwnership(self.root).retain()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(calls, ["persisted"])
        with WorkspaceOwnership(self.root).scope():
            pass

    async def test_cancelled_submission_is_persisted_and_scheduled_once(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowStore(ConversationStore):
            def create(store, role, *args, **kwargs):
                if role == MessageRole.USER:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test release missing")
                return super().create(role, *args, **kwargs)

        self.manager.conversations = lambda task: SlowStore(task.paths.conversation)
        pending = asyncio.create_task(self.manager.submit(self.project, self.task, "once"))
        await self.until(entered.is_set)
        pending.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(pending.done())
        self.assertEqual(len(self.engine.contexts), 0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await self.manager.wait_idle(self.project, self.task)
        self.assertEqual(len(self.engine.contexts), 1)
        self.assertEqual(len(self.manager.runs.list(self.task)), 1)

    async def test_interrupt_waits_for_delta_and_shutdown_keeps_ownership_until_drain(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowStore(ConversationStore):
            def delta(store, *args):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test release missing")
                return super().delta(*args)

        self.manager.conversations = lambda task: SlowStore(task.paths.conversation)
        await self.manager.submit(self.project, self.task, "slow")
        await self.until(entered.is_set)
        shutdown = asyncio.create_task(self.manager.shutdown())
        await asyncio.sleep(0.02)
        self.assertFalse(shutdown.done())
        with self.assertRaises(WorkspaceBusyError):
            WorkspaceOwnership(self.root).retain()
        release.set()
        await shutdown
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.INTERRUPTED)
        self.assertEqual(ConversationStore(self.task.paths.conversation).get(
            run.assistant_message_id).content, "Hello")
        with WorkspaceOwnership(self.root).scope():
            pass

    async def test_concurrent_submissions_are_serial_and_preserve_all_inputs(self):
        await asyncio.gather(*(self.manager.submit(self.project, self.task, str(i))
                               for i in range(10)))
        await self.manager.wait_idle(self.project, self.task)
        self.assertEqual([context.messages[-1].content for context in self.engine.contexts],
                         list(map(str, range(10))))
        self.assertEqual(self.engine.max_active, 1)
