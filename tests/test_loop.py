from ish.compat import timeout
from typing import Optional
import asyncio
import importlib
import json
import os
import tempfile
import threading
import unittest
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from ish.core.models import MessageStatus, ProjectConfig, RunStatus, StepStatus
from ish.engines.base import EngineEventType, EngineRegistry
from tests.support.fake_engine import FakeStreamingEngine
from ish.engines.loop import LoopEngine
from ish.providers.litellm import stream_completion
from ish.components.tools import Tool, ToolRegistry
from ish.components.tools.component import ToolComponent
from ish.components.registry import ComponentRegistry
from ish.services.conversation import ConversationStore
from ish.services.projects import ProjectManager, ProjectRepository
from ish.services.runs import RunManager
from ish.services.tasks import TaskManager


def chunk(text: Optional[str] = None, *, calls: Optional[list] = None,
          finish: Optional[str] = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text, "tool_calls": calls},
                         "finish_reason": finish}]}


def call(arguments: str = '{"a":2,"b":3}', *, index: int = 0,
         name: str = "add", call_id: str = "call_1") -> dict[str, Any]:
    return {"index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class ScriptedCompletion:
    def __init__(self, *responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.threads: list[int] = []
        self.closed = 0

    def __call__(self, **kwargs: Any) -> Iterator[Any]:
        self.requests.append(deepcopy(kwargs))
        self.threads.append(threading.get_ident())
        response = self.responses.pop(0)
        try:
            for item in response:
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.closed += 1


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tasks = TaskManager()
        self.components = ComponentRegistry()
        self.projects = ProjectManager(ProjectRepository(Path(self.temporary.name)), self.tasks, components=self.components)
        self.project = self.projects.create("Loop project", config=ProjectConfig(default_engine="loop", completion={'model': "openai/test-model"}))
        self.task = self.tasks.create(self.project, "Loop task")
        self.store = ConversationStore(self.task.paths.conversation)
        self.registry = EngineRegistry()
        self.registry.register("fake", FakeStreamingEngine())
        self.events = []
        self.observer_errors = []
        self.addCleanup(lambda: self.assertEqual(self.observer_errors, []))
        self.manager = RunManager(self.tasks, self.registry, task=self.task, on_event=self.observe, capabilities=self.components)
        self.addAsyncCleanup(self.manager.shutdown)
        self.tool_arguments = []
        async def add(arguments: dict[str, Any]) -> dict[str, Any]:
            self.tool_arguments.append(arguments)
            return {"result": arguments["a"] + arguments["b"]}
        self.tool = Tool("add", "Add two numbers", {
            "type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"], "additionalProperties": False,
        }, add)

    def observe(self, run, event) -> None:
        self.events.append(event)
        if event.type == EngineEventType.TEXT_DELTA:
            # A UI sees only deltas already durably appended.
            if not self.store.get(run.assistant_message_id).content.endswith(event.text):
                self.observer_errors.append(event.text)

    async def until(self, predicate) -> None:
        async with timeout(10):
            while not predicate():
                await asyncio.sleep(0.005)

    def enable_tools(self, tools):
        self.components.register(ToolComponent(tools))
        self.projects.set_components(self.project, ("tools",))
        self.projects.configure_component(self.project, "tools", {"enabled": list(tools.names())})

    def engine(self, completion_fn, **kwargs) -> LoopEngine:
        tools = kwargs.pop("tools", None)
        if tools is not None:
            self.enable_tools(tools)
        engine = LoopEngine(completion_fn=completion_fn, **kwargs)
        self.registry.register("loop", engine)
        return engine

    async def submit(self, text: str = "request") -> None:
        await self.manager.submit(text)
        await asyncio.wait_for(self.manager.wait_idle(), timeout=15)

    def run_status(self) -> RunStatus:
        return self.manager.runs.list(self.task)[-1].status

    def output(self) -> str:
        run = self.manager.runs.list(self.task)[-1]
        return self.store.get(run.assistant_message_id).content

    async def test_real_time_deltas_and_request_configuration(self) -> None:
        release = threading.Event()
        closed = threading.Event()
        self.addCleanup(release.set)
        requests = []
        thread_ids = []
        def completion_fn(**kwargs):
            requests.append(kwargs)
            thread_ids.append(threading.get_ident())
            try:
                yield chunk("첫")
                release.wait(10)
                yield chunk(" 답변", finish="stop")
            finally:
                closed.set()
        self.project.config.completion["api_base"] = "http://localhost:8000/v1"
        self.projects.save(self.project)
        self.engine(completion_fn, completion_kwargs={"max_tokens": 100, "api_key": "secret-test-value"})
        with patch.dict(os.environ, {"ISH_TEST_API_KEY": "secret-test-value"}):
            await self.manager.submit("question")
            await self.until(lambda: any(event.type == EngineEventType.TEXT_DELTA for event in self.events))
            self.assertEqual(self.output(), "첫")
            self.assertEqual(self.run_status(), RunStatus.RUNNING)
            self.assertNotEqual(thread_ids[0], threading.get_ident())
            queued = await self.manager.submit("later", engine="fake")
            self.assertEqual(self.store.get(queued.id).status, MessageStatus.QUEUED)
            self.assertEqual(requests[0]["messages"], [{"role": "user", "content": "question"}])
            self.assertTrue(requests[0]["stream"])
            self.assertEqual(requests[0]["api_key"], "secret-test-value")
            self.assertEqual(requests[0]["api_base"], "http://localhost:8000/v1")
            self.assertEqual(requests[0]["num_retries"], 0)
            self.assertEqual(requests[0]["timeout"], 60)
            self.assertEqual(requests[0]["max_tokens"], 100)
            self.assertNotIn("temperature", requests[0])
            release.set()
            await self.manager.wait_idle()
        self.assertTrue(closed.is_set())
        first = self.manager.runs.list(self.task)[0]
        self.assertEqual(self.store.get(first.assistant_message_id).content, "첫 답변")
        for path in self.project.paths.root.rglob("*.json*"):
            self.assertNotIn("secret-test-value", path.read_text(encoding="utf-8"))

    async def test_free_completion_kwargs_override_defaults_without_persistence(self):
        completion_fn = ScriptedCompletion([chunk("answer", finish="stop")])
        params = {"model": "openai/override", "temperature": 0.15,
                  "top_p": 0.9, "max_tokens": 64, "num_retries": 2,
                  "timeout": 12, "api_key": "runtime-only-secret",
                  "response_format": {"type": "json_object"},
                  "provider_new_option": {"nested": [1, 2]}}
        self.engine(completion_fn, completion_kwargs=params, system_prompt="Be concise.")
        params["provider_new_option"]["nested"].append(3)
        await self.submit()
        request, = completion_fn.requests
        self.assertEqual(request["model"], "openai/override")
        self.assertEqual(request["temperature"], 0.15)
        self.assertEqual(request["provider_new_option"], {"nested": [1, 2]})
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["num_retries"], 2)
        self.assertEqual(request["timeout"], 12)
        self.assertEqual(request["messages"][0], {"role": "system", "content": "Be concise."})
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)
        for path in self.project.paths.root.rglob("*.json*"):
            self.assertNotIn("runtime-only-secret", path.read_text(encoding="utf-8"))
        self.assertEqual(len(self.store.list()), 2)

    async def test_completion_snapshot_is_fresh_per_iteration_and_factories_run_once(self):
        requests = []
        params = {"provider_option": {"values": [1]}}
        factories = []
        def factory(context):
            factories.append(context.run.id)
            return params
        def completion_fn(**kwargs):
            requests.append(deepcopy(kwargs))
            kwargs["provider_option"]["values"].append(99)
            if len(requests) == 1:
                params["provider_option"]["values"].append(2)
                yield chunk(calls=[call()], finish="tool_calls")
            else:
                yield chunk("done", finish="stop")
        self.engine(completion_fn, completion_kwargs=factory,
                    tools=ToolRegistry((self.tool,)), system_prompt="system")
        await self.submit()
        self.assertEqual(len(factories), 1)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request["provider_option"] == {"values": [1]} for request in requests))
        self.assertTrue(all(sum(message["role"] == "system" for message in request["messages"]) == 1
                            for request in requests))
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)

    async def test_reserved_completion_contract_is_rejected_before_provider_call(self):
        completion_fn = ScriptedCompletion()
        engine = self.engine(completion_fn)
        for params in ({"stream": False}, {"n": 2}, {"messages": []},
                       {"tools": []}, {"functions": []}, {"function_call": "auto"}):
            engine.completion_kwargs = params
            await self.submit()
            self.assertEqual(self.run_status(), RunStatus.FAILED)
        self.assertEqual(completion_fn.requests, [])

    async def test_explicit_model_works_without_project_model(self):
        self.project.config.completion["model"] = ""
        self.projects.save(self.project)
        completion_fn = ScriptedCompletion([chunk("done", finish="stop")])
        self.engine(completion_fn, completion_kwargs={"model": "openai/explicit"})
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)

    async def test_tool_choice_option_is_preserved_for_registered_tools(self):
        completion_fn = ScriptedCompletion([chunk("done", finish="stop")])
        self.engine(completion_fn, tools=ToolRegistry((self.tool,)),
                    completion_kwargs={"tool_choice": "none", "parallel_tool_calls": False})
        await self.submit()
        self.assertEqual(completion_fn.requests[0]["tool_choice"], "none")
        self.assertFalse(completion_fn.requests[0]["parallel_tool_calls"])
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)

    async def test_fragmented_tool_calls_execute_and_feed_next_iteration(self) -> None:
        completion_fn = ScriptedCompletion([
            chunk("계산 중 "),
            chunk(calls=[call('{"a":', name="ad", call_id="call_")]),
            chunk(calls=[{"index": 0, "id": "1", "function": {"name": "d", "arguments": "2,"}}]),
            chunk(calls=[{"index": 0, "function": {"arguments": '"b":3}'}}], finish="tool_calls"),
        ], [chunk("5", finish="stop"), {"choices": [], "usage": {"total_tokens": 10}}])
        self.engine(completion_fn, tools=ToolRegistry((self.tool,)))
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)
        self.assertEqual(self.output(), "계산 중 5")
        self.assertEqual(self.tool_arguments, [{"a": 2, "b": 3}])
        second = completion_fn.requests[1]["messages"]
        self.assertEqual([message["role"] for message in second], ["user", "assistant", "tool"])
        self.assertEqual(second[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(second[2]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(second[2]["content"]), {"result": 5})
        run = self.manager.runs.list(self.task)[0]
        steps = self.manager.steps.list(run)
        self.assertEqual([step.kind for step in steps], ["llm", "tool", "llm"])
        self.assertTrue(all(step.status == StepStatus.COMPLETED for step in steps))
        self.assertEqual(completion_fn.closed, 2)
        self.assertNotIn("arguments", steps[1].metadata)

    async def test_multiple_tool_calls_are_ordered_by_index(self) -> None:
        completion_fn = ScriptedCompletion([
            chunk(calls=[call('{"a":10,"b":20}', index=1, call_id="second"),
                         call(call_id="first")], finish="tool_calls"),
        ], [chunk("done", finish="stop")])
        self.engine(completion_fn, tools=ToolRegistry((self.tool,)))
        await self.submit()
        self.assertEqual(self.tool_arguments, [{"a": 2, "b": 3}, {"a": 10, "b": 20}])
        transcript = completion_fn.requests[1]["messages"]
        self.assertEqual([item["tool_call_id"] for item in transcript if item["role"] == "tool"],
                         ["first", "second"])

    async def test_invalid_batch_does_not_execute_even_first_valid_tool(self) -> None:
        completion_fn = ScriptedCompletion([
            chunk(calls=[call(), call('{"a":"wrong","b":3}', index=1, call_id="bad")],
                  finish="tool_calls"),
        ])
        self.engine(completion_fn, tools=ToolRegistry((self.tool,)))
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.FAILED)
        self.assertEqual(self.tool_arguments, [])

    async def test_unknown_tool_cannot_execute(self) -> None:
        completion_fn = ScriptedCompletion([chunk(calls=[call(name="unregistered")], finish="tool_calls")])
        self.engine(completion_fn, tools=ToolRegistry((self.tool,)))
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.FAILED)
        self.assertEqual(self.tool_arguments, [])

    async def test_loop_limit_prevents_unconsumable_tool_side_effects(self) -> None:
        completion_fn = ScriptedCompletion([chunk(calls=[call()], finish="tool_calls")])
        self.engine(completion_fn, tools=ToolRegistry((self.tool,)), max_iterations=1)
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.FAILED)
        self.assertEqual(len(completion_fn.requests), 1)
        self.assertEqual(self.tool_arguments, [])

    async def test_duplicate_call_id_is_not_executed_twice(self) -> None:
        completion_fn = ScriptedCompletion(
            [chunk(calls=[call()], finish="tool_calls")],
            [chunk(calls=[call()], finish="tool_calls")])
        self.engine(completion_fn, tools=ToolRegistry((self.tool,)))
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.FAILED)
        self.assertEqual(len(self.tool_arguments), 1)

    async def test_provider_failure_reports_diagnostics_and_queue_continues(self) -> None:
        completion_fn = ScriptedCompletion([chunk("partial"), RuntimeError("Authorization: private-key")])
        self.engine(completion_fn)
        await self.manager.submit("fail")
        await self.manager.submit("next", engine="fake")
        await self.manager.wait_idle()
        first, second = self.manager.runs.list(self.task)
        self.assertEqual(first.status, RunStatus.FAILED)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertEqual(self.store.get(first.assistant_message_id).content, "partial")
        self.assertEqual(self.manager.steps.list(first)[0].status, StepStatus.FAILED)
        self.assertEqual(first.error, "Authorization: private-key")

    async def test_tool_failure_reports_diagnostics_without_retry(self) -> None:
        async def failing(arguments):
            raise RuntimeError("tool failure detail")
        tool = Tool("add", "fail", self.tool.parameters, failing)
        completion_fn = ScriptedCompletion([chunk(calls=[call()], finish="tool_calls")])
        self.engine(completion_fn, tools=ToolRegistry((tool,)))
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.FAILED)
        run = self.manager.runs.list(self.task)[0]
        self.assertEqual([step.status for step in self.manager.steps.list(run)],
                         [StepStatus.COMPLETED, StepStatus.FAILED])
        self.assertEqual(len(completion_fn.requests), 1)
        self.assertEqual(run.error, "tool failure detail")

    async def test_interrupt_during_blocked_read_preserves_queue_and_drops_late_delta(self) -> None:
        release = threading.Event()
        closed = threading.Event()
        self.addCleanup(release.set)
        def completion_fn(**kwargs):
            try:
                yield chunk("partial")
                release.wait(10)
                yield chunk("late", finish="stop")
            finally:
                closed.set()
        self.engine(completion_fn)
        await self.manager.submit("first")
        await self.until(lambda: any(event.type == EngineEventType.TEXT_DELTA for event in self.events))
        await self.manager.submit("next", engine="fake")
        self.assertTrue(await asyncio.wait_for(self.manager.interrupt(), 1))
        await self.manager.wait_idle()
        first, second = self.manager.runs.list(self.task)
        self.assertEqual(first.status, RunStatus.INTERRUPTED)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertEqual(self.manager.steps.list(first)[0].status, StepStatus.INTERRUPTED)
        release.set()
        await self.until(closed.is_set)
        self.assertEqual(self.store.get(first.assistant_message_id).content, "partial")

    async def test_cancel_during_completion_creation_closes_late_stream(self) -> None:
        release = threading.Event()
        entered = threading.Event()
        closed = threading.Event()
        self.addCleanup(release.set)
        class LateStream:
            def __iter__(self):
                return self
            def __next__(self):
                raise AssertionError("Cancelled stream must not be consumed")
            def close(self):
                closed.set()
        def completion_fn(**kwargs):
            entered.set()
            release.wait(10)
            return LateStream()
        self.engine(completion_fn)
        await self.manager.submit("first")
        await self.until(entered.is_set)
        await asyncio.wait_for(self.manager.interrupt(), 1)
        self.assertEqual(self.run_status(), RunStatus.INTERRUPTED)
        release.set()
        await self.until(closed.is_set)

    async def test_request_timeout_stops_delivery(self) -> None:
        release = threading.Event()
        closed = threading.Event()
        self.addCleanup(release.set)
        def completion_fn(**kwargs):
            try:
                yield chunk("partial")
                release.wait(10)
                yield chunk("late", finish="stop")
            finally:
                closed.set()
        self.engine(completion_fn, request_timeout=0.2)
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.FAILED)
        self.assertEqual(self.output(), "partial")
        release.set()
        await self.until(closed.is_set)

    async def test_tool_cancellation_keeps_worker_alive(self) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def blocking(arguments):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        tool = Tool("add", "blocking", self.tool.parameters, blocking)
        completion_fn = ScriptedCompletion([chunk(calls=[call()], finish="tool_calls")])
        self.engine(completion_fn, tools=ToolRegistry((tool,)))
        await self.manager.submit("first")
        await asyncio.wait_for(entered.wait(), 10)
        await self.manager.submit("next", engine="fake")
        await self.manager.interrupt()
        await self.manager.wait_idle()
        first, second = self.manager.runs.list(self.task)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(first.status, RunStatus.INTERRUPTED)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertEqual(self.manager.steps.list(first)[-1].status, StepStatus.INTERRUPTED)
        self.assertEqual(len(completion_fn.requests), 1)

    async def test_tool_timeout_fails_without_next_llm_call(self) -> None:
        async def blocking(arguments):
            await asyncio.Event().wait()
        tool = Tool("add", "blocking", self.tool.parameters, blocking)
        completion_fn = ScriptedCompletion([chunk(calls=[call()], finish="tool_calls")])
        self.engine(completion_fn, tools=ToolRegistry((tool,)), tool_timeout=0.05)
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.FAILED)
        self.assertEqual(len(completion_fn.requests), 1)

    async def test_no_finish_reason_fails_instead_of_returning_partial_success(self) -> None:
        self.engine(ScriptedCompletion([chunk("partial")]))
        await self.submit()
        self.assertEqual(self.output(), "partial")
        self.assertEqual(self.run_status(), RunStatus.FAILED)

    async def test_token_limit_retains_last_delta_and_fails(self) -> None:
        self.engine(ScriptedCompletion([chunk("partial", finish="length")]))
        await self.submit()
        self.assertEqual(self.output(), "partial")
        self.assertEqual(self.run_status(), RunStatus.FAILED)

    async def test_sdk_environment_authentication_is_left_to_provider(self) -> None:
        provider = ScriptedCompletion([chunk("answer", finish="stop")])
        self.engine(provider)
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)
        self.assertNotIn("api_key", provider.requests[0])

    async def test_object_chunks_empty_content_and_usage_are_supported(self) -> None:
        object_chunk = SimpleNamespace(choices=[SimpleNamespace(
            index=0, delta=SimpleNamespace(content="answer", tool_calls=None), finish_reason="stop")])
        self.engine(ScriptedCompletion([chunk(), object_chunk, {"choices": []}]))
        await self.submit()
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)
        self.assertEqual(self.output(), "answer")

    async def test_two_tasks_stream_concurrently_on_separate_threads(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        entered = []
        def completion_fn(**kwargs):
            entered.append(threading.get_ident())
            yield chunk("partial")
            release.wait(10)
            yield chunk("done", finish="stop")
        self.engine(completion_fn)
        # The default observer asserts the primary Task's conversation only.
        self.manager.on_event = None
        other = self.tasks.create(self.project, "other")
        other_manager = RunManager(self.tasks, self.registry, task=other, capabilities=self.components)
        self.addAsyncCleanup(other_manager.shutdown)
        await self.manager.submit("first")
        await other_manager.submit("second")
        await self.until(lambda: len(entered) == 2)
        self.assertEqual(len(set(entered)), 2)
        release.set()
        await asyncio.gather(self.manager.wait_idle(),
                             other_manager.wait_idle())
        self.assertEqual(self.manager.runs.list(other)[0].status, RunStatus.COMPLETED)
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)

    async def test_real_litellm_sdk_with_mock_http_sse_and_tool_loop(self) -> None:
        # Exercise the installed SDK and its actual stream wrapper, but route
        # every model request through an in-memory HTTP transport.
        # Token accounting is outside this test. A local byte vocabulary avoids
        # SDK import-time tokenizer downloads on a clean machine.
        import tiktoken
        encoding = tiktoken.Encoding(name="offline-test", pat_str=r"(?s).",
                                     mergeable_ranks={bytes([i]): i for i in range(256)},
                                     special_tokens={})
        for name in ("get_encoding", "encoding_for_model"):
            patcher = patch.object(tiktoken, name, return_value=encoding)
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
            litellm = await asyncio.to_thread(importlib.import_module, "litellm")
        import httpx
        from openai import OpenAI

        requests = []
        closed = []
        class SSE(httpx.SyncByteStream):
            def __init__(self, events):
                self.events = events
            def __iter__(self):
                for event in self.events:
                    value = {"id": "chatcmpl-offline", "object": "chat.completion.chunk",
                             "created": 1, "model": "test-model", **event}
                    yield ("data: " + json.dumps(value) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
            def close(self):
                closed.append(True)
        def handle(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                events = [chunk(calls=[call('{"a":2,')]),
                          chunk(calls=[{"index": 0, "function": {"arguments": '"b":3}'}}],
                                finish="tool_calls")]
            else:
                events = [chunk("The result is "), chunk("5."), chunk(finish="stop")]
            events.append({"choices": [], "usage": {
                "prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5,
            }})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SSE(events))
        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
            client = OpenAI(api_key="offline-test", base_url="https://llm.invalid/v1",
                            max_retries=0, http_client=http_client)
            self.enable_tools(ToolRegistry((self.tool,)))
            self.registry.register("loop", LoopEngine(completion_kwargs={
                "client": client, "top_p": 0.8, "max_tokens": 32,
            }))
            await self.submit("Add 2 and 3")
        self.assertEqual(self.run_status(), RunStatus.COMPLETED)
        self.assertEqual(self.output(), "The result is 5.")
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request["stream"] for request in requests))
        self.assertTrue(all(request["top_p"] == 0.8 for request in requests))
        self.assertTrue(all(request["max_tokens"] == 32 for request in requests))
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(len(closed), 2)
        result = self.projects.results.load(self.project, self.manager.runs.list(self.task)[0].id)
        self.assertEqual(result.total_tokens, 10)
        self.assertEqual(result.finish_reasons, ["tool_calls", "stop"])


class StreamBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_buffer_and_consumer_close(self) -> None:
        closed = threading.Event()
        produced = []
        def completion_fn(**kwargs):
            try:
                while True:
                    produced.append(1)
                    yield chunk("delta")
            finally:
                closed.set()
        stream = stream_completion({}, completion_fn=completion_fn, buffer_size=1)
        try:
            await asyncio.wait_for(stream.__anext__(), 5)
            await asyncio.sleep(0.05)
            # One consumed chunk, one buffered chunk, one pending put.
            self.assertLessEqual(len(produced), 3)
        finally:
            await stream.aclose()
        async with timeout(5):
            while not closed.is_set():
                await asyncio.sleep(0.005)


class ConfigurationTests(unittest.TestCase):
    def test_invalid_limits(self) -> None:
        for kwargs in ({"max_iterations": 0}, {"max_iterations": True},
                       {"request_timeout": float("inf")}, {"request_timeout": True},
                       {"buffer_size": 0}, {"tool_timeout": -1},
                       {"max_tool_calls": 0}, {"max_output_chars": False},
                       {"max_argument_chars": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LoopEngine(**kwargs)


    def test_schema_and_nonfinite_arguments_rejected(self) -> None:
        async def handler(arguments):
            return arguments
        with self.assertRaises(ValueError):
            ToolRegistry((Tool("x", "x", {"type": "object", "$ref": "https://example.com/schema"}, handler),))
        registry = ToolRegistry((Tool("x", "x", {"type": "object"}, handler),))
        for arguments in ('{"x":NaN}', '{"x":1e999}', '[]', 'broken'):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                registry.prepare("x", arguments)
