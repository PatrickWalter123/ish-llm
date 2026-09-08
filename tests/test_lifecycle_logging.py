from ish.compat import timeout
import asyncio
import json
import logging
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from ish.core.models import MessageRole, MessageStatus, ProjectConfig, TaskStatus
from ish.core.paths import ProjectPaths, TaskPaths
from ish.compat import is_junction
from ish.engines.base import EngineRegistry
from ish.services.conversation import ConversationStore
from ish.services.storage import remove_owned_tree
from ish.services.logging import _RaisingFileHandler, log_event
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager, TaskRuntime
from tests.support.fake_engine import FakeStreamingEngine


def records(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "service.log").read_text(
        encoding="utf-8").splitlines()]


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "projects"
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(self.root), self.tasks)
        self.project = self.projects.create("Project")
        self.task = self.tasks.create(self.project, "Task")
        self.store = ConversationStore(self.task.paths.conversation)
        self.store.create(MessageRole.USER, "retained history", MessageStatus.QUEUED)

    def test_delete_defaults_to_reversible_metadata_change(self) -> None:
        stale = deepcopy(self.task)
        self.task.metadata["newer"] = True
        self.tasks.save(self.task)
        self.tasks.delete(stale)
        self.assertEqual(self.tasks.load(self.project, stale.id).metadata, {"newer": True})
        self.assertTrue(self.task.paths.conversation.is_file())
        self.tasks.restore(stale)
        self.assertEqual(self.tasks.load(self.project, stale.id).status, TaskStatus.IDLE)
        self.projects.delete(self.project)
        self.assertEqual(self.projects.list(), [])
        self.projects.restore(self.project)
        self.assertEqual(self.store.list()[0].content, "retained history")

    def test_permanent_task_delete_removes_descendants_preserves_sibling_and_parent_log(self) -> None:
        sibling = self.tasks.create(self.project, "Sibling")
        artifact = self.task.paths.root / "runs" / "nested" / "artifact.txt"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("artifact", encoding="utf-8")
        self.tasks.delete(self.task, permanent=True)
        self.assertFalse(self.task.paths.root.exists())
        self.assertTrue(sibling.paths.root.exists())
        event = records(self.project.paths.logs)[-1]
        self.assertEqual((event["event"], event["entity_id"], event["permanent"]),
                         ("task.deleted", self.task.id, True))
        with self.assertRaises(FileNotFoundError):
            self.tasks.restore(self.task)
        self.assertFalse(self.task.paths.root.exists())

    def test_permanent_project_delete_removes_all_tasks_and_preserves_sibling(self) -> None:
        sibling = self.projects.create("Sibling")
        self.tasks.delete(self.task)
        self.projects.delete(self.project, permanent=True)
        self.assertFalse(self.project.paths.root.exists())
        self.assertTrue(sibling.paths.root.exists())
        self.assertEqual(records(self.root / "logs")[-1]["entity_id"], self.project.id)
        with self.assertRaises(FileNotFoundError):
            self.projects.restore(self.project)
        self.assertFalse(self.project.paths.root.exists())

    def test_permanent_requires_explicit_bool(self) -> None:
        for value in ("false", "true", 1, None):
            for manager, item in ((self.tasks, self.task), (self.projects, self.project)):
                with self.subTest(value=value, manager=type(manager).__name__):
                    with self.assertRaises(TypeError):
                        manager.delete(item, permanent=value)
        self.assertTrue(self.task.paths.root.exists())

    def test_failed_removal_does_not_report_success_or_mark_deleted(self) -> None:
        with patch("ish.services.storage.shutil.rmtree", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.tasks.delete(self.task, permanent=True)
        self.assertEqual(self.task.status, TaskStatus.IDLE)
        self.assertTrue(self.task.paths.conversation.exists())
        self.assertFalse(any(item["event"] == "task.deleted" and item.get("permanent")
                             for item in records(self.project.paths.logs)))

    def test_permanent_delete_rejects_persisted_active_run(self) -> None:
        self.task.status = TaskStatus.RUNNING
        self.tasks.repository.save(self.task)
        for manager, item in ((self.tasks, self.task), (self.projects, self.project)):
            with self.assertRaises(ValueError):
                manager.delete(item, permanent=True)
        self.assertTrue(self.task.paths.root.exists())

    def test_forged_paths_and_ownership_are_rejected_without_removing_files(self) -> None:
        forged_project = deepcopy(self.project)
        forged_project.paths = ProjectPaths(self.root.parent)
        with self.assertRaises(ValueError):
            self.projects.delete(forged_project, permanent=True)
        forged_task = deepcopy(self.task)
        forged_task.paths = TaskPaths(self.project.paths.root)
        with self.assertRaises(ValueError):
            self.tasks.repository.delete(forged_task)
        forged_task = deepcopy(self.task)
        forged_task.project_id = "a" * 32
        with self.assertRaises(ValueError):
            self.tasks.repository.delete(forged_task)
        with self.assertRaises(ValueError):
            remove_owned_tree(self.root, self.root.parent, self.project.id)
        self.assertTrue(self.task.paths.conversation.is_file())

    def test_linked_descendant_preflight_rejects_before_removal(self) -> None:
        # Model a detected symlink without requiring Windows symlink privileges.
        linked = self.task.paths.root / "external"
        linked.mkdir()
        marker = linked / "marker"
        marker.write_text("must survive", encoding="utf-8")
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == linked or original(path)):
            with self.assertRaises(ValueError):
                self.tasks.delete(self.task, permanent=True)
        self.assertTrue(marker.exists())
        self.assertTrue(self.task.paths.conversation.exists())

    def test_junction_target_preflight_rejects_before_removal(self) -> None:
        redirected = lambda path: path == self.task.paths.root or is_junction(path)
        with patch("ish.services.storage.is_junction", side_effect=redirected), \
                patch("ish.services.logging.is_junction", side_effect=redirected):
            with self.assertWarns(RuntimeWarning):
                with self.assertRaises(ValueError):
                    self.tasks.delete(self.task, permanent=True)
        self.assertTrue(self.task.paths.conversation.exists())

    def test_logging_failure_does_not_fail_metadata_save(self) -> None:
        self.task.title = "Changed"
        with patch("ish.services.logging._RaisingFileHandler", side_effect=PermissionError("private")):
            with self.assertWarnsRegex(RuntimeWarning, "could not write an operational log"):
                self.tasks.save(self.task)
        self.assertEqual(self.tasks.load(self.project, self.task.id).title, "Changed")

    def test_log_rotation_and_no_duplicate_global_handlers(self) -> None:
        root_handlers = list(logging.getLogger().handlers)
        def small_handler(filename, **kwargs):
            return _RaisingFileHandler(filename, **{**kwargs, "maxBytes": 220})
        with patch("ish.services.logging._RaisingFileHandler", side_effect=small_handler):
            for _ in range(12):
                log_event(self.task.paths.logs, "task.loaded", entity_id=self.task.id)
        files = list(self.task.paths.logs.glob("service.log*"))
        self.assertEqual(len(files), 4)
        self.assertEqual(logging.getLogger().handlers, root_handlers)
        self.assertTrue(all(json.loads(line)["event"] == "task.loaded"
                            for path in files for line in path.read_text(encoding="utf-8").splitlines()))

    def test_logging_never_recreates_deleted_domain(self) -> None:
        self.tasks.delete(self.task, permanent=True)
        log_event(self.task.paths.logs, "task.loaded", entity_id=self.task.id)
        self.assertFalse(self.task.paths.root.exists())


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(self.root), self.tasks)
        self.project = self.projects.create("private-project-title",
                                            config=ProjectConfig(default_engine="fake"))
        self.task = self.tasks.create(self.project, "private-task-title")
        self.registry = EngineRegistry()
        self.registry.register("fake", FakeStreamingEngine(chunks=("private-answer",)))
        self.manager = RunManager(self.tasks, self.registry, task=self.task)
        self.addAsyncCleanup(self.manager.shutdown)

    async def test_queued_and_idle_attached_tasks_cannot_be_deleted_or_cloned(self) -> None:
        await self.manager.submit("private-prompt")
        for phase in ("queued", "idle"):
            if phase == "idle":
                await self.manager.wait_idle()
            for permanent in (False, True):
                for manager, item in ((self.tasks, self.task), (self.projects, self.project)):
                    with self.assertRaisesRegex(ValueError, "runtime is attached"):
                        manager.delete(item, permanent=permanent)
            with self.assertRaises(ValueError):
                self.tasks.clone(self.task, self.project)
        await self.manager.shutdown()
        self.tasks.delete(self.task, permanent=True)
        self.assertFalse(self.task.paths.root.exists())

    async def test_second_manager_cannot_recover_an_attached_task(self) -> None:
        await self.manager.start()
        other = RunManager(self.tasks, self.registry, task=self.task)
        self.addAsyncCleanup(other.shutdown)
        with self.assertRaisesRegex(ValueError, "already has an attached runtime"):
            await other.start()
        await self.manager.shutdown()
        await other.start()
        # Repeated shutdown on the old owner must not release the new owner.
        await self.manager.shutdown()
        with self.assertRaises(ValueError):
            self.tasks.delete(self.task, permanent=True)

    async def test_recovery_failure_releases_runtime_attachment(self) -> None:
        with patch.object(self.manager, "_recover", side_effect=ValueError("broken")):
            with self.assertRaises(ValueError):
                await self.manager.start()
        self.tasks.delete(self.task)

    async def test_failed_and_interrupted_runs_have_terminal_domain_logs(self) -> None:
        self.registry.register("broken", FakeStreamingEngine(fail_after=0))
        await self.manager.submit("private-prompt", engine="broken")
        await self.manager.wait_idle()
        failed_run, = self.manager.repository.list(self.task)
        failed_step, = self.manager.steps.list(failed_run)
        self.assertIn("run.failed", [row["event"] for row in records(failed_run.paths.logs)])
        self.assertIn("step.failed", [row["event"] for row in records(failed_step.paths.logs)])

        self.registry.register("blocked", FakeStreamingEngine(gate=asyncio.Event()))
        await self.manager.submit("private-prompt", engine="blocked")
        async with timeout(10):
            while not any(message.status == MessageStatus.STREAMING and message.content
                          for message in ConversationStore(self.task.paths.conversation).list()):
                await asyncio.sleep(0.001)
        self.assertTrue(await self.manager.interrupt())
        await self.manager.wait_idle()
        interrupted = self.manager.repository.list(self.task)[-1]
        step, = self.manager.steps.list(interrupted)
        self.assertIn("run.interrupted", [row["event"] for row in records(interrupted.paths.logs)])
        self.assertIn("step.interrupted", [row["event"] for row in records(step.paths.logs)])

    async def test_each_domain_records_lifecycle_without_conversation_or_metadata(self) -> None:
        self.task.metadata = {"Authorization": "private-authorization"}
        self.tasks.save(self.task)
        await self.manager.submit("private-prompt")
        await self.manager.wait_idle()
        run, = self.manager.repository.list(self.task)
        step, = self.manager.steps.list(run)
        for model, event in ((self.project, "project.created"), (self.task, "request.queued"),
                             (run, "run.completed"), (step, "step.completed")):
            self.assertIn(event, [item["event"] for item in records(model.paths.logs)])
        all_logs = "\n".join(path.read_text(encoding="utf-8")
                             for path in self.root.rglob("service.log*"))
        for secret in ("private-project-title", "private-task-title", "private-authorization",
                       "private-prompt", "private-answer"):
            self.assertNotIn(secret, all_logs)
        self.assertEqual(TaskRuntime.__module__, "ish.services.tasks")
