"""Engine contract, Step event lifecycle, and reusable LiteLLM streaming."""

import asyncio
import inspect
import json
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import field, replace
from typing import Any, Optional, Protocol, Union

from ish.compat import StrEnum, aclosing, dataclass, timeout
from ish.core.models import Message, Project, Run, RunStatus, Task, new_id, now
from ish.core.results import CompletionResult
from ish.components.tools import ToolRegistry
from ish.providers.litellm import completion, stream_completion
from ish.providers.parameters import copy_params


class EngineEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    COMPLETION = "completion"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_INTERRUPTED = "step_interrupted"
    STEP_CANCELLED = "step_cancelled"


@dataclass(frozen=True, slots=True)
class EngineEvent:
    type: EngineEventType
    text: str = ""
    step_id: Optional[str] = None
    kind: str = "llm"
    name: str = ""
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None
    completion: Optional[CompletionResult] = None


@dataclass(frozen=True, slots=True)
class EngineContext:
    project: Project
    task: Task
    run: Run
    messages: tuple[Message, ...]
    # Per-Run snapshot of Project capabilities, never part of persisted models.
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    # Preparation outputs and handles for this Run only; never persisted.
    state: dict[str, Any] = field(default_factory=dict)

    def settings(self, engine: str) -> dict:
        return self.project.config.for_engine(engine, self.task.config)


class Engine(Protocol):
    def execute(self, context: EngineContext) -> AsyncIterator[EngineEvent]: ...


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}

    def register(self, name: str, engine: Engine) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Engine name must be a nonempty string")
        if not callable(getattr(engine, "execute", None)):
            raise TypeError("Engine must implement execute(context)")
        if name in self._engines:
            raise ValueError(f"Engine already registered: {name}")
        self._engines[name] = engine

    def names(self) -> tuple[str, ...]:
        return tuple(self._engines)

    def resolve(self, name: str) -> Engine:
        return self._engines[name]


