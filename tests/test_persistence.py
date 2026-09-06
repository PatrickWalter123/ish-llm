import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from ish.core.models import (
    Message, MessageRole, MessageStatus, Project, ProjectConfig, Run, RunStatus,
    Step, StepStatus, Task, TaskStatus, new_id,
)
from ish.engines.base import EngineEvent, EngineEventType
from ish.services.conversation import ConversationStore
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunRepository
from ish.services.steps import StepEventRecorder, StepManager
from ish.services.storage import atomic_json, read_json
from ish.services.tasks import TaskManager


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(self.root / "projects"), self.tasks)
        self.project = self.projects.create("Project", config=ProjectConfig(model="fake-model"))
        self.task = self.tasks.create(self.project, "Task")
        self.store = ConversationStore(self.task.paths.conversation)

    def test_domain_and_config_round_trip(self) -> None:
        self.assertEqual(self.projects.load(self.project.id), self.project)
        self.assertEqual(self.tasks.load(self.project, self.task.id), self.task)
        self.assertEqual(self.tasks.list(self.project), [self.task])
        self.assertEqual(self.projects.list(), [self.project])
        for model in (Project, Task, Message, Run, Step):
            names = {item.name for item in fields(model)}
            self.assertTrue(names.isdisjoint({"queue", "worker", "execution", "lock"}))
            self.assertIn("__slots__", model.__dict__)
        with self.assertRaises(TypeError):
            ProjectConfig(api_key="must-not-be-persisted")
        config = read_json(self.project.paths.root / "project.json")["config"]
        self.assertNotIn("api_key", config)

    def test_conversation_replays_all_event_types_and_appends_deltas(self) -> None:
        user = self.store.create(MessageRole.USER, "안녕하세요", MessageStatus.QUEUED)
        run_id = new_id()
        self.store.bind_run(user.id, run_id)
        self.store.set_status(user.id, MessageStatus.COMMITTED)
        self.store.update_metadata(user.id, {"engine": "fake"})
        assistant = self.store.create(MessageRole.ASSISTANT, "", MessageStatus.STREAMING,
                                      run_id=run_id)
        prefix = self.store.path.read_bytes()
        self.store.delta(assistant.id, "Hello")
        self.store.delta(assistant.id, " 🌍")
        self.store.set_status(assistant.id, MessageStatus.COMPLETED)
        self.assertTrue(self.store.path.read_bytes().startswith(prefix))
        loaded = ConversationStore(self.store.path).list()
        self.assertEqual(loaded[0].content, "안녕하세요")
        self.assertEqual(loaded[0].run_id, run_id)
        self.assertEqual(loaded[0].metadata, {"engine": "fake"})
        self.assertEqual(loaded[0].status, MessageStatus.COMMITTED)
        self.assertEqual(loaded[1].content, "Hello 🌍")
        self.assertEqual(loaded[1].status, MessageStatus.COMPLETED)
        with self.assertRaises(ValueError):
            self.store.delta(assistant.id, "late")

    def test_incomplete_tail_survives_read_and_is_repaired_before_append(self) -> None:
        first = self.store.create(MessageRole.USER, "one", MessageStatus.QUEUED)
        prefix = self.store.path.read_bytes()
        with self.store.path.open("ab") as stream:
            stream.write(b'{"type":"message.create","message":"\xe2\x82')
        self.assertEqual(self.store.list(), [first])
        self.store.create(MessageRole.USER, "two", MessageStatus.QUEUED)
        self.assertEqual([message.content for message in self.store.list()], ["one", "two"])
        self.assertTrue(self.store.path.read_bytes().startswith(prefix))
        for line in self.store.path.read_bytes().splitlines():
            json.loads(line)

    def test_corrupt_complete_event_is_not_silently_discarded(self) -> None:
        self.store.path.write_bytes(b'not-json\n')
        with self.assertRaises(json.JSONDecodeError):
            self.store.list()

    def test_invalid_metadata_does_not_replace_valid_json(self) -> None:
        path = self.root / "metadata.json"
        atomic_json(path, {"valid": True})
        with self.assertRaises(TypeError):
            atomic_json(path, {"invalid": object()})
        self.assertEqual(read_json(path), {"valid": True})

    def test_ids_cannot_escape_owned_roots(self) -> None:
        with self.assertRaises(ValueError):
            self.projects.load("../outside")
        with self.assertRaises(ValueError):
            self.tasks.load(self.project, "../outside")

    def test_soft_delete_restore_preserves_history(self) -> None:
        user = self.store.create(MessageRole.USER, "queued", MessageStatus.QUEUED)
        self.tasks.soft_delete(self.task)
        self.assertEqual(self.tasks.list(self.project), [])
        self.assertEqual(len(self.tasks.list(self.project, include_deleted=True)), 1)
        self.tasks.restore(self.task)
        self.assertEqual(self.store.get(user.id).status, MessageStatus.QUEUED)
        self.projects.soft_delete(self.project)
        self.assertEqual(self.projects.list(), [])
        with self.assertRaises(ValueError):
            self.tasks.create(self.project, "Rejected")
        self.projects.restore(self.project)
        self.assertEqual(len(self.projects.list()), 1)

    def test_clone_copies_configuration_and_history_without_replaying_queue(self) -> None:
        run_id = new_id()
        user = self.store.create(MessageRole.USER, "done", MessageStatus.COMMITTED, run_id=run_id)
        self.store.create(MessageRole.ASSISTANT, "answer", MessageStatus.COMPLETED, run_id=run_id)
        self.store.create(MessageRole.USER, "queued", MessageStatus.QUEUED)
        cloned_project = self.projects.clone(self.project, title="Copy")
        cloned_task = self.tasks.list(cloned_project)[0]
        messages = ConversationStore(cloned_task.paths.conversation).list()
        self.assertNotEqual(cloned_task.id, self.task.id)
        self.assertEqual(cloned_task.project_id, cloned_project.id)
        self.assertIsNone(cloned_task.current_run_id)
        self.assertEqual(cloned_task.status, TaskStatus.IDLE)
        self.assertEqual(RunRepository().list(cloned_task), [])
        self.assertEqual([message.content for message in messages], ["done", "answer", "queued"])
        self.assertNotEqual(messages[0].id, user.id)
        self.assertTrue(all(message.run_id is None for message in messages))
        self.assertEqual(messages[-1].status, MessageStatus.CANCELLED)
        cloned_project.config.model = "different"
        self.assertEqual(self.projects.load(self.project.id).config.model, "fake-model")
        self.assertEqual(self.store.list()[-1].status, MessageStatus.QUEUED)

    def test_active_task_lifecycle_changes_rejected_using_persisted_state(self) -> None:
        stale_handle = self.tasks.load(self.project, self.task.id)
        self.task.status = TaskStatus.RUNNING
        self.task.current_run_id = new_id()
        self.tasks.save(self.task)
        with self.assertRaises(ValueError):
            self.tasks.soft_delete(stale_handle)
        with self.assertRaises(ValueError):
            self.tasks.clone(stale_handle, self.project)
        with self.assertRaises(ValueError):
            self.projects.clone(self.project)
        with self.assertRaises(ValueError):
            self.projects.soft_delete(self.project)

    def test_step_events_and_recovery(self) -> None:
        runs = RunRepository()
        run_id = new_id()
        run = Run(run_id, self.task.id, new_id(), new_id(), "fake", runs.paths(self.task, run_id))
        runs.save(run)
        self.assertEqual(runs.load(self.task, run.id).status, RunStatus.PENDING)
        manager = StepManager()
        recorder = StepEventRecorder(manager)
        for event_type, expected in (
            (EngineEventType.STEP_COMPLETED, StepStatus.COMPLETED),
            (EngineEventType.STEP_FAILED, StepStatus.FAILED),
            (EngineEventType.STEP_INTERRUPTED, StepStatus.INTERRUPTED),
            (EngineEventType.STEP_CANCELLED, StepStatus.CANCELLED),
        ):
            step_id = new_id()
            recorder.record(run, EngineEvent(EngineEventType.STEP_STARTED, step_id=step_id))
            recorder.record(run, EngineEvent(event_type, step_id=step_id))
            self.assertEqual(manager.load(run, step_id).status, expected)
            with self.assertRaises(ValueError):
                recorder.record(run, EngineEvent(event_type, step_id=step_id))
        pending = manager.create(run, "tool", "pending")
        running = manager.create(run, "llm", "running")
        manager.start(running)
        manager.recover(run)
        manager.recover(run)
        self.assertEqual(manager.load(run, pending.id).status, StepStatus.INTERRUPTED)
        self.assertEqual(manager.load(run, running.id).status, StepStatus.INTERRUPTED)
        self.assertEqual(len(manager.list(run)), 6)
