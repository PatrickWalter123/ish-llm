"""Stream completions, execute requested tools, and repeat within one Run."""

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ish.core.models import MessageRole, MessageStatus, new_id
from ish.services.secrets import SecretManager, SecretResolver
from .base import EngineContext, EngineEvent, EngineEventType
from ish.providers.litellm import completion, stream_completion
from ish.components.tools import ToolRegistry


class LoopEngineError(RuntimeError):
    """A safe-to-display LoopEngine failure."""


@dataclass(frozen=True, slots=True)
class LoopOptions:
    max_iterations: int = 8
    request_timeout: float = 60.0
    tool_timeout: float = 30.0
    max_tokens: int | None = None
    buffer_size: int = 8
    max_tool_calls: int = 16
    max_argument_chars: int = 65536
    max_output_chars: int = 1_000_000

    def __post_init__(self) -> None:
        for value in (self.max_iterations, self.buffer_size, self.max_tool_calls,
                      self.max_argument_chars, self.max_output_chars):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Loop limits must be positive integers")
        for value in (self.request_timeout, self.tool_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be positive and finite")
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int)
            or self.max_tokens < 1
        ):
            raise ValueError("max_tokens must be positive")


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


@dataclass(slots=True)
class _ToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""

    def message(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {
            "name": self.name, "arguments": self.arguments,
        }}


