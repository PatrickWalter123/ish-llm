"""A BaseEngine subclass: complete, execute requested tools, then repeat."""

import json
import math
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any, Optional, Union
from urllib.parse import urlsplit

from ish.compat import aclosing
from ish.core.models import MessageRole, MessageStatus
from ish.services.secrets import SecretManager, SecretResolver
from ish.providers.litellm import completion
from .base import BaseEngine, EngineContext, EngineEvent


class LoopEngine(BaseEngine):
    """LiteLLM completion/tool loop with runtime kwargs and optional preparation inputs."""

    def __init__(self, *, secrets: Optional[SecretResolver] = None,
                 max_iterations: int = 8, request_timeout: float = 60.0,
                 tool_timeout: float = 30.0, buffer_size: int = 8,
                 max_tool_calls: int = 16, max_argument_chars: int = 65536,
                 max_output_chars: int = 1_000_000,
                 completion_kwargs: Optional[Union[Mapping[str, Any],
                     Callable[[EngineContext], Mapping[str, Any]]]] = None,
                 system_prompt: Optional[Union[str, Callable[[EngineContext], str]]] = None,
                 completion_fn: Callable[..., Iterator[Any]] = completion) -> None:
        super().__init__("Loop", completion_fn=completion_fn, buffer_size=buffer_size,
                         max_tool_calls=max_tool_calls, max_argument_chars=max_argument_chars,
                         max_output_chars=max_output_chars)
        if type(max_iterations) is not int or max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")
        for value in (request_timeout, tool_timeout):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError("Timeouts must be positive and finite")
        self.secrets = secrets
        self.max_iterations = max_iterations
        self.request_timeout = request_timeout
        self.tool_timeout = tool_timeout
        if completion_kwargs is not None and not (
            isinstance(completion_kwargs, Mapping) or callable(completion_kwargs)
        ):
            raise TypeError("completion_kwargs must be a mapping or context factory")
        if system_prompt is not None and not isinstance(system_prompt, str) and not callable(system_prompt):
            raise TypeError("system_prompt must be a string or context factory")
        self.completion_kwargs = (self.copy_params(dict(completion_kwargs))
                                  if isinstance(completion_kwargs, Mapping) else completion_kwargs)
        self.system_prompt = system_prompt

    def _request(self, context: EngineContext, params: dict[str, Any]) -> dict[str, Any]:
        config = context.project.config
        request: dict[str, Any] = {
            "model": config.model, "stream": True, "timeout": self.request_timeout,
            "num_retries": 0,
        }
        if config.temperature is not None:
            request["temperature"] = config.temperature
        if config.api_base is not None:
            request["api_base"] = config.api_base
        request.update(self.copy_params(params))
        if not isinstance(request.get("model"), str) or not request["model"].strip():
            raise ValueError("Completion model is required")
        if request.get("api_base") is not None:
            url = urlsplit(request["api_base"])
            if (url.scheme not in ("http", "https") or not url.hostname or url.username
                    or url.password or url.query or url.fragment):
                raise ValueError("api_base must be an HTTP URL without credentials or query")
        if config.credential_ref is not None and "api_key" not in request:
            try:
                resolver = self.secrets if self.secrets is not None else SecretManager(
                    log_dir=context.project.paths.logs)
                request["api_key"] = resolver.resolve(config.credential_ref)
            except Exception:
                raise ValueError("Credential could not be resolved") from None
        definitions = context.tools.definitions()
        if definitions:
            request["tools"] = definitions
            request.setdefault("tool_choice", "auto")
        return request

    async def execute(self, context: EngineContext) -> AsyncIterator[EngineEvent]:
        # Evaluate factories after preparation, once per Run. No shared per-Run
        # state lives on the Engine; each provider call gets fresh containers.
        supplied = (self.completion_kwargs(context) if callable(self.completion_kwargs)
                    else self.completion_kwargs)
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, Mapping) or any(not isinstance(key, str) for key in supplied):
            raise ValueError("Completion parameters must be a string-keyed mapping")
        params = self.copy_params(dict(supplied))
        # These fields define the Loop transcript/tool execution contract.
        if any(key in params for key in ("messages", "tools", "functions", "function_call")):
            raise ValueError("Loop owns messages and registered tool definitions")
        if params.get("stream", True) is not True or params.get("n", 1) != 1:
            raise ValueError("Loop requires stream=True and n=1")
        prompt = self.system_prompt(context) if callable(self.system_prompt) else self.system_prompt
        if prompt is not None and not isinstance(prompt, str):
            raise ValueError("System prompt must be a string")
        messages = [{"role": message.role.value, "content": message.content}
                    for message in context.messages
                    if (message.role == MessageRole.USER and message.status == MessageStatus.COMMITTED)
                    or (message.role != MessageRole.USER and message.status in (
                        MessageStatus.COMPLETED, MessageStatus.INTERRUPTED, MessageStatus.FAILED))]
        if prompt:
            messages.insert(0, {"role": "system", "content": prompt})
        seen_call_ids: set[str] = set()
        for iteration in range(1, self.max_iterations + 1):
            response: dict[str, Any] = {}
            calls, prepared = [], []

            async def complete(_context):
                nonlocal calls, prepared
                request = self._request(context, params)
                request["messages"] = messages
                async with aclosing(self.stream_completion(request, response=response)) as deltas:
                    async for text in deltas:
                        yield text
                calls = response.get("tool_calls", [])
                if calls and iteration == self.max_iterations:
                    raise ValueError("Loop iteration limit reached")
                if any(call["id"] in seen_call_ids for call in calls):
                    raise ValueError("Repeated tool call ID")
                # Validate the whole batch before any tool can have side effects.
                prepared = [context.tools.prepare(
                    call["function"]["name"], call["function"]["arguments"],
                ) for call in calls]

            async with aclosing(self.step(
                context, complete, name="LLM completion", kind="llm",
                timeout_seconds=self.request_timeout, metadata={"iteration": iteration},
                error_message="LLM iteration failed",
            )) as events:
                async for event in events:
                    yield event
            if not calls:
                return
            messages.append(response)
            for call, (tool, arguments) in zip(calls, prepared):
                seen_call_ids.add(call["id"])

                async def execute_tool(_context):
                    result = await tool.handler(arguments)
                    content = result if isinstance(result, str) else json.dumps(
                        result, ensure_ascii=False, allow_nan=False)
                    if len(content) > self.max_output_chars:
                        raise ValueError("Tool output limit exceeded")
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "name": tool.name, "content": content})

                async with aclosing(self.step(
                    context, execute_tool, name=tool.name, kind="tool",
                    timeout_seconds=self.tool_timeout,
                    metadata={"iteration": iteration, "tool_call_id": call["id"]},
                    error_message="Tool execution failed",
                )) as events:
                    async for event in events:
                        yield event
