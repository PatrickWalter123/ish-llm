"""Embedding inference reusable from Engines, Tools, RAG or ordinary Python."""

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from ._client import ModelClient


class EmbeddingModel(ModelClient):
    def __init__(self, *, embedding_fn: Optional[Callable[..., Awaitable[Any]]] = None,
                 **params: Any) -> None:
        super().__init__("aembedding", embedding_fn, params)

    async def embed(self, input: Any, **kwargs: Any) -> Any:
        """Return LiteLLM's native response, including vectors and reported usage.

        Per-call kwargs override defaults. Provider-specific input formats and
        model parameters are forwarded without an application allowlist.
        """
        return await self._invoke(input=input, **kwargs)
