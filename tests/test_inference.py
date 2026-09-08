import asyncio
import subprocess
import sys
import unittest
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock, patch

from ish.inference import EmbeddingModel, RerankModel


class InferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_passes_open_kwargs_and_preserves_native_response(self):
        response = SimpleNamespace(data=[{"embedding": [0.1, 0.2]}], usage={"total_tokens": 2})
        client = object()
        async def embed(**request):
            self.assertIs(request["client"], client)
            self.assertEqual(request["dimensions"], 2)
            self.assertEqual(request["provider_option"], {"modes": ["custom"]})
            self.assertEqual(request["timeout"], 12)
            self.assertEqual(request["num_retries"], 0)
            request["input"].append("provider mutation")
            request["provider_option"]["modes"].clear()
            return response
        options = {"provider_option": {"modes": ["custom"]}}
        model = EmbeddingModel(model="openai/test", embedding_fn=embed, client=client,
                               dimensions=100, **options)
        inputs = ["hello"]
        self.assertIs(await model.embed(inputs, dimensions=2, timeout=12), response)
        self.assertEqual(inputs, ["hello"])
        self.assertEqual(options, {"provider_option": {"modes": ["custom"]}})
        self.assertEqual(model.params["provider_option"], options["provider_option"])

    async def test_rerank_accepts_document_objects_and_returns_scores_and_metadata(self):
        response = {"results": [{"index": 1, "relevance_score": 0.9}], "meta": {"billed_units": 1}}
        async def rerank(**request):
            self.assertEqual(request["model"], "cohere/override")
            self.assertEqual(request["query"], "query")
            self.assertEqual(request["top_n"], 1)
            self.assertEqual(request["return_documents"], True)
            request["documents"][0]["text"] = "changed"
            return response
        model = RerankModel(model="cohere/default", rerank_fn=rerank, top_n=5)
        documents = [{"text": "first"}, {"text": "second"}]
        self.assertIs(await model.rerank("query", documents, model="cohere/override",
                                        top_n=1, return_documents=True), response)
        self.assertEqual(documents[0]["text"], "first")

    async def test_concurrent_calls_keep_inputs_separate(self):
        entered = []
        ready = asyncio.Event()
        async def embed(**request):
            entered.append(request)
            request["extra"]["values"].append(request["input"])
            if len(entered) == 2:
                ready.set()
            await ready.wait()
            return request["extra"]["values"]
        model = EmbeddingModel(model="test", embedding_fn=embed, extra={"values": []})
        first, second = await asyncio.wait_for(asyncio.gather(model.embed("a"), model.embed("b")), 3)
        self.assertEqual(first, ["a"])
        self.assertEqual(second, ["b"])
        self.assertEqual(model.params["extra"], {"values": []})

    async def test_cancellation_propagates_to_async_provider(self):
        started, closed = asyncio.Event(), asyncio.Event()
        async def rerank(**request):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        model = RerankModel(model="test", rerank_fn=rerank)
        task = asyncio.create_task(model.rerank("query", ["doc"]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())

    async def test_missing_model_and_native_provider_error(self):
        call = AsyncMock()
        with self.assertRaises(ValueError):
            await EmbeddingModel(embedding_fn=call).embed("text")
        call.assert_not_called()
        failure = RuntimeError("provider diagnostic")
        call.side_effect = failure
        with self.assertRaises(RuntimeError) as raised:
            await EmbeddingModel(model="test", embedding_fn=call).embed("text")
        self.assertIs(raised.exception, failure)

    async def test_default_dispatch_uses_installed_litellm_async_entry_points(self):
        # Actual SDK import/API surface; provider entry points mocked, no network.
        import importlib
        sdk = await asyncio.to_thread(importlib.import_module, "litellm")
        self.assertTrue(callable(sdk.aembedding))
        self.assertTrue(callable(sdk.arerank))
        response = object()
        with patch.object(sdk, "aembedding", AsyncMock(return_value=response)) as embed:
            self.assertIs(await EmbeddingModel(model="test").embed("text"), response)
            embed.assert_awaited_once_with(model="test", input="text", timeout=60, num_retries=0)
        with patch.object(sdk, "arerank", AsyncMock(return_value=response)) as rerank:
            self.assertIs(await RerankModel(model="test").rerank("q", ["d"]), response)
            rerank.assert_awaited_once_with(model="test", query="q", documents=["d"], timeout=60, num_retries=0)

    def test_import_does_not_load_engines_services_or_sdk(self):
        code = ("import sys; import ish.inference; "
                "assert not any(n == 'litellm' or n.startswith(('ish.engines', 'ish.services', 'ish.core')) "
                "for n in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        for method in (EmbeddingModel.__init__, EmbeddingModel.embed, RerankModel.__init__, RerankModel.rerank):
            self.assertTrue(get_type_hints(method))
