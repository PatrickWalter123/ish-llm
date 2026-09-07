import asyncio
import tempfile
import unittest
from pathlib import Path

from ish.core.models import ProjectConfig, RunStatus, StepStatus, new_id
from ish.engines.base import EngineEvent, EngineEventType, EngineRegistry
from ish.engines.loop import LoopEngine
from ish.engines.pipeline import PipelineEngine, PreparationStep
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager
from tests.support.fake_engine import FakeStreamingEngine
from tests.test_loop import ScriptedCompletion, chunk


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(Path(temporary.name)), self.tasks)
        self.project = self.projects.create("Pipeline", config=ProjectConfig(default_engine="pipeline"))
        self.task = self.tasks.create(self.project, "Task")
        self.engines = EngineRegistry()
        self.fake = FakeStreamingEngine()
        self.engines.register("fake", self.fake)
        self.manager = RunManager(self.tasks, self.engines)
        self.addAsyncCleanup(self.manager.shutdown)

    async def idle(self, task=None):
        await asyncio.wait_for(self.manager.wait_idle(self.project, task or self.task), 10)

    async def test_preparations_feed_loop_factories_and_have_persistent_steps(self):
        order = []
        async def read(context):
            order.append("read")
            context.state["document"] = "retrieved material"
            context.state["env"] = {"PRIVATE_TEST": "runtime-preparation-secret"}
        async def configure(context):
            order.append("configure")
            self.assertEqual(context.state["document"], "retrieved material")
            context.state["model"] = "openai/prepared"
        completion = ScriptedCompletion([chunk("answer", finish="stop")])
        loop = LoopEngine(
            completion_fn=completion,
            completion_kwargs=lambda ctx: {"model": ctx.state["model"], "top_p": 0.8},
            system_prompt=lambda ctx: "Use: " + ctx.state["document"],
        )
        self.engines.register("pipeline", PipelineEngine([
            PreparationStep("Read documents", read, kind="retrieval"),
            PreparationStep("Prepare environment", configure, kind="shell"), loop,
        ]))
        await self.manager.submit(self.project, self.task, "question")
        await self.idle()
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        steps = self.manager.steps.list(run)
        self.assertEqual([step.kind for step in steps], ["retrieval", "shell", "llm"])
        self.assertTrue(all(step.status == StepStatus.COMPLETED for step in steps))
        self.assertEqual(order, ["read", "configure"])
        request, = completion.requests
        self.assertEqual(request["model"], "openai/prepared")
        self.assertEqual(request["messages"][0]["content"], "Use: retrieved material")
        for path in self.project.paths.root.rglob("*.json*"):
            self.assertNotIn("runtime-preparation-secret", path.read_text(encoding="utf-8"))

    async def test_preparation_failure_stops_loop_and_preserves_next_request(self):
        async def fail(context):
            raise RuntimeError("private-preparation-error")
        self.engines.register("pipeline", PipelineEngine([PreparationStep("Prepare", fail), self.fake]))
        await self.manager.submit(self.project, self.task, "fail")
        await self.manager.submit(self.project, self.task, "next", engine="fake")
        await self.idle()
        first, second = self.manager.runs.list(self.task)
        self.assertEqual([first.status, second.status], [RunStatus.FAILED, RunStatus.COMPLETED])
        self.assertEqual(self.manager.steps.list(first)[0].status, StepStatus.FAILED)
        self.assertEqual([ctx.messages[-1].content for ctx in self.fake.contexts], ["next"])
        for path in self.project.paths.root.rglob("*.json*"):
            self.assertNotIn("private-preparation-error", path.read_text(encoding="utf-8"))

    async def test_interrupt_preparation_closes_action_and_only_next_request_runs_loop(self):
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def prepare(context):
            if context.messages[-1].content == "first":
                entered.set()
                try:
                    await release.wait()
                finally:
                    closed.set()
        self.engines.register("pipeline", PipelineEngine([PreparationStep("Prepare", prepare), self.fake]))
        await self.manager.submit(self.project, self.task, "first")
        await asyncio.wait_for(entered.wait(), 5)
        await self.manager.submit(self.project, self.task, "second")
        self.assertTrue(await self.manager.interrupt(self.project, self.task))
        await self.idle()
        first, second = self.manager.runs.list(self.task)
        self.assertTrue(closed.is_set())
        self.assertEqual(first.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.manager.steps.list(first)[0].status, StepStatus.INTERRUPTED)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertEqual([ctx.messages[-1].content for ctx in self.fake.contexts], ["second"])

    async def test_timeout_stops_preparation_and_closes_action(self):
        closed = asyncio.Event()
        async def prepare(context):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        self.engines.register("pipeline", PipelineEngine([
            PreparationStep("Prepare", prepare, timeout_seconds=0.02), self.fake,
        ]))
        await self.manager.submit(self.project, self.task, "request")
        await self.idle()
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.fake.contexts, [])

    async def test_shared_pipeline_has_isolated_state_for_concurrent_tasks_and_later_runs(self):
        entered, release = asyncio.Event(), asyncio.Event()
        states = []
        async def prepare(context):
            self.assertEqual(context.state, {})
            context.state["input"] = context.messages[-1].content
            states.append(context.state)
            if len(states) == 2:
                entered.set()
            await release.wait()
        completion = ScriptedCompletion(*[[chunk("done", finish="stop")] for _ in range(3)])
        self.engines.register("pipeline", PipelineEngine([
            PreparationStep("Prepare", prepare),
            LoopEngine(completion_fn=completion, completion_kwargs={"model": "openai/test"},
                       system_prompt=lambda ctx: ctx.state["input"]),
        ]))
        other = self.tasks.create(self.project, "Other")
        await self.manager.submit(self.project, self.task, "one")
        await self.manager.submit(self.project, other, "two")
        await asyncio.wait_for(entered.wait(), 5)
        release.set()
        await asyncio.gather(self.idle(), self.idle(other))
        await self.manager.submit(self.project, self.task, "three")
        await self.idle()
        self.assertEqual(len({id(state) for state in states}), 3)
        self.assertEqual({request["messages"][0]["content"] for request in completion.requests},
                         {"one", "two", "three"})

    async def test_failed_event_and_unfinished_stage_stop_following_stages(self):
        closed = []
        class BadStage:
            def __init__(self, fail):
                self.fail = fail
            async def execute(self, context):
                step = new_id()
                try:
                    yield EngineEvent(EngineEventType.STEP_STARTED, step_id=step)
                    if self.fail:
                        yield EngineEvent(EngineEventType.STEP_FAILED, step_id=step, error="Failed")
                        raise AssertionError("Pipeline must stop consuming after failure")
                finally:
                    closed.append(True)
        for fail in (True, False):
            name = "bad-" + str(fail)
            self.engines.register(name, PipelineEngine([BadStage(fail), self.fake]))
            await self.manager.submit(self.project, self.task, "request", engine=name)
            await self.idle()
        self.assertEqual(closed, [True, True])
        self.assertEqual(self.fake.contexts, [])
        self.assertTrue(all(run.status == RunStatus.FAILED for run in self.manager.runs.list(self.task)))

    async def test_nested_pipelines_compose_with_generic_async_iterators(self):
        order = []
        async def prepare(context):
            order.append("prepare")
        self.engines.register("pipeline", PipelineEngine([
            PipelineEngine([PreparationStep("Prepare", prepare, timeout_seconds=None)]), self.fake,
        ]))
        await self.manager.submit(self.project, self.task, "request")
        await self.idle()
        self.assertEqual(order, ["prepare"])
        self.assertEqual(self.manager.runs.list(self.task)[0].status, RunStatus.COMPLETED)


class PipelineConfigurationTests(unittest.TestCase):
    def test_invalid_pipeline_and_preparation_configuration(self):
        for stages in ([], [object()]):
            with self.assertRaises(ValueError):
                PipelineEngine(stages)
        async def action(context):
            pass
        for seconds in (0, -1, float("inf"), True):
            with self.assertRaises(ValueError):
                PreparationStep("Prepare", action, timeout_seconds=seconds)