class BaseEngine:
    """Adapt a developer operation to the existing Engine event contract.

    Override run(context), or pass action=. Async generators yield strings;
    async functions perform work without emitting response text. Every execute
    call gets a new Step ID. No execution state or result is kept on this object.
    Labels/metadata/error_message are trusted, persistable developer constants.
    """

    def __init__(self, name: str = "Step", *, kind: str = "custom",
                 action: Optional[Callable[[EngineContext],
                     Union[AsyncIterator[Union[str, EngineEvent]], Awaitable[None]]]] = None,
                 timeout_seconds: Optional[float] = None,
                 metadata: Optional[dict] = None,
                 error_message: str = "Step execution failed",
                 completion_fn: Callable[..., Iterator[Any]] = completion,
                 buffer_size: int = 8, max_tool_calls: int = 16,
                 max_argument_chars: int = 65536,
                 max_output_chars: int = 1_000_000) -> None:
        if not isinstance(name, str) or not name.strip() or not isinstance(kind, str) or not kind.strip():
            raise ValueError("Step requires a name and kind")
        if action is not None and not callable(action):
            raise TypeError("Step action must be callable")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ValueError("Step timeout must be positive and finite, or None")
        if not isinstance(error_message, str) or not error_message.strip():
            raise ValueError("Step error message must be a nonempty string")
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("Step metadata must be a dictionary")
        json.dumps(metadata or {}, allow_nan=False)
        self.name, self.kind = name, kind
        self.action = action
        self.timeout_seconds = timeout_seconds
        self.metadata = deepcopy(metadata or {})
        self.error_message = error_message
        for value in (buffer_size, max_tool_calls, max_argument_chars, max_output_chars):
            if type(value) is not int or value < 1:
                raise ValueError("Completion limits must be positive integers")
        if not callable(completion_fn):
            raise TypeError("completion_fn must be callable")
        self.completion_fn = completion_fn
        self.buffer_size = buffer_size
        self.max_tool_calls = max_tool_calls
        self.max_argument_chars = max_argument_chars
        self.max_output_chars = max_output_chars

    def step(self, context: EngineContext,
             action: Callable[[EngineContext], Union[AsyncIterator[Union[str, EngineEvent]], Awaitable[None]]], *,
             name: str, kind: str = "custom", timeout_seconds: Optional[float] = None,
             metadata: Optional[dict] = None,
             error_message: str = "Step execution failed") -> AsyncIterator[EngineEvent]:
        """Wrap one operation when an Engine needs several Steps in execute().

        Consume under aclosing() so early exit closes the operation promptly.
        Only events are emitted; RunManager and StepManager own persistence.
        """
        return BaseEngine(name, kind=kind, action=action, timeout_seconds=timeout_seconds,
                          metadata=metadata, error_message=error_message).execute(context)

    @staticmethod
    def copy_params(value: Any) -> Any:
        """Copy builtin option containers, keeping live SDK clients/callbacks intact."""
        return copy_params(value)

    @staticmethod
    def chunk_value(value: Any, key: str, default: Any = None) -> Any:
        """Read either LiteLLM SDK attributes or equivalent dictionary chunks."""
        return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)

    async def stream_completion(self, request: Mapping[str, Any], *,
                                response: Optional[dict] = None,
                                include_events: bool = True) -> AsyncIterator[Union[str, EngineEvent]]:
        """Yield text and completion observations; inherited execute() routes both.

        include_events=False is for standalone text consumers without persistence.
        response receives the assistant message on success only. Observations
        contain no prompts, tool arguments, headers, or arbitrary SDK payloads.
        """
        result = CompletionResult(model=request.get("model") if isinstance(request.get("model"), str) else None)
        started = time.monotonic()
        if include_events:
            yield EngineEvent(EngineEventType.COMPLETION, completion=deepcopy(result))
        try:
            async with aclosing(self._stream_completion(request, response, result)) as stream:
                async for item in stream:
                    if include_events or isinstance(item, str):
                        yield item
        except (asyncio.CancelledError, GeneratorExit):
            # RunManager finalizes the last durable observation; never yield here.
            raise
        except Exception:
            result.status = RunStatus.FAILED
            result.ended_at = now()
            result.duration_seconds = time.monotonic() - started
            if include_events:
                yield EngineEvent(EngineEventType.COMPLETION, completion=deepcopy(result))
            raise
        else:
            result.status = RunStatus.COMPLETED
            result.ended_at = now()
            result.duration_seconds = time.monotonic() - started
            if include_events:
                yield EngineEvent(EngineEventType.COMPLETION, completion=deepcopy(result))

    @staticmethod
    def _usage(value: Any) -> dict:
        if callable(getattr(value, "model_dump", None)):
            value = value.model_dump()
        if not isinstance(value, Mapping):
            return {}
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if type(item) is int and item >= 0:
                result[key] = item
            elif isinstance(item, Mapping) or callable(getattr(item, "model_dump", None)):
                nested = BaseEngine._usage(item)
                if nested:
                    result[key] = nested
        return result

    async def _stream_completion(self, request: Mapping[str, Any], response: Optional[dict],
                                 result: CompletionResult) -> AsyncIterator[Union[str, EngineEvent]]:
        params = self.copy_params(dict(request))
        if params.get("stream", True) is not True or params.get("n", 1) != 1:
            raise ValueError("Completion requires stream=True and n=1")
        params["stream"] = True
        params.setdefault("num_retries", 0)
        if "stream_options" not in params:
            params["stream_options"] = {"include_usage": True}
        elif isinstance(params["stream_options"], dict):
            params["stream_options"].setdefault("include_usage", True)
        if response is not None and not isinstance(response, dict):
            raise TypeError("Completion response must be a dictionary")
        content_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish_reason = None
        size = 0
        get = self.chunk_value
        async with aclosing(stream_completion(
            params, completion_fn=self.completion_fn, buffer_size=self.buffer_size,
        )) as chunks:
            async for chunk in chunks:
                changed = False
                for attribute, key in (("response_id", "id"), ("model", "model")):
                    value = get(chunk, key)
                    if isinstance(value, str) and value != getattr(result, attribute):
                        setattr(result, attribute, value)
                        changed = True
                usage = self._usage(get(chunk, "usage"))
                if usage:
                    updated = {**result.usage, **usage}
                    if updated != result.usage:
                        result.usage = updated
                        changed = True
                if changed:
                    yield EngineEvent(EngineEventType.COMPLETION, completion=deepcopy(result))
                choices = get(chunk, "choices", [])
                if not choices:
                    continue  # Optional usage-only chunk; no text to deliver.
                if len(choices) != 1 or get(choices[0], "index", 0) != 0:
                    raise ValueError("Expected one completion choice")
                choice = choices[0]
                delta = get(choice, "delta")
                content = get(delta, "content")
                fragments = get(delta, "tool_calls") or []
                if get(delta, "function_call"):
                    raise ValueError("Legacy function_call responses are unsupported")
                if finish_reason is not None and (content or fragments):
                    raise ValueError("Content after stream termination")
                if content is not None:
                    if not isinstance(content, str):
                        raise ValueError("Expected text delta")
                    size += len(content)
                    if size > self.max_output_chars:
                        raise ValueError("Completion output limit exceeded")
                    if content:
                        content_parts.append(content)
                for fragment in fragments:
                    index = get(fragment, "index")
                    if type(index) is not int or not 0 <= index < self.max_tool_calls:
                        raise ValueError("Invalid tool call index or too many calls")
                    if get(fragment, "type") not in (None, "function"):
                        raise ValueError("Unsupported tool call type")
                    call = calls.setdefault(index, {"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                    function = get(fragment, "function")
                    for target, key, value in (
                        (call, "id", get(fragment, "id")),
                        (call["function"], "name", get(function, "name")),
                        (call["function"], "arguments", get(function, "arguments")),
                    ):
                        if value is not None:
                            if not isinstance(value, str):
                                raise ValueError("Invalid tool call fragment")
                            target[key] += value
                    if (len(call["function"]["arguments"]) > self.max_argument_chars
                            or len(call["id"]) > 256 or len(call["function"]["name"]) > 64):
                        raise ValueError("Tool call size limit exceeded")
                reason = get(choice, "finish_reason")
                if reason is not None:
                    if not isinstance(reason, str):
                        raise ValueError("Invalid finish reason")
                    if finish_reason is not None and finish_reason != reason:
                        raise ValueError("Conflicting stream termination")
                    finish_reason = reason
                    if result.finish_reason != reason:
                        result.finish_reason = reason
                        yield EngineEvent(EngineEventType.COMPLETION, completion=deepcopy(result))
                if content:
                    yield content
        result.usage_complete = bool(result.usage)
        if finish_reason not in ("stop", "tool_calls"):
            raise ValueError("Completion did not finish normally")
        if bool(calls) != (finish_reason == "tool_calls"):
            raise ValueError("Tool calls do not match finish reason")
        ids = [call["id"] for call in calls.values()]
        if (any(not call["id"] or not call["function"]["name"] for call in calls.values())
                or len(set(ids)) != len(ids)):
            raise ValueError("Incomplete or duplicate tool calls")
        if response is not None:
            response.clear()
            response.update(role="assistant", content="".join(content_parts) or None)
            if calls:
                response["tool_calls"] = [calls[index] for index in sorted(calls)]

    def run(self, context: EngineContext) -> Union[AsyncIterator[Union[str, EngineEvent]], Awaitable[None]]:
        """Override with async def; yield text, or await work and return None."""
        if self.action is None:
            raise NotImplementedError("Implement run(context) or provide action=")
        return self.action(context)

    async def execute(self, context: EngineContext) -> AsyncIterator[EngineEvent]:
        step_id = new_id()
        yield EngineEvent(EngineEventType.STEP_STARTED, step_id=step_id,
                          kind=self.kind, name=self.name, metadata=deepcopy(self.metadata))
        termination = None
        try:
            async with timeout(self.timeout_seconds):
                operation = self.run(context)
                if inspect.isawaitable(operation):
                    result = await operation
                    if result is not None:
                        raise TypeError("Use yield for text or context.state for results")
                else:
                    try:
                        async for text in operation:
                            if isinstance(text, EngineEvent) and text.type == EngineEventType.COMPLETION:
                                if text.completion is None:
                                    raise ValueError("Completion event requires a result")
                                yield replace(text, step_id=step_id,
                                              completion=replace(text.completion, step_id=step_id))
                                continue
                            if not isinstance(text, str):
                                raise TypeError("Step streams must yield strings")
                            if text:
                                yield EngineEvent(EngineEventType.TEXT_DELTA, text=text)
                    except (asyncio.CancelledError, GeneratorExit) as error:
                        termination = error
                        raise
                    finally:
                        close = getattr(operation, "aclose", None)
                        if close is not None:
                            try:
                                await close()
                            except Exception:
                                if termination is None:
                                    raise
                                # Preserve cancellation/GeneratorExit through
                                # cleanup. A timeout scope can then translate
                                # its own cancellation into TimeoutError.
        except (asyncio.CancelledError, GeneratorExit):
            # Never yield while being cancelled/closed. RunManager finalizes the
            # persisted active Step, including cancellation before run() starts.
            raise
        except Exception as error:
            yield EngineEvent(EngineEventType.STEP_FAILED, step_id=step_id, error=f"{self.error_message}: {error}")
            raise
        yield EngineEvent(EngineEventType.STEP_COMPLETED, step_id=step_id)
