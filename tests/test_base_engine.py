import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ish.compat import aclosing
from ish.core.models import ProjectConfig, Run, RunStatus, StepStatus, new_id
from ish.engines import EngineContext, EngineRegistry, BaseEngine
from ish.engines.base import EngineEventType
from ish.services.conversation import ConversationStore
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager
from tests.test_loop import ScriptedCompletion, call, chunk


class BaseEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tasks = TaskManager()
        self.projects = ProjectManager(ProjectRepository(Path(temporary.name)), self.tasks)
        self.project = self.projects.create("Simple", config=ProjectConfig(default_engine="simple"))
        self.task = self.tasks.create(self.project, "Task")
        self.engines = EngineRegistry()
        self.manager = RunManager(self.tasks, self.engines)
        self.addAsyncCleanup(self.manager.shutdown)

    async def idle(self, task=None):
        await asyncio.wait_for(self.manager.wait_idle(self.project, task or self.task), 10)

    def context(self):
        run_id = new_id()
        run = Run(run_id, self.task.id, new_id(), new_id(), "simple",
                  self.manager.runs.paths(self.task, run_id))
        return EngineContext(self.project, self.task, run, ())

    async def test_subclass_reuses_completion_with_automatic_step_persistence(self):
        class Chat(BaseEngine):
            def run(self, context):
                return self.stream_completion({"model": "openai/test", "messages": [
                    {"role": "user", "content": context.messages[-1].content},
                ]})
        provider = ScriptedCompletion([chunk("Hello "), chunk("there", finish="stop")])
        self.engines.register("simple", Chat("Answer", kind="llm", completion_fn=provider))
        await self.manager.submit(self.project, self.task, "hello")
        await self.idle()
        run, = self.manager.runs.list(self.task)
        step, = self.manager.steps.list(run)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(step.status, StepStatus.COMPLETED)
        self.assertEqual(step.kind, "llm")
        self.assertEqual(ConversationStore(self.task.paths.conversation).get(
            run.assistant_message_id).content, "Hello there")
        self.assertTrue(provider.requests[0]["stream"])
        self.assertEqual(provider.closed, 1)

    async def test_subclass_yields_text_and_records_one_completed_step(self):
        class Echo(BaseEngine):
            async def run(self, context):
                yield "Echo: "
                yield ""
                yield context.messages[-1].content
        self.engines.register("simple", Echo("Echo", kind="text"))
        await self.manager.submit(self.project, self.task, "hello")
        await self.idle()
        run, = self.manager.runs.list(self.task)
        step, = self.manager.steps.list(run)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(step.status, StepStatus.COMPLETED)
        self.assertEqual(step.name, "Echo")
        self.assertEqual(step.kind, "text")
        self.assertEqual(ConversationStore(self.task.paths.conversation).get(
            run.assistant_message_id).content, "Echo: hello")

    async def test_coroutine_action_stores_private_state_without_emitting_text(self):
        seen = []
        async def prepare(context):
            context.state["secret"] = "private-action-value"
            seen.append(context.state)
        self.engines.register("simple", BaseEngine("Prepare", action=prepare))
        await self.manager.submit(self.project, self.task, "hello")
        await self.idle()
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(seen, [{"secret": "private-action-value"}])
        for path in self.project.paths.root.rglob("*.json*"):
            self.assertNotIn("private-action-value", path.read_text(encoding="utf-8"))

    async def test_partial_failure_is_sanitized_closes_source_and_continues_queue(self):
        closed = []
        async def respond(context):
            try:
                yield "partial"
                if context.messages[-1].content == "bad":
                    raise ValueError("private-sdk-error")
                yield " done"
            finally:
                closed.append(True)
        self.engines.register("simple", BaseEngine("Respond", action=respond))
        await self.manager.submit(self.project, self.task, "bad")
        await self.manager.submit(self.project, self.task, "good")
        await self.idle()
        first, second = self.manager.runs.list(self.task)
        self.assertEqual(first.status, RunStatus.FAILED)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertEqual(self.manager.steps.list(first)[0].error, "Step execution failed")
        self.assertEqual(ConversationStore(self.task.paths.conversation).get(
            first.assistant_message_id).content, "partial")
        self.assertEqual(closed, [True, True])
        for path in self.project.paths.root.rglob("*.json*"):
            self.assertNotIn("private-sdk-error", path.read_text(encoding="utf-8"))

    async def test_interrupt_closes_stream_and_preserves_queued_request(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def respond(context):
            try:
                yield "partial"
                if context.messages[-1].content == "first":
                    entered.set()
                    await asyncio.Event().wait()
            finally:
                closed.set()
        self.engines.register("simple", BaseEngine("Respond", action=respond))
        await self.manager.submit(self.project, self.task, "first")
        await asyncio.wait_for(entered.wait(), 5)
        await self.manager.submit(self.project, self.task, "second")
        self.assertTrue(await self.manager.interrupt(self.project, self.task))
        await self.idle()
        first, second = self.manager.runs.list(self.task)
        self.assertEqual(first.status, RunStatus.INTERRUPTED)
        self.assertEqual(self.manager.steps.list(first)[0].status, StepStatus.INTERRUPTED)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertTrue(closed.is_set())

    async def test_timeout_finalizes_failed_step_and_closes_coroutine(self):
        closed = asyncio.Event()
        async def action(context):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        self.engines.register("simple", BaseEngine("Wait", action=action, timeout_seconds=0.02))
        await self.manager.submit(self.project, self.task, "request")
        await self.idle()
        run, = self.manager.runs.list(self.task)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(self.manager.steps.list(run)[0].status, StepStatus.FAILED)
        self.assertTrue(closed.is_set())

    async def test_shared_engine_has_unique_steps_and_isolated_metadata_and_state(self):
        states = []
        entered, release = asyncio.Event(), asyncio.Event()
        async def action(context):
            self.assertEqual(context.state, {})
            context.state["task"] = context.task.id
            states.append(context.state)
            if len(states) == 2:
                entered.set()
            await release.wait()
        metadata = {"labels": ["fixed"]}
        engine = BaseEngine("Shared", action=action, metadata=metadata)
        metadata["labels"].append("external")
        self.engines.register("simple", engine)
        other = self.tasks.create(self.project, "Other")
        await self.manager.submit(self.project, self.task, "one")
        await self.manager.submit(self.project, other, "two")
        await asyncio.wait_for(entered.wait(), 5)
        release.set()
        await asyncio.gather(self.idle(), self.idle(other))
        steps = [self.manager.steps.list(self.manager.runs.list(task)[0])[0]
                 for task in (self.task, other)]
        self.assertNotEqual(steps[0].id, steps[1].id)
        self.assertIsNot(states[0], states[1])
        self.assertTrue(all(step.metadata == {"labels": ["fixed"]} for step in steps))

    async def test_close_after_started_does_not_invoke_action_or_emit_terminal_event(self):
        called = []
        async def action(context):
            called.append(True)
        stream = BaseEngine(action=action).execute(self.context())
        self.assertEqual((await stream.__anext__()).type, EngineEventType.STEP_STARTED)
        await stream.aclose()
        self.assertEqual(called, [])
        with self.assertRaises(StopAsyncIteration):
            await stream.__anext__()

    async def test_close_after_text_closes_underlying_generator(self):
        closed = []
        async def action(context):
            try:
                yield "partial"
                raise AssertionError("must not advance after close")
            finally:
                closed.append(True)
        stream = BaseEngine(action=action).execute(self.context())
        await stream.__anext__()
        self.assertEqual((await stream.__anext__()).text, "partial")
        await stream.aclose()
        self.assertEqual(closed, [True])

    async def test_invalid_output_fails_without_persisting_raw_objects(self):
        async def wrong_generator(context):
            yield {"private": "response"}
        async def wrong_coroutine(context):
            return "use yield instead"
        for index, action in enumerate((wrong_generator, wrong_coroutine)):
            name = str(index)
            self.engines.register(name, BaseEngine(action=action))
            await self.manager.submit(self.project, self.task, "request", engine=name)
            await self.idle()
            run = self.manager.runs.list(self.task)[-1]
            self.assertEqual(run.status, RunStatus.FAILED)
            self.assertEqual(ConversationStore(self.task.paths.conversation).get(
                run.assistant_message_id).content, "")

    async def test_generic_async_iterator_and_close_failure(self):
        class Iterator:
            def __init__(self):
                self.used = False
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self.used:
                    raise StopAsyncIteration
                self.used = True
                return "hello"
        engine = BaseEngine(action=lambda context: Iterator())
        events = [event async for event in engine.execute(self.context())]
        self.assertEqual([event.type for event in events], [EngineEventType.STEP_STARTED,
                         EngineEventType.TEXT_DELTA, EngineEventType.STEP_COMPLETED])
        class BadClose(Iterator):
            async def aclose(self):
                raise ValueError("private-close-error")
        events = []
        with self.assertRaisesRegex(RuntimeError, "Step execution failed"):
            async for event in BaseEngine(action=lambda context: BadClose()).execute(self.context()):
                events.append(event)
        self.assertEqual(events[-1].type, EngineEventType.STEP_FAILED)
        self.assertNotIn(EngineEventType.STEP_COMPLETED, [event.type for event in events])

    async def test_cleanup_failure_during_close_does_not_yield_failure_event(self):
        closed = []
        class Source:
            def __aiter__(self):
                return self
            async def __anext__(self):
                return "text"
            async def aclose(self):
                closed.append(True)
                raise ValueError("private-close-error")
        stream = BaseEngine(action=lambda ctx: Source()).execute(self.context())
        await stream.__anext__()
        await stream.__anext__()
        await stream.aclose()
        self.assertEqual(closed, [True])
        with self.assertRaises(StopAsyncIteration):
            await stream.__anext__()

    async def test_unimplemented_operation_is_a_failed_step(self):
        events = []
        with self.assertRaisesRegex(RuntimeError, "Step execution failed"):
            async for event in BaseEngine().execute(self.context()):
                events.append(event)
        self.assertEqual([event.type for event in events],
                         [EngineEventType.STEP_STARTED, EngineEventType.STEP_FAILED])


class StepConfigurationTests(unittest.TestCase):
    def test_invalid_persistent_metadata_is_rejected_before_execution(self):
        for metadata in ({"not_json": object()}, {"nonfinite": float("nan")}, ["wrong"]):
            with self.assertRaises((TypeError, ValueError)):
                BaseEngine(metadata=metadata)


class CompletionHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_chunks_and_interleaved_tool_fragments_build_assistant_message(self):
        provider = ScriptedCompletion([
            SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=None,
                delta=SimpleNamespace(content="Thinking", tool_calls=None))]),
            chunk(calls=[call('{"b":', index=1, name="second", call_id="id_2"),
                         call('{"a":', name="first", call_id="id_1")]),
            chunk(calls=[{"index": 0, "function": {"arguments": "1}"}},
                         {"index": 1, "function": {"arguments": "2}"}}], finish="tool_calls"),
            {"choices": [], "usage": {"total_tokens": 4}},
        ])
        response = {}
        engine = BaseEngine(completion_fn=provider)
        text = [part async for part in engine.stream_completion(include_events=False, request={"model": "test"}, response=response)]
        self.assertEqual(text, ["Thinking"])
        self.assertEqual(response["role"], "assistant")
        self.assertEqual(response["content"], "Thinking")
        self.assertEqual([item["id"] for item in response["tool_calls"]], ["id_1", "id_2"])
        self.assertEqual([item["function"]["arguments"] for item in response["tool_calls"]],
                         ['{"a":1}', '{"b":2}'])
        self.assertEqual(provider.closed, 1)

    async def test_incomplete_or_oversized_stream_does_not_publish_complete_response(self):
        cases = [
            ({}, [chunk("partial")]),
            ({}, [chunk("partial", finish="length")]),
            ({"max_output_chars": 2}, [chunk("abc", finish="stop")]),
            ({"max_argument_chars": 2}, [chunk(calls=[call()], finish="tool_calls")]),
            ({"max_tool_calls": 1}, [chunk(calls=[call(index=1)], finish="tool_calls")]),
            ({}, [chunk(calls=[call(), call(index=1)], finish="tool_calls")]),
        ]
        for limits, chunks in cases:
            with self.subTest(limits=limits, chunks=chunks):
                response = {}
                provider = ScriptedCompletion(chunks)
                engine = BaseEngine(completion_fn=provider, **limits)
                with self.assertRaises(ValueError):
                    async for _ in engine.stream_completion(include_events=False, request={}, response=response):
                        pass
                self.assertEqual(response, {})

    async def test_simultaneous_streams_keep_results_and_request_copies_isolated(self):
        client = object()
        original = {"model": "test", "client": client,
                    "messages": [{"role": "user", "content": "shared"}]}
        def provider(**request):
            if request["client"] is not client:
                raise AssertionError("SDK client identity changed")
            label = request["label"]
            request["messages"][0]["content"] = label
            yield chunk(label)
            yield chunk("!", finish="stop")
        engine = BaseEngine(completion_fn=provider)
        async def collect(label):
            response = {}
            text = [part async for part in engine.stream_completion(include_events=False, request=
                dict(original, label=label), response=response)]
            return text, response
        first, second = await asyncio.gather(collect("first"), collect("second"))
        self.assertEqual(first, (["first", "!"], {"role": "assistant", "content": "first!"}))
        self.assertEqual(second, (["second", "!"], {"role": "assistant", "content": "second!"}))
        self.assertEqual(original["messages"][0]["content"], "shared")
        self.assertNotIn("stream", original)

    async def test_early_close_closes_provider_and_leaves_response_unfinished(self):
        closed = threading.Event()
        def provider(**request):
            try:
                while True:
                    yield chunk("delta")
            finally:
                closed.set()
        response = {}
        engine = BaseEngine(completion_fn=provider, buffer_size=1)
        async with aclosing(engine.stream_completion(include_events=False, request={}, response=response)) as stream:
            self.assertEqual(await stream.__anext__(), "delta")
        self.assertTrue(await asyncio.to_thread(closed.wait, 5))
        self.assertEqual(response, {})

    async def test_nonstreaming_or_multiple_choices_rejected_before_provider_call(self):
        provider = ScriptedCompletion([])
        engine = BaseEngine(completion_fn=provider)
        for request in ({"stream": False}, {"n": 2}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                async for _ in engine.stream_completion(include_events=False, request=request):
                    pass
        self.assertEqual(provider.requests, [])