@dataclass(slots=True)
class _Turn:
    content: list[str] = field(default_factory=list)
    calls: dict[int, _ToolCall] = field(default_factory=dict)
    finish_reason: str | None = None
    size: int = 0

    def add(self, chunk: Any, options: LoopOptions) -> str | None:
        choices = _get(chunk, "choices", [])
        if not choices:
            return None  # e.g. the optional usage-only final chunk
        if len(choices) != 1 or _get(choices[0], "index", 0) != 0:
            raise LoopEngineError("Expected one completion choice")
        choice = choices[0]
        delta = _get(choice, "delta")
        content = _get(delta, "content")
        calls = _get(delta, "tool_calls") or []
        if _get(delta, "function_call"):
            raise LoopEngineError("Legacy function_call responses are unsupported")
        if self.finish_reason is not None and (content or calls):
            raise LoopEngineError("Content after stream termination")
        if content is not None:
            if not isinstance(content, str):
                raise LoopEngineError("Expected text delta")
            self.size += len(content)
            if self.size > options.max_output_chars:
                raise LoopEngineError("Completion output limit exceeded")
            if content:
                self.content.append(content)
        for fragment in calls:
            index = _get(fragment, "index")
            if type(index) is not int or not 0 <= index < options.max_tool_calls:
                raise LoopEngineError("Invalid tool call index or too many calls")
            if _get(fragment, "type") not in (None, "function"):
                raise LoopEngineError("Unsupported tool call type")
            call = self.calls.setdefault(index, _ToolCall())
            function = _get(fragment, "function")
            for attribute, value in (("id", _get(fragment, "id")),
                                     ("name", _get(function, "name")),
                                     ("arguments", _get(function, "arguments"))):
                if value is not None:
                    if not isinstance(value, str):
                        raise LoopEngineError("Invalid tool call fragment")
                    setattr(call, attribute, getattr(call, attribute) + value)
            if (len(call.arguments) > options.max_argument_chars
                    or len(call.id) > 256 or len(call.name) > 64):
                raise LoopEngineError("Tool call size limit exceeded")
        reason = _get(choice, "finish_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise LoopEngineError("Invalid finish reason")
            if self.finish_reason is not None and self.finish_reason != reason:
                raise LoopEngineError("Conflicting stream termination")
            self.finish_reason = reason
        return content or None

    def validate(self) -> None:
        if self.finish_reason is None:
            raise LoopEngineError("Stream ended without a finish reason")
        if self.finish_reason not in ("stop", "tool_calls"):
            raise LoopEngineError("Completion did not finish normally")
        if bool(self.calls) != (self.finish_reason == "tool_calls"):
            raise LoopEngineError("Tool calls do not match finish reason")
        ids = [call.id for call in self.calls.values()]
        if any(not call.id or not call.name for call in self.calls.values()) or len(set(ids)) != len(ids):
            raise LoopEngineError("Incomplete or duplicate tool calls")


class LoopEngine:
    def __init__(self, *, tools: ToolRegistry | None = None,
                 secrets: SecretResolver | None = None,
                 options: LoopOptions | None = None,
                 completion_fn: Callable[..., Iterator[Any]] = completion) -> None:
        self.tools = tools or ToolRegistry()
        self.secrets = secrets
        self.options = options or LoopOptions()
        self.completion_fn = completion_fn

    def _request(self, context: EngineContext) -> dict[str, Any]:
        config = context.project.config
        if not config.model.strip():
            raise LoopEngineError("Project model is required")
        request: dict[str, Any] = {
            "model": config.model, "stream": True, "timeout": self.options.request_timeout,
            "num_retries": 0,
        }
        if config.temperature is not None:
            request["temperature"] = config.temperature
        if self.options.max_tokens is not None:
            request["max_tokens"] = self.options.max_tokens
        if config.api_base is not None:
            url = urlsplit(config.api_base)
            if (url.scheme not in ("http", "https") or not url.hostname or url.username
                    or url.password or url.query or url.fragment):
                raise LoopEngineError("api_base must be an HTTP URL without credentials or query")
            request["api_base"] = config.api_base
        if config.credential_ref is not None:
            try:
                resolver = self.secrets if self.secrets is not None else SecretManager(
                    log_dir=context.project.paths.logs)
                request["api_key"] = resolver.resolve(config.credential_ref)
            except Exception:
                raise LoopEngineError("Credential could not be resolved") from None
        definitions = self.tools.definitions()
        if definitions:
            request["tools"] = definitions
            request["tool_choice"] = "auto"
        return request

    async def execute(self, context: EngineContext) -> AsyncIterator[EngineEvent]:
        messages = [{"role": message.role.value, "content": message.content}
                    for message in context.messages
                    if (message.role == MessageRole.USER and message.status == MessageStatus.COMMITTED)
                    or (message.role != MessageRole.USER and message.status in (
                        MessageStatus.COMPLETED, MessageStatus.INTERRUPTED, MessageStatus.FAILED))]
        seen_call_ids: set[str] = set()
        for iteration in range(1, self.options.max_iterations + 1):
            step_id = new_id()
            yield EngineEvent(EngineEventType.STEP_STARTED, step_id=step_id, kind="llm",
                              name="LLM completion", metadata={"iteration": iteration})
            turn = _Turn()
            try:
                request = self._request(context)
                # Use a fresh transcript: a cancelled worker may still hold this
                # request while waiting for its synchronous network read to end.
                request["messages"] = deepcopy(messages)
                async with asyncio.timeout(self.options.request_timeout):
                    async with aclosing(stream_completion(
                        request, completion_fn=self.completion_fn,
                        buffer_size=self.options.buffer_size,
                    )) as chunks:
                        async for chunk in chunks:
                            text = turn.add(chunk, self.options)
                            if text is not None:
                                yield EngineEvent(EngineEventType.TEXT_DELTA, text=text)
                turn.validate()
                calls = [turn.calls[index] for index in sorted(turn.calls)]
                if calls and iteration == self.options.max_iterations:
                    raise LoopEngineError("Loop iteration limit reached")
                if any(call.id in seen_call_ids for call in calls):
                    raise LoopEngineError("Repeated tool call ID")
                try:
                    prepared = [self.tools.prepare(call.name, call.arguments) for call in calls]
                except ValueError:
                    raise LoopEngineError("Tool call validation failed") from None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Only our own controlled messages may enter Step persistence.
                reason = (str(error) if isinstance(error, LoopEngineError) else
                          "LLM request timed out" if isinstance(error, TimeoutError) else
                          "LLM iteration failed")
                yield EngineEvent(EngineEventType.STEP_FAILED, step_id=step_id,
                                  error=reason)
                raise LoopEngineError(reason) from None
            yield EngineEvent(EngineEventType.STEP_COMPLETED, step_id=step_id)
            if not calls:
                return
            messages.append({"role": "assistant", "content": "".join(turn.content) or None,
                             "tool_calls": [call.message() for call in calls]})
            for call, (tool, arguments) in zip(calls, prepared, strict=True):
                seen_call_ids.add(call.id)
                tool_step_id = new_id()
                yield EngineEvent(EngineEventType.STEP_STARTED, step_id=tool_step_id,
                                  kind="tool", name=tool.name,
                                  metadata={"iteration": iteration, "tool_call_id": call.id})
                try:
                    async with asyncio.timeout(self.options.tool_timeout):
                        result = await tool.handler(arguments)
                    content = result if isinstance(result, str) else json.dumps(
                        result, ensure_ascii=False, allow_nan=False)
                    if len(content) > self.options.max_output_chars:
                        raise LoopEngineError("Tool output limit exceeded")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    yield EngineEvent(EngineEventType.STEP_FAILED, step_id=tool_step_id,
                                      error="Tool execution failed")
                    raise LoopEngineError("Tool execution failed") from None
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "name": tool.name, "content": content})
                yield EngineEvent(EngineEventType.STEP_COMPLETED, step_id=tool_step_id)
