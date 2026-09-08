"""Task-bound public runtime API, notifications, and open component settings."""

import asyncio
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path

from ish.components import Component, ComponentRegistry
from ish.components.tools import Tool, ToolComponent, ToolRegistry
from ish.components.tools.resolver import ComponentToolResolver
from ish.components.subagents import SubagentComponent
from ish.components.workflows import WorkflowComponent
from ish.core.models import ProjectConfig, Run, RunStatus, MessageRole, MessageStatus, new_id
from ish.engines import BaseEngine, EngineRegistry
from ish.services.conversation import ConversationStore
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager, RunRequestError, RunErrorCode, RunEventType
from ish.services.tasks import TaskManager
from tests.support.fake_engine import FakeStreamingEngine


class PublicRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tasks = TaskManager()
        self.components = ComponentRegistry()
        self.projects = ProjectManager(ProjectRepository(Path(temp.name)), self.tasks,
                                       components=self.components)
        self.project = self.projects.create("Project", config=ProjectConfig(default_engine="fake"))
        self.task = self.tasks.create(self.project, "Task")
        self.engines = EngineRegistry()
        self.engine = FakeStreamingEngine()
        self.engines.register("fake", self.engine)

    def manager(self, task=None, **kwargs):
        manager = RunManager(self.tasks, self.engines, task=task or self.task, **kwargs)
        self.addAsyncCleanup(manager.shutdown)
        return manager

    async def test_task_is_required_and_binding_is_detached_from_caller_mutations(self):
        with self.assertRaises(TypeError):
            RunManager(self.tasks, self.engines)
        with self.assertRaises(TypeError):
            RunManager(self.tasks, self.engines, task=None)
        manager = self.manager()
        exposed = manager.task
        exposed.id = new_id()
        self.assertEqual(manager.task.id, self.task.id)
        with self.assertRaises(TypeError):
            await manager.submit(self.project, self.task, "old signature")
        await manager.submit("new signature")
        await manager.wait_idle()
        self.assertEqual(manager.repository.list(self.task)[0].status, RunStatus.COMPLETED)

    async def test_registration_rejections_do_not_admit_messages_or_attach_runtime(self):
        manager = self.manager()
        with self.assertRaises(RunRequestError) as caught:
            await manager.submit("request", engine="typo")
        self.assertEqual(caught.exception.code, RunErrorCode.ENGINE_NOT_REGISTERED)
        self.assertEqual(ConversationStore(self.task.paths.conversation).list(), [])
        self.assertEqual(manager.repository.list(self.task), [])
        self.tasks.save(self.task)  # No runtime lease was acquired by the rejection.
        for name, engine in (("", self.engine), ("invalid", object())):
            with self.assertRaises((TypeError, ValueError)):
                self.engines.register(name, engine)

    async def test_missing_component_registration_is_rejected_before_queue_admission(self):
        self.components.register(SubagentComponent())
        self.projects.set_components(self.project, ("subagents",))
        manager = self.manager()  # Application omitted the Project component registry.
        with self.assertRaises(RunRequestError) as caught:
            await manager.submit("request")
        self.assertEqual(caught.exception.code, RunErrorCode.COMPONENT_NOT_REGISTERED)
        self.assertEqual(manager.repository.list(self.task), [])
        self.assertEqual(ConversationStore(self.task.paths.conversation).list(), [])

    async def test_started_and_terminal_notifications_follow_durable_state_on_event_loop(self):
        loop_thread = threading.get_ident()
        events = []
        manager = None
        def observe(event):
            self.assertEqual(threading.get_ident(), loop_thread)
            persisted = manager.repository.load(self.task, event.run.id)
            self.assertEqual(persisted.status, event.run.status)
            events.append(event)
        manager = self.manager(on_run_event=observe)
        await manager.submit("request")
        await manager.wait_idle()
        self.assertEqual([event.type for event in events], [RunEventType.STARTED, RunEventType.COMPLETED])
        self.assertEqual(events[0].run.status, RunStatus.RUNNING)
        events[1].run.metadata["changed_by_observer"] = True
        self.assertNotIn("changed_by_observer", manager.repository.list(self.task)[0].metadata)

    async def test_early_failure_reports_code_detail_and_failed_event_then_continues_queue(self):
        class Broken:
            async def execute(self, context):
                raise RuntimeError("action failure detail")
                yield
        self.engines.register("broken", Broken())
        events = []
        manager = self.manager(on_run_event=events.append)
        await manager.submit("bad", engine="broken")
        await manager.submit("good")
        await manager.wait_idle()
        self.assertEqual([e.type for e in events], [RunEventType.STARTED, RunEventType.FAILED,
                                                  RunEventType.STARTED, RunEventType.COMPLETED])
        failed = events[1].run
        self.assertEqual(failed.error_code, RunErrorCode.ENGINE_FAILED)
        self.assertEqual(failed.error, "action failure detail")
        result = self.projects.results.load(self.project, failed.id)
        self.assertEqual(result.error_code, "engine_failed")
        self.assertEqual(result.error, failed.error)

    async def test_runtime_binding_failure_is_a_failed_run_not_a_data_crud_failure(self):
        tools = ToolComponent(ToolRegistry())
        self.components.register(tools)
        self.projects.set_components(self.project, ("tools",))
        data = self.projects.component(self.project, "tools")
        data.create({"type": "function", "function": {"name": "offline", "parameters": {"type": "object"}}})
        data.configure({"enabled": ["offline"]})
        events = []
        manager = self.manager(capabilities=self.components, on_run_event=events.append)
        await manager.submit("request")
        await manager.wait_idle()
        self.assertEqual(events[-1].type, RunEventType.FAILED)
        self.assertEqual(events[-1].run.error_code, RunErrorCode.CAPABILITY_FAILED)
        self.assertEqual(self.engine.contexts, [])
        self.assertIn("offline", data.list())

    async def test_restored_queue_with_unregistered_engine_has_a_terminal_run_and_notification(self):
        store = ConversationStore(self.task.paths.conversation)
        request = store.create(MessageRole.USER, "old request", MessageStatus.QUEUED,
                               metadata={"engine": "removed_engine"})
        events = []
        manager = self.manager(on_run_event=events.append)
        await manager.start()
        await manager.wait_idle()
        run = manager.repository.list(self.task)[0]
        self.assertEqual(run.error_code, RunErrorCode.ENGINE_NOT_REGISTERED)
        self.assertEqual(events[-1].type, RunEventType.FAILED)
        self.assertEqual(store.get(request.id).status, MessageStatus.COMMITTED)
        self.assertEqual(self.engine.contexts, [])

    async def test_failing_observer_does_not_drop_requests(self):
        called = []
        def observe(event):
            called.append(event.type)
            raise RuntimeError("display failure")
        manager = self.manager(on_run_event=observe)
        await manager.submit("one")
        await manager.submit("two")
        await manager.wait_idle()
        self.assertEqual(called.count(RunEventType.COMPLETED), 2)
        self.assertTrue(all(r.status == RunStatus.COMPLETED for r in manager.repository.list(self.task)))

    async def test_shutdown_one_task_preserves_other_task_and_releases_only_its_attachment(self):
        self.engine.gate = asyncio.Event()
        other = self.tasks.create(self.project, "Other")
        events = []
        first = self.manager(on_run_event=events.append)
        second = self.manager(other)
        await first.submit("first")
        await second.submit("other")
        async def both_running():
            while self.engine.active != 2:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(both_running(), 10)
        queued = await first.submit("later")
        await first.shutdown()
        self.assertEqual(events[-1].type, RunEventType.INTERRUPTED)
        self.assertEqual(events[-1].run.error_code, RunErrorCode.INTERRUPTED)
        self.assertEqual(ConversationStore(self.task.paths.conversation).get(queued.id).status, MessageStatus.QUEUED)
        self.assertEqual(second.repository.list(other)[0].status, RunStatus.RUNNING)
        edited = self.tasks.load(self.project, self.task.id)
        edited.title = "Edited without stopping Other"
        self.tasks.save(edited)
        self.engine.gate.set()
        await second.wait_idle()
        resumed = self.manager()
        await resumed.start()
        await resumed.wait_idle()
        self.assertEqual(resumed.repository.list(self.task)[-1].status, RunStatus.COMPLETED)

    async def test_recovery_emits_interrupted_with_restart_code_without_replaying(self):
        manager = self.manager()
        store = ConversationStore(self.task.paths.conversation)
        request = store.create(MessageRole.USER, "claimed", MessageStatus.COMMITTED)
        run_id = new_id()
        run = Run(run_id, self.task.id, request.id, new_id(), "fake",
                  manager.repository.paths(self.task, run_id), status=RunStatus.RUNNING)
        with self.projects.ownership.scope():
            manager.repository.save(run)
            store.create(MessageRole.ASSISTANT, "partial", MessageStatus.STREAMING,
                         message_id=run.assistant_message_id, run_id=run.id)
        events = []
        manager.on_run_event = events.append
        await manager.start()
        await manager.wait_idle()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, RunEventType.INTERRUPTED)
        self.assertEqual(events[0].run.error_code, RunErrorCode.PROCESS_RESTART)
        self.assertEqual(self.engine.contexts, [])


class OpenComponentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tasks = TaskManager()
        self.tool = ToolComponent(ToolRegistry())
        self.components = ComponentRegistry((self.tool, SubagentComponent(), WorkflowComponent()))
        self.projects = ProjectManager(ProjectRepository(Path(temp.name)), self.tasks, components=self.components)
        self.project = self.projects.create("Project", components=("tools", "subagents", "workflows"))

    def test_open_config_roundtrip_clone_task_overrides_and_field_names(self):
        config = ProjectConfig({"editor": {"font_size": 14}}, theme="dark")
        self.assertFalse(is_dataclass(config))
        config["password"] = "ordinary-example-value"
        config.completion = {"model": "example", "api_key": "example-value"}
        config["new_section"] = {"custom": [1, True]}
        config["model"] = {"workspace_setting": True}
        config["__deepcopy__"] = "ordinary mapping key"
        self.project.config = config
        self.assertEqual(asdict(self.project)["config"], config)
        self.projects.save(self.project)
        loaded = self.projects.load(self.project.id).config
        self.assertEqual(loaded, config)
        self.assertEqual(ProjectConfig.deserialize(config.serialize()), config)
        self.assertEqual(self.projects.clone(self.project).config, config)
        settings = config.for_engine("loop", {"editor": {"font_size": 18}})
        self.assertEqual(settings["editor"]["font_size"], 18)
        self.assertEqual(config["editor"]["font_size"], 14)
        self.assertEqual(settings["theme"], "dark")
        encoded = Component.serialize({"properties": {"password": {"type": "string"}}})
        self.assertIn("password", Component.deserialize(encoded)["properties"])

    def test_all_definition_crud_and_clone_work_without_runtime_handlers(self):
        data = self.projects.component(self.project, "tools")
        definition = {"type": "function", "function": {"name": "offline", "parameters": {"type": "object"}}}
        data.create(definition)
        data.configure({"enabled": ["offline"]})
        self.assertEqual(data.load("offline"), definition)
        data.update("offline", {"extension": True})
        for name in ("subagents", "workflows"):
            records = self.projects.component(self.project, name)
            records.create({"unknown": {"future": [1]}}, identifier="example")
            records.update("example", {"new": True})
            self.assertTrue(records.load("example")["new"])
        clone = self.projects.clone(self.project)
        for name in self.project.components:
            self.assertEqual(self.projects.component(clone, name).list(), self.projects.component(self.project, name).list())
        with self.assertRaises(ValueError):
            ComponentToolResolver(self.components).resolve_tools(self.project)
        async def handler(arguments): return "ok"
        self.tool.catalog.register(Tool("offline", "", {"type": "object"}, handler))
        self.assertIs(ComponentToolResolver(self.components).resolve_tools(self.project).get("offline").handler, handler)
        data.configure({"enabled": []})
        data.delete("offline")
        self.assertEqual(data.list(), {})

    def test_only_requested_capability_is_built(self):
        requested = []
        class Multi(Component):
            name = "multi"
            directory = "multi"
            capabilities = ("retriever", "reranker")
            def resolve(self, project, capability):
                requested.append(capability)
                if capability == "reranker":
                    raise RuntimeError("backend unavailable")
                return "retriever instance"
        self.components.register(Multi())
        self.projects.set_components(self.project, ("tools", "subagents", "workflows", "multi"))
        self.assertEqual(self.components.resolve(self.project, "retriever"), ("retriever instance",))
        self.assertEqual(requested, ["retriever"])
        self.assertEqual(ComponentToolResolver(self.components).resolve_tools(self.project).names(), ())
        self.assertEqual(requested, ["retriever"])
