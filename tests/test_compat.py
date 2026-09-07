"""Exercise actual interpreter behavior, including timeout and path safety."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import get_type_hints

from ish.compat import aclosing, is_junction, timeout
from ish.core.models import (
    Message, MessageRole, MessageStatus, Project, ProjectConfig, Run, RunStatus,
    Step, StepStatus, Task, TaskStatus,
)
from ish.core.paths import ProjectPaths
from ish.engines.base import BaseEngine, EngineContext, EngineEvent, EngineEventType
from ish.engines.loop import LoopEngine
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.storage import record
from ish.services.tasks import TaskManager, TaskRuntime


class CompatibilityTests(unittest.TestCase):
    def test_enum_string_formatting_and_json_remain_persisted_values(self) -> None:
        for enum in (MessageRole, MessageStatus, TaskStatus, RunStatus, StepStatus, EngineEventType):
            for member in enum:
                with self.subTest(enum=enum.__name__, value=member.value):
                    self.assertEqual(str(member), member.value)
                    self.assertEqual(f"{member}", member.value)
                    self.assertEqual(format(member, ">16"), format(member.value, ">16"))
                    self.assertEqual(json.loads(json.dumps(member)), member.value)
                    self.assertIs(enum(member.value), member)

    def test_public_model_type_hints_resolve_on_python39(self) -> None:
        for model in (Project, ProjectConfig, Task, Message, Run, Step, TaskRuntime,
                      EngineEvent, EngineContext):
            with self.subTest(model=model.__name__):
                hints = get_type_hints(model)
                self.assertTrue({field.name for field in fields(model)} <= hints.keys())
        self.assertTrue(get_type_hints(TaskManager.create))
        for method in (BaseEngine.__init__, BaseEngine.step, BaseEngine.stream_completion,
                       LoopEngine.__init__):
            self.assertTrue(get_type_hints(method))

    def test_dataclass_defaults_frozen_copy_and_field_only_persistence(self) -> None:
        config = ProjectConfig()
        clone = deepcopy(config)
        clone.model = "different"
        self.assertEqual(config.model, "")
        event = EngineEvent(EngineEventType.TEXT_DELTA, text="hello")
        self.assertEqual(deepcopy(event), event)
        with self.assertRaises(FrozenInstanceError):
            event.text = "changed"
        self.assertEqual(hasattr(config, "__dict__"), sys.version_info < (3, 10))
        if sys.version_info < (3, 10):
            config.runtime_only = object()
            self.assertNotIn("runtime_only", record(config))

    def test_junction_detection_for_normal_file_directory_and_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "file"
            file.touch()
            for path in (root, file, root / "missing"):
                self.assertFalse(is_junction(path))

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_real_windows_junction_cannot_be_followed_by_permanent_delete(self) -> None:
        # Create both the owned tree and its external target inside one test root.
        # _winapi is used only by this Windows test, never by product code.
        import _winapi
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = TaskManager()
            projects = ProjectManager(ProjectRepository(root / "projects"), tasks)
            project = projects.create("test")
            task = tasks.create(project, "test")
            outside = root / "outside"
            outside.mkdir()
            marker = outside / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            link = task.paths.root / "redirect"
            _winapi.CreateJunction(str(outside), str(link))
            try:
                self.assertTrue(is_junction(link))
                with self.assertRaises(ValueError):
                    tasks.delete(task, permanent=True)
                with self.assertRaises(ValueError):
                    projects.delete(project, permanent=True)
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                self.assertTrue((task.paths.root / "task.json").exists())
            finally:
                # Remove only the junction entry, never recursively its target.
                link.rmdir()


class AsyncCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_expires_and_task_can_continue(self) -> None:
        with self.assertRaises(asyncio.TimeoutError):
            async with timeout(0.02):
                await asyncio.Event().wait()
        async with timeout(1):
            await asyncio.sleep(0)

    async def test_external_cancellation_stays_cancelled_and_closes_stream(self) -> None:
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def stream():
            try:
                entered.set()
                yield "partial"
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def consume():
            async with timeout(10):
                async with aclosing(stream()) as chunks:
                    async for _ in chunks:
                        pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())
        self.assertTrue(closed.is_set())

    async def test_nested_timeout_only_expires_inner_scope(self) -> None:
        async with timeout(1):
            with self.assertRaises(asyncio.TimeoutError):
                async with timeout(0.02):
                    await asyncio.Event().wait()
            await asyncio.sleep(0)

    async def test_body_errors_are_not_converted_into_timeouts(self) -> None:
        with self.assertRaisesRegex(ValueError, "example"):
            async with timeout(1):
                raise ValueError("example")

    async def test_early_stream_close_runs_finally(self) -> None:
        closed = []

        async def stream():
            try:
                yield 1
                yield 2
            finally:
                closed.append(True)

        async with aclosing(stream()) as chunks:
            self.assertEqual(await chunks.__anext__(), 1)
        self.assertEqual(closed, [True])
