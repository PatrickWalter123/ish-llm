import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from ish.components.base import Component
from ish.components.registry import ComponentRegistry
from ish.components.tools import Tool, ToolRegistry
from ish.components.tools.component import ToolComponent, ToolPaths
from ish.components.workflows.component import WorkflowComponent, WorkflowPaths
from ish.core.models import MessageRole, MessageStatus, ProjectConfig, RunStatus, TaskStatus
from ish.core.paths import ProjectPaths, TaskPaths
from ish.engines.base import EngineEvent, EngineEventType, EngineRegistry
from ish.engines.loop import LoopEngine
from ish.services.conversation import ConversationStore
from ish.services.context import ConversationContextBuilder
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.storage import atomic_json, read_json
from ish.services.tasks import TaskManager
from tests.support.fake_engine import FakeStreamingEngine


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = []

        async def add(arguments):
            self.calls.append(arguments)
            return "5"

        self.catalog = ToolRegistry((Tool("add", "add", {"type": "object"}, add),))
        self.components = ComponentRegistry((ToolComponent(self.catalog), WorkflowComponent()))
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(self.root), self.tasks, components=self.components)
        self.project = self.projects.create("Project", config=ProjectConfig(default_engine="fake"))
        self.task = self.tasks.create(self.project, "Task")
        self.engines = EngineRegistry()
        self.engines.register("fake", FakeStreamingEngine())

    def manager(self, **kwargs):
        manager = RunManager(self.tasks, self.engines, **kwargs)
        self.addAsyncCleanup(manager.shutdown)
        return manager

    async def test_deleted_project_stale_handles_cannot_mutate_or_execute(self):
        stale = deepcopy(self.project)
        self.projects.delete(self.project)
        mutations = (
            lambda: self.tasks.create(stale, "invalid"),
            lambda: self.projects.save(stale),
            lambda: self.projects.clone(stale),
            lambda: self.tasks.clone(self.task, stale),
            lambda: self.tasks.save(self.task),
            lambda: self.projects.set_components(stale, ("tools",)),
            lambda: self.projects.configure_component(stale, "tools", {"enabled": ["add"]}),
        )
        for operation in mutations:
            with self.assertRaises(ValueError):
                operation()
        manager = self.manager()
        with self.assertRaises(ValueError):
            await manager.start(stale, self.task)
        with self.assertRaises(ValueError):
            await manager.submit(stale, self.task, "rejected")
        self.assertFalse(self.task.paths.conversation.exists())
        self.assertEqual(manager.repository.list(self.task), [])

    async def test_permanent_deletion_cannot_be_reversed_by_stale_save(self):
        stale_project, stale_task = deepcopy(self.project), deepcopy(self.task)
        self.projects.delete(self.project, permanent=True)
        for operation in (lambda: self.projects.save(stale_project),
                          lambda: self.tasks.save(stale_task),
                          lambda: self.tasks.create(stale_project, "invalid")):
            with self.assertRaises(FileNotFoundError):
                operation()
        self.assertFalse(stale_project.paths.root.exists())

    async def test_deleted_task_stale_save_and_restore_under_deleted_parent_are_rejected(self):
        stale = deepcopy(self.task)
        self.tasks.delete(self.task)
        with self.assertRaises(ValueError):
            self.tasks.save(stale)
        self.projects.delete(self.project)
        with self.assertRaises(ValueError):
            self.tasks.restore(self.task)
        self.projects.restore(self.project)
        self.tasks.delete(self.task, permanent=True)
        with self.assertRaises(FileNotFoundError):
            self.tasks.save(stale)
        self.assertFalse(stale.paths.root.exists())

    async def test_save_cannot_change_managed_lifecycle_state(self):
        changed = deepcopy(self.task)
        changed.status = TaskStatus.RUNNING
        with self.assertRaises(ValueError):
            self.tasks.save(changed)
        changed_project = deepcopy(self.project)
        changed_project.deleted = True
        with self.assertRaises(ValueError):
            self.projects.save(changed_project)
        changed_project = deepcopy(self.project)
        changed_project.components = ("tools",)
        with self.assertRaises(ValueError):
            self.projects.save(changed_project)
        self.assertEqual(self.tasks.load(self.project, self.task.id).status, TaskStatus.IDLE)

    async def test_idle_attached_task_cannot_be_overwritten_by_public_save(self):
        manager = self.manager()
        await manager.start(self.project, self.task)
        self.task.title = "stale edit"
        with self.assertRaises(ValueError):
            self.tasks.save(self.task)
        self.assertEqual(self.tasks.load(self.project, self.task.id).title, "Task")

    async def test_forged_handle_paths_rejected_before_mutation(self):
        forged = deepcopy(self.project)
        forged.paths = ProjectPaths(self.root / "outside")
        with self.assertRaises(ValueError):
            self.projects.restore(forged)
        forged_task = deepcopy(self.task)
        forged_task.paths = TaskPaths(self.root / "outside")
        with self.assertRaises(ValueError):
            await self.manager().submit(self.project, forged_task, "invalid")
        self.assertFalse((self.root / "outside").exists())

    async def test_observer_errors_do_not_fail_execution_or_drop_queued_requests(self):
        def broken(run, event):
            raise RuntimeError("private display error")
        manager = self.manager(on_event=broken)
        await manager.submit(self.project, self.task, "one")
        await manager.submit(self.project, self.task, "two")
        await manager.wait_idle(self.project, self.task)
        runs = manager.repository.list(self.task)
        self.assertEqual([run.status for run in runs], [RunStatus.COMPLETED, RunStatus.COMPLETED])
        for run in runs:
            self.assertEqual(ConversationStore(self.task.paths.conversation).get(
                run.assistant_message_id).content, "Hello world")
            content = (run.paths.logs / "service.log").read_text(encoding="utf-8")
            self.assertIn("observer.failed", content)
            self.assertNotIn("private display error", content)

    async def test_components_create_only_selected_directories_and_survive_reload(self):
        self.assertFalse(ToolPaths.for_project(self.project).root.exists())
        self.assertFalse(WorkflowPaths.for_project(self.project).root.exists())
        selected = self.projects.create("selected", components=("tools", "workflows"))
        self.assertTrue(ToolPaths.for_project(selected).configuration.is_file())
        self.assertTrue(WorkflowPaths.for_project(selected).root.is_dir())
        self.assertEqual(self.projects.load(selected.id).components, ("tools", "workflows"))
        self.assertFalse(hasattr(selected.paths, "tools"))
        self.assertFalse(hasattr(selected.paths, "workflows"))

    async def test_unknown_duplicate_or_malformed_selection_creates_nothing(self):
        before = set(self.root.iterdir())
        for names in (("unknown",), ("tools", "tools"), "tools", (123,)):
            with self.assertRaises(ValueError):
                self.projects.create("invalid", components=names)
        self.assertEqual(set(self.root.iterdir()), before)

    async def test_failed_component_addition_does_not_publish_selection(self):
        class Broken(Component):
            name = "broken"
            def initialize(self, project):
                (project.paths.root / "partial").mkdir(exist_ok=True)
                raise ValueError("failed")
        self.components.register(Broken())
        with self.assertRaises(ValueError):
            self.projects.set_components(self.project, ("broken",))
        self.assertEqual(self.projects.load(self.project.id).components, ())
        self.assertEqual(self.project.components, ())
        self.assertEqual(self.components.resolve_tools(self.projects.load(self.project.id)).names(), ())

    async def test_unselected_or_invalid_tool_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.projects.configure_component(self.project, "tools", {"enabled": ["add"]})
        self.projects.set_components(self.project, ("tools",))
        path = ToolPaths.for_project(self.project).configuration
        before = path.read_bytes()
        for config in ({"enabled": ["unknown"]}, {"enabled": "add"},
                       {"enabled": ["add", "add"]}, {"enabled": ["add"], "api_key": "bad"}):
            with self.assertRaises(ValueError):
                self.projects.configure_component(self.project, "tools", config)
        self.assertEqual(path.read_bytes(), before)

    async def test_component_selection_changes_preserve_data_and_initialization_is_idempotent(self):
        self.projects.set_components(self.project, ("tools",))
        self.projects.configure_component(self.project, "tools", {"enabled": ["add"]})
        self.projects.set_components(self.project, ())
        self.assertTrue(ToolPaths.for_project(self.project).configuration.exists())
        self.assertEqual(self.components.resolve_tools(self.projects.load(self.project.id)).names(), ())
        self.projects.set_components(self.project, ("tools",))
        self.assertEqual(self.components.resolve_tools(self.project).names(), ("add",))

    async def test_clone_uses_current_project_state_and_delegates_component_configuration(self):
        stale = deepcopy(self.project)
        self.project.title = "Updated"
        self.projects.save(self.project)
        self.projects.set_components(self.project, ("tools", "workflows"))
        self.projects.configure_component(self.project, "tools", {"enabled": ["add"]})
        clone = self.projects.clone(stale)
        self.assertEqual(clone.title, "Updated")
        self.assertEqual(clone.components, ("tools", "workflows"))
        self.assertEqual(self.components.resolve_tools(clone).names(), ("add",))
        self.projects.configure_component(clone, "tools", {"enabled": []})
        self.assertEqual(self.components.resolve_tools(self.project).names(), ("add",))

    async def test_old_metadata_defaults_to_no_components_without_scanning_directories(self):
        path = self.project.paths.root / "project.json"
        data = read_json(path)
        data.pop("components")
        atomic_json(path, data)
        ToolPaths.for_project(self.project).root.mkdir()
        loaded = self.projects.load(self.project.id)
        self.assertEqual(loaded.components, ())
        self.assertEqual(self.components.resolve_tools(loaded).names(), ())

    async def test_component_owns_custom_layout_and_failed_initialization_can_be_restored(self):
        class Custom(Component):
            name = "custom"
            broken = True
            def initialize(self, project):
                (project.paths.root / "custom_data" / "nested").mkdir(parents=True, exist_ok=True)
                if self.broken:
                    raise ValueError("initialization failed")
        custom = Custom()
        self.components.register(custom)
        with self.assertRaises(ValueError):
            self.projects.create("failed", components=("custom",))
        failed, = [project for project in self.projects.list(include_deleted=True) if project.deleted]
        self.assertTrue((failed.paths.root / "custom_data" / "nested").is_dir())
        self.assertNotIn(failed.id, [project.id for project in self.projects.list()])
        custom.broken = False
        self.projects.restore(failed)
        self.assertFalse(self.projects.load(failed.id).deleted)

    async def test_missing_component_registration_fails_before_provider_execution(self):
        self.projects.set_components(self.project, ("tools",))
        completion = Mock(side_effect=AssertionError("must not call provider"))
        self.engines.register("loop", LoopEngine(completion_fn=completion))
        manager = self.manager()  # no capability resolver registered in this process
        await manager.submit(self.project, self.task, "test", engine="loop")
        await manager.wait_idle(self.project, self.task)
        self.assertEqual(manager.repository.list(self.task)[0].status, RunStatus.FAILED)
        completion.assert_not_called()

    async def test_two_projects_share_loop_engine_but_not_enabled_tools(self):
        self.projects.set_components(self.project, ("tools",))
        self.projects.configure_component(self.project, "tools", {"enabled": ["add"]})
        self.project.config.model = "openai/test-model"
        self.projects.save(self.project)
        other = self.projects.create("Other", config=self.project.config)
        other_task = self.tasks.create(other, "Other")
        requests = []
        def completion(**kwargs):
            requests.append(kwargs)
            if kwargs["messages"][-1]["role"] == "tool":
                delta, reason = {"content": "done"}, "stop"
            else:
                delta, reason = {"tool_calls": [{"index": 0, "id": "call1", "type": "function",
                    "function": {"name": "add", "arguments": "{}"}}]}, "tool_calls"
            yield {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
        self.engines.register("loop", LoopEngine(completion_fn=completion))
        manager = self.manager(capabilities=self.components)
        await manager.submit(self.project, self.task, "allowed", engine="loop")
        await manager.submit(other, other_task, "denied", engine="loop")
        await asyncio.gather(manager.wait_idle(self.project, self.task), manager.wait_idle(other, other_task))
        self.assertEqual(self.calls, [{}])
        self.assertEqual(manager.repository.list(self.task)[0].status, RunStatus.COMPLETED)
        self.assertEqual(manager.repository.list(other_task)[0].status, RunStatus.FAILED)
        self.assertNotIn("tools", next(item for item in requests if item["messages"][0]["content"] == "denied"))

    async def test_capabilities_are_fixed_for_one_run_and_refreshed_for_next(self):
        self.projects.set_components(self.project, ("tools",))
        self.projects.configure_component(self.project, "tools", {"enabled": ["add"]})
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        class Inspect:
            async def execute(self, context):
                seen.append(context.tools.names())
                entered.set()
                await release.wait()
                seen.append(context.tools.names())
                yield EngineEvent(EngineEventType.TEXT_DELTA, text="done")
        self.engines.register("inspect", Inspect())
        manager = self.manager(capabilities=self.components)
        await manager.submit(self.project, self.task, "one", engine="inspect")
        await asyncio.wait_for(entered.wait(), 5)
        self.projects.configure_component(self.project, "tools", {"enabled": []})
        await manager.submit(self.project, self.task, "two", engine="inspect")
        release.set()
        await manager.wait_idle(self.project, self.task)
        self.assertEqual(seen, [("add",), ("add",), (), ()])

    async def test_factory_and_context_builder_are_shared_by_runtime_and_clone(self):
        builder = Mock(wraps=ConversationContextBuilder())
        factory = Mock(side_effect=lambda task: ConversationStore(task.paths.root / "custom.jsonl"))
        tasks = TaskManager(conversations=factory, context_builder=builder)
        projects = ProjectManager(ProjectRepository(self.root / "injected"), tasks)
        project = projects.create("Injected", config=ProjectConfig(default_engine="fake"))
        task = tasks.create(project, "Injected")
        manager = RunManager(tasks, self.engines)
        self.addAsyncCleanup(manager.shutdown)
        await manager.submit(project, task, "one")
        await manager.wait_idle(project, task)
        await manager.shutdown()
        clone = tasks.clone(task, project)
        self.assertTrue((clone.paths.root / "custom.jsonl").exists())
        self.assertFalse(clone.paths.conversation.exists())
        builder.for_run.assert_called_once()
        builder.for_clone.assert_called_once()
        self.assertEqual([message.content for message in factory(clone).list()], ["one", "Hello world"])
        self.assertTrue(all(message.run_id is None for message in factory(clone).list()))

    async def test_new_runs_use_saved_project_configuration_instead_of_stale_handle(self):
        manager = self.manager()
        stale = deepcopy(self.project)
        await manager.start(stale, self.task)
        self.project.config.model = "updated"
        self.projects.save(self.project)
        await manager.submit(stale, self.task, "test")
        await manager.wait_idle(stale, self.task)
        self.assertEqual(self.engines.resolve("fake").contexts[0].project.config.model, "updated")
