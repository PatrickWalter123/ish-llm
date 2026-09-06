import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from ish.core.models import Run, StepStatus, TaskStatus, new_id
from ish.engines.base import EngineRegistry
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager, RunRepository
from ish.services.steps import StepManager, StepRepository
from ish.services.storage import atomic_json, read_json
from ish.services.tasks import TaskManager, TaskRepository


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task_repository = TaskRepository()
        self.tasks = TaskManager(repository=self.task_repository)
        self.projects = ProjectManager(ProjectRepository(self.root), self.tasks)
        self.project = self.projects.create("Project")
        self.task = self.tasks.create(self.project, "Task")
        self.runs = RunRepository()
        run_id = new_id()
        self.run = Run(run_id, self.task.id, new_id(), new_id(), "fake",
                       self.runs.paths(self.task, run_id))
        self.runs.save(self.run)
        self.steps = StepManager(repository=StepRepository())
        self.step = self.steps.create(self.run, "llm", "Completion")

    def test_all_metadata_repositories_round_trip_without_runtime_paths(self) -> None:
        cases = (
            (self.project, "project.json", lambda: self.projects.repository.load(self.project.id)),
            (self.task, "task.json", lambda: self.task_repository.load(self.project, self.task.id)),
            (self.run, "run.json", lambda: self.runs.load(self.task, self.run.id)),
            (self.step, "step.json", lambda: self.steps.repository.load(self.run, self.step.id)),
        )
        for model, filename, load in cases:
            with self.subTest(filename=filename):
                self.assertEqual(load(), model)
                data = read_json(model.paths.root / filename)
                self.assertNotIn("paths", data)
                self.assertNotIn("queue", data)
                self.assertEqual(data["id"], model.id)

    def test_repositories_reject_wrong_id_or_parent(self) -> None:
        cases = (
            (self.project, "project.json", ("id",), lambda: self.projects.load(self.project.id)),
            (self.task, "task.json", ("id", "project_id"),
             lambda: self.task_repository.load(self.project, self.task.id)),
            (self.run, "run.json", ("id", "task_id"), lambda: self.runs.load(self.task, self.run.id)),
            (self.step, "step.json", ("id", "run_id"),
             lambda: self.steps.repository.load(self.run, self.step.id)),
        )
        for model, filename, keys, load in cases:
            path = model.paths.root / filename
            original = read_json(path)
            for key in keys:
                with self.subTest(filename=filename, key=key):
                    atomic_json(path, {**original, key: new_id()})
                    with self.assertRaises(ValueError):
                        load()
                    atomic_json(path, original)

    def test_task_repository_lists_filters_and_reloads(self) -> None:
        stale = deepcopy(self.task)
        self.task.metadata = {"saved": True}
        self.tasks.save(self.task)
        self.assertEqual(self.task_repository.reload(stale).metadata, {"saved": True})
        other = self.tasks.create(self.project, "Deleted task")
        self.tasks.delete(other)
        self.assertEqual(self.task_repository.list(self.project), [self.task])
        self.assertEqual(len(self.task_repository.list(self.project, include_deleted=True)), 2)

    def test_task_manager_lifecycle_uses_injected_repository_without_json(self) -> None:
        repository = Mock(spec=TaskRepository)
        repository.paths.side_effect = TaskRepository().paths
        stored = {}
        repository.save.side_effect = lambda task: stored.update({task.id: deepcopy(task)})
        repository.reload.side_effect = lambda task: deepcopy(stored[task.id])
        repository.load.side_effect = lambda project, task_id: deepcopy(stored[task_id])
        repository.list.side_effect = lambda project, include_deleted=False: [
            deepcopy(task) for task in stored.values()
            if include_deleted or task.status != TaskStatus.DELETED]
        manager = TaskManager(repository=repository)
        task = manager.create(self.project, "In memory")
        repository.initialize.assert_called_once_with(self.project)
        self.assertFalse(task.paths.root.exists())
        self.assertEqual(manager.load(self.project, task.id), task)
        self.assertEqual(manager.list(self.project), [task])
        clone = manager.clone(task, self.project)
        self.assertNotEqual(clone.id, task.id)
        manager.delete(task)
        self.assertEqual(stored[task.id].status, TaskStatus.DELETED)
        manager.restore(task)
        self.assertEqual(stored[task.id].status, TaskStatus.IDLE)
        # The inactive guard must consult the repository, not a task.json path
        # or the caller's stale object. No file exists for this Task.
        stored[task.id].status = TaskStatus.RUNNING
        with self.assertRaises(ValueError):
            manager.require_inactive(task)
        repository.reload.assert_called_with(task)

    def test_step_manager_lifecycle_uses_injected_repository_without_json(self) -> None:
        repository = Mock(spec=StepRepository)
        repository.paths.side_effect = StepRepository().paths
        stored = {}
        repository.exists.side_effect = lambda run, step_id: step_id in stored
        repository.save.side_effect = lambda step: stored.update({step.id: deepcopy(step)})
        repository.load.side_effect = lambda run, step_id: deepcopy(stored[step_id])
        repository.list.side_effect = lambda run: [deepcopy(step) for step in stored.values()]
        manager = StepManager(repository=repository)
        step = manager.create(self.run, "tool", "In memory")
        self.assertFalse(step.paths.root.exists())
        manager.start(step)
        manager.complete(step)
        self.assertEqual(manager.load(self.run, step.id).status, StepStatus.COMPLETED)
        with self.assertRaises(ValueError):
            manager.create(self.run, "tool", "Duplicate", step_id=step.id)
        pending = manager.create(self.run, "llm", "Pending")
        manager.recover(self.run)
        self.assertEqual(manager.load(self.run, pending.id).status, StepStatus.INTERRUPTED)
        self.assertEqual(manager.load(self.run, step.id).status, StepStatus.COMPLETED)

    def test_step_repository_validates_paths_and_duplicate_identity(self) -> None:
        self.assertTrue(self.steps.repository.exists(self.run, self.step.id))
        self.assertEqual(self.steps.repository.list(self.run), [self.step])
        with self.assertRaises(ValueError):
            self.steps.create(self.run, "llm", "Duplicate", step_id=self.step.id)
        with self.assertRaises(ValueError):
            self.steps.repository.paths(self.run, "../outside")
        with self.assertRaises(ValueError):
            self.task_repository.paths(self.project, "../outside")

    def test_run_manager_repository_injection_and_legacy_alias(self) -> None:
        manager = RunManager(self.tasks, EngineRegistry(), repository=self.runs)
        self.assertIs(manager.repository, self.runs)
        self.assertIs(manager.runs, self.runs)
        legacy = RunManager(self.tasks, EngineRegistry(), runs=self.runs)
        self.assertIs(legacy.repository, self.runs)
        with self.assertRaises(ValueError):
            RunManager(self.tasks, EngineRegistry(), repository=self.runs, runs=self.runs)
