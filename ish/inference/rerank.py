"""Document reranking independent of execution strategy and storage."""

from collections.abc import Awaitable, Callable
from typing import Any, Optional, Union

from ._client import ModelClient


class RerankModel(ModelClient):
    def __init__(self, *, rerank_fn: Optional[Callable[..., Awaitable[Any]]] = None,
                 **params: Any) -> None:
        super().__init__("arerank", rerank_fn, params)

    async def rerank(self, query: str, documents: list[Union[str, dict[str, Any]]],
                     **kwargs: Any) -> Any:
        """Return LiteLLM's native response with indexes, scores and metadata."""
        return await self._invoke(query=query, documents=documents, **kwargs)
