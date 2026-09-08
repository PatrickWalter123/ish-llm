"""Generic component data, lifecycle, path safety and runtime tool adaptation."""

import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import get_type_hints
from unittest.mock import patch

from ish.components.base import Component, ProjectComponent
from ish.components.registry import ComponentRegistry
from ish.components.subagents import SubagentComponent
from ish.components.tools import Tool, ToolComponent, ToolRegistry
from ish.components.tools.resolver import ComponentToolResolver
from ish.components.workflows import WorkflowComponent
from ish.core.models import ProjectConfig, RunStatus
from ish.engines.base import EngineRegistry
from ish.engines.loop import LoopEngine
from ish.services.components import ComponentData
from ish.services.locking import WorkspaceBusyError
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager
from tests.test_loop import ScriptedCompletion, chunk, call


class Notes(Component):
    name = "notes"
    directory = "knowledge"


class ComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = ComponentRegistry((Notes(), SubagentComponent(), WorkflowComponent()))
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(self.root / "projects"), self.tasks,
                                       components=self.registry)
        self.project = self.projects.create("Example", components=("notes",))
        self.data = self.projects.component(self.project, "notes")

    def test_declaration_and_selection_own_directory_creation(self):
        root = self.project.paths.root
        self.assertTrue((root / "knowledge" / "records").is_dir())
        self.assertEqual(self.data.configuration(), {})
        self.assertFalse((root / "subagents").exists())
        self.projects.set_components(self.project, ("notes", "subagents", "workflows"))
        for directory in ("knowledge", "subagents", "workflows"):
            self.assertTrue((root / directory / "component.json").is_file())
        self.assertFalse(hasattr(self.project.paths, "knowledge"))
        self.assertEqual(self.projects.load(self.project.id).components, self.project.components)

    def test_crud_open_keys_serialization_and_detached_values(self):
        value = {"title": "문서", "future": {"options": [1, True, None]}, "version": 7}
        self.assertEqual(Component.deserialize(Component.serialize(value)), value)
        identifier = self.data.create(value, identifier="document")
        value["future"]["options"].append("caller")
        loaded = self.data.load(identifier)
        self.assertEqual(loaded["future"]["options"], [1, True, None])
        loaded["future"]["options"].append("reader")
        self.assertNotEqual(loaded, self.data.load(identifier))
        updated = self.data.update(identifier, {"future": {"new": 1}, "new_key": ["x"]})
        self.assertEqual(updated["future"], {"new": 1})
        self.assertEqual(updated["title"], "문서")
        self.assertEqual(self.data.list(), {identifier: updated})
        self.data.save(identifier, {"replacement": True})
        self.assertEqual(self.data.load(identifier), {"replacement": True})
        with self.assertRaises(FileExistsError):
            self.data.create({}, identifier=identifier)
        self.data.delete(identifier)
        self.assertEqual(self.data.list(), {})
        with self.assertRaises(FileNotFoundError):
            self.data.save(identifier, {})
        self.data.configure({"future_setting": {"label": "anything"}})
        self.assertEqual(self.data.configuration(), {"future_setting": {"label": "anything"}})

    def test_invalid_json_and_failed_atomic_replacement_keep_original_data(self):
        identifier = self.data.create({"kept": True})
        invalid = [{"value": object()}, {1: "lossy"}, {"value": float("nan")},
                   [1], {"tuple": (1, 2)}]
        for value in invalid:
            with self.subTest(value=type(value)):
                with self.assertRaises((TypeError, ValueError)):
                    self.data.save(identifier, value)
                self.assertEqual(self.data.load(identifier), {"kept": True})
        with patch("ish.services.storage.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.data.update(identifier, {"new": 1})
        self.assertEqual(self.data.load(identifier), {"kept": True})
        with self.assertRaises(TypeError):
            Component.deserialize("[]")
        with self.assertRaises(ValueError):
            Component.deserialize('{"number": NaN}')

    def test_directory_conflicts_and_path_traversal_are_rejected(self):
        for directory in ("../escape", "tasks", "state", "logs", "cache", "CON", "a/b", "a:b", "KNOWLEDGE"):
            with self.subTest(directory=directory):
                candidate = type("Other", (Component,), {"name": "other", "directory": directory})()
                with self.assertRaises(ValueError):
                    self.registry.register(candidate)
        with self.assertRaises(ValueError):
            self.registry.register(type("Missing", (Component,), {"name": "missing"})())
        for identifier in ("../project", "a/b", "a:b", "CON", "x.json", ""):
            with self.assertRaises(ValueError):
                self.data.create({}, identifier=identifier)
        self.assertTrue((self.project.paths.root / "project.json").is_file())

    def test_handles_reload_selection_deletion_and_project_ownership(self):
        self.data.create({"kept": True}, identifier="note")
        self.projects.remove_component(self.project, "notes")
        with self.assertRaises(ValueError):
            self.data.load("note")
        self.assertTrue((self.project.paths.root / "knowledge" / "records" / "note.json").exists())
        self.projects.set_components(self.project, ("notes",))
        self.assertEqual(self.data.load("note"), {"kept": True})
        wrong = deepcopy(self.project)
        wrong.paths = type(wrong.paths)(self.root / "outside")
        with self.assertRaises(ValueError):
            self.projects.component(wrong, "notes")
        self.projects.delete(self.project)
        with self.assertRaises(ValueError):
            self.data.create({})

    def test_permanent_component_removal_and_reenable(self):
        self.data.create({"kept": False}, identifier="note")
        tasks_root = self.project.paths.tasks
        self.projects.remove_component(self.project, "notes", permanent=True)
        self.assertFalse((self.project.paths.root / "knowledge").exists())
        self.assertTrue(tasks_root.exists())
        self.projects.set_components(self.project, ("notes",))
        self.assertEqual(self.data.list(), {})
        self.assertEqual(self.data.configuration(), {})
        with self.assertRaises(TypeError):
            self.projects.remove_component(self.project, "notes", permanent="yes")

    def test_clone_copies_definitions_and_config_but_not_arbitrary_artifacts(self):
        self.projects.set_components(self.project, ("notes", "subagents", "workflows"))
        agents = self.projects.component(self.project, "subagents")
        graphs = self.projects.component(self.project, "workflows")
        agents.create({"completion": {"model": "openai/test", "temperature": 0.2},
                       "system_prompt": "Review code", "custom": [1]}, identifier="reviewer")
        graphs.create({"nodes": [{"id": "review", "subagent": "reviewer"}], "edges": [],
                       "future_graph_option": True}, identifier="review")
        self.data.configure({"format_version": 3})
        self.data.create({"body": "text"}, identifier="note")
        (self.project.paths.root / "knowledge" / "artifact.bin").write_bytes(b"not a definition")
        clone = self.projects.clone(self.project)
        for name in self.project.components:
            source = self.projects.component(self.project, name)
            target = self.projects.component(clone, name)
            self.assertEqual(source.configuration(), target.configuration())
            self.assertEqual(source.list(), target.list())
        self.projects.component(clone, "subagents").update("reviewer", {"custom": []})
        self.assertEqual(agents.load("reviewer")["custom"], [1])
        self.assertFalse((clone.paths.root / "knowledge" / "artifact.bin").exists())

    def test_legacy_workflow_directory_is_initialized_without_losing_files(self):
        root = self.project.paths.root / "workflows"
        root.mkdir()
        (root / "legacy.json").write_text('{"graph": []}', encoding="utf-8")
        self.projects.set_components(self.project, ("notes", "workflows"))
        graphs = self.projects.component(self.project, "workflows")
        self.assertEqual(graphs.list(), {})
        self.assertTrue((root / "legacy.json").exists())

    def test_legacy_selected_workflow_can_be_read_and_cloned_without_migration(self):
        self.projects.set_components(self.project, ("notes", "workflows"))
        path = self.project.paths.root / "workflows" / "component.json"
        path.unlink()  # Reproduce the previous directory-only persisted format.
        graphs = self.projects.component(self.projects.load(self.project.id), "workflows")
        self.assertEqual(graphs.configuration(), {})
        clone = self.projects.clone(self.project)
        self.assertEqual(self.projects.component(clone, "workflows").configuration(), {})
        self.assertFalse(path.exists())  # Queries/clone do not modify the source.

    def test_generic_exports_have_no_tool_dependency(self):
        class Search(Component):
            name = "search"
            directory = "search_index"
            capabilities = ("retriever",)
            def resolve(self, project, capability):
                return {"project": project.id}
        self.registry.register(Search())
        self.projects.set_components(self.project, ("notes", "search"))
        self.assertEqual(self.registry.resolve(self.project, "retriever"), ({"project": self.project.id},))
        self.assertEqual(ComponentToolResolver(self.registry).resolve_tools(self.project).names(), ())
        code = ("import sys; import ish.components.base; import ish.components.registry; "
                "assert not any(k.startswith('ish.components.tools') for k in sys.modules)")
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)

    def test_component_handle_obeys_workspace_lock_and_serializes_updates(self):
        self.data.create({}, identifier="record")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.data.update("record", {str(i): i}), range(12)))
        self.assertEqual(self.data.load("record"), {str(i): i for i in range(12)})
        competing = ProjectRepository(self.projects.repository.root)
        with competing.ownership.scope():
            with self.assertRaises(WorkspaceBusyError):
                self.data.update("record", {"must_not_write": True})
        self.assertNotIn("must_not_write", self.data.load("record"))

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_linked_directory_and_contents_cannot_escape_component_root(self):
        import _winapi
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        root = self.project.paths.root / "knowledge"
        link = root / "records"
        link.rmdir()
        _winapi.CreateJunction(str(outside), str(link))
        try:
            for action in (lambda: self.data.create({}, identifier="escape"), self.data.list,
                           lambda: self.projects.remove_component(self.project, "notes", permanent=True)):
                with self.assertRaises(ValueError):
                    action()
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((outside / "escape.json").exists())
        finally:
            link.rmdir()

    def test_public_type_hints_remain_resolvable(self):
        for cls in (Component, ComponentData, ProjectComponent):
            for name, member in inspect.getmembers(cls, inspect.isfunction):
                if not name.startswith("_"):
                    get_type_hints(member)


class ToolDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_tool_definition_reaches_loop_and_uses_registered_handler(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            async def handler(arguments):
                calls.append(arguments)
                return "result"
            catalog = ToolRegistry((Tool("add", "old", {"type": "object"}, handler),))
            tools = ToolComponent(catalog)
            registry = ComponentRegistry((tools, SubagentComponent()))
            tasks = TaskManager()
            projects = ProjectManager(ProjectRepository(Path(temporary)), tasks, components=registry)
            project = projects.create("Test", components=("tools", "subagents"),
                                      config=ProjectConfig(completion={"model": "openai/test"}))
            data = projects.component(project, "tools")
            definition = {"type": "function", "function": {"name": "add", "description": "new",
                          "parameters": {"type": "object", "properties": {"a": {"type": "number"}},
                                         "required": ["a"], "additionalProperties": False},
                          "strict": True}, "future_extension": {"enabled": True}}
            self.assertEqual(data.create(definition), "add")
            data.configure({"enabled": ["add"], "future_policy": {"label": "test"}})
            resolved = ComponentToolResolver(registry).resolve_tools(project)
            self.assertEqual(resolved.definitions(), [definition])
            self.assertIs(resolved.get("add").handler, handler)
            with self.assertRaises(ValueError):
                resolved.prepare("add", '{"b": 1}')
            completion = ScriptedCompletion([chunk(calls=[call('{"a":2}')], finish="tool_calls")],
                                            [chunk("done", finish="stop")])
            engines = EngineRegistry()
            engines.register("loop", LoopEngine(completion_fn=completion))
            task = tasks.create(project, "Test")
            manager = RunManager(tasks, engines, task=task, capabilities=registry)
            try:
                await manager.submit("request")
                await manager.wait_idle()
                self.assertEqual(calls, [{"a": 2}])
                self.assertEqual(completion.requests[0]["tools"], [definition])
                self.assertEqual(manager.repository.list(task)[0].status, RunStatus.COMPLETED)
                with self.assertRaises(ValueError):
                    projects.remove_component(project, "tools", permanent=True)
            finally:
                await manager.shutdown()
            with self.assertRaises(ValueError):
                data.delete("add")
            clone = projects.clone(project)
            self.assertEqual(projects.component(clone, "tools").list(), {"add": definition})
            data.configure({"enabled": []})
            data.delete("add")
            self.assertEqual(data.list(), {})
            self.assertEqual(projects.component(clone, "tools").load("add"), definition)

    async def test_invalid_tool_record_does_not_replace_valid_definition(self):
        with tempfile.TemporaryDirectory() as temporary:
            async def handler(arguments):
                return None
            catalog = ToolRegistry((Tool("add", "add", {"type": "object"}, handler),))
            registry = ComponentRegistry((ToolComponent(catalog),))
            projects = ProjectManager(ProjectRepository(Path(temporary)), TaskManager(), components=registry)
            project = projects.create("Test", components=("tools",))
            data = projects.component(project, "tools")
            valid = catalog.definitions()[0]
            data.create(valid)
            for invalid in ({"type": "function", "function": {"name": "other", "parameters": {}}},
                            {"type": "function", "function": {"name": "add", "parameters": {"type": "string"}}},
                            {"type": "function", "function": {"name": "add", "parameters": {"type": "object", "$ref": "https://invalid.test"}}}):
                with self.assertRaises(ValueError):
                    data.save("add", invalid)
                self.assertEqual(data.load("add"), valid)
