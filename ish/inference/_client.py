"""Shared request mechanics; no scheduler, lifecycle events or domain writes."""

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from ish.providers.parameters import copy_params


class ModelClient:
    def __init__(self, operation: str, call_fn: Optional[Callable[..., Awaitable[Any]]],
                 params: dict[str, Any]) -> None:
        if call_fn is not None and not callable(call_fn):
            raise TypeError("Model call function must be callable")
        self._operation = operation
        self._call_fn = call_fn
        self.params = copy_params(params)

    async def _invoke(self, **kwargs: Any) -> Any:
        request = copy_params(self.params)
        request.update(copy_params(kwargs))
        if not isinstance(request.get("model"), str) or not request["model"].strip():
            raise ValueError("Model is required")
        request.setdefault("timeout", 60)
        request.setdefault("num_retries", 0)
        call = self._call_fn
        if call is None:
            # Heavy SDK initialization must not block the application's loop.
            sdk = await asyncio.to_thread(importlib.import_module, "litellm")
            call = getattr(sdk, self._operation)
        return await call(**request)
