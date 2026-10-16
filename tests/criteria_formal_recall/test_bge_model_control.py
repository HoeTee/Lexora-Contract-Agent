"""BGE protocol compatibility must retain the common model controller."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from config import infer_rerank_provider
from tools.docxindex import reranker
from tools.docxindex.vector import client as vector_client
from tools.retrieval.llamaindex import limited_embedding


class BGEModelControlTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.slots = []

        @asynccontextmanager
        async def slot(kind, caller, model, **kwargs):
            self.slots.append((kind, model))
            yield SimpleNamespace(key=f"test:{kind}")

        slot_patch = patch("endpoints.runtime.model_calls.model_semaphore", slot)
        slot_patch.start()
        self.addCleanup(slot_patch.stop)

    def http_client(self, remaining):
        return httpx.AsyncClient(timeout=remaining, transport=httpx.MockTransport(self.respond))

    async def respond(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={
                "object": "list", "model": body["model"],
                "data": [{"object": "embedding", "index": i, "embedding": [float(i), 0.5]}
                         for i in range(len(body["input"]))],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            })
        return httpx.Response(200, json=[{"index": 1, "score": 0.9}])

    def test_bge_embedding_name_uses_openai_compatible_protocol_in_both_backends(self):
        # The deployment owns the actual model name; no Qwen-only allow-list applies.
        model = "bge-m3"
        with patch.object(vector_client, "async_http_client", self.http_client), \
                patch.object(limited_embedding, "async_http_client", self.http_client):
            docx = vector_client.EmbeddingClient(vector_client.EmbeddingSettings(
                model=model, base_url="http://unused.invalid/v1", api_key="unused"))
            self.assertEqual(docx.embed_batch(["a", "b"]), [[0.0, 0.5], [1.0, 0.5]])
            llama = limited_embedding.SemaphoreOpenAILikeEmbedding(
                model_name=model, api_base="http://unused.invalid/v1", api_key="unused")
            self.assertEqual(llama.get_query_embedding("a"), [0.0, 0.5])
        self.assertEqual(self.slots, [("embedding", model)] * 2)
        self.assertEqual([path for path, _ in self.requests], ["/v1/embeddings"] * 2)
        self.assertTrue(all(body["model"] == model for _, body in self.requests))

    def test_bge_reranker_mapping_payload_and_shared_slot(self):
        model = "bge-reranker-v2-m3"
        self.assertEqual(infer_rerank_provider(model), "bge")
        with patch.object(reranker, "async_http_client", self.http_client):
            result = reranker.RerankerClient(model=model, base_url="http://unused.invalid/rerank",
                                            api_key="", instruct="unused").rank("query", ["a", "b"], 1)
        self.assertEqual(result, [(1, 0.9)])
        self.assertEqual(self.slots, [("reranker", model)])
        self.assertEqual(self.requests, [("/rerank", {"query": "query", "documents": ["a", "b"], "top_n": 1})])

    def test_test_network_qwen_names_use_their_own_model_pools(self):
        self.assertEqual(infer_rerank_provider("qwen3-reranker"), "higress_qwen")
        with patch.object(vector_client, "async_http_client", self.http_client), \
                patch.object(reranker, "async_http_client", self.http_client):
            embedding = vector_client.EmbeddingClient(vector_client.EmbeddingSettings(
                model="qwen3-embedding", base_url="http://unused.invalid/v1", api_key="unused"))
            self.assertEqual(embedding.embed_batch(["text"]), [[0.0, 0.5]])
            result = reranker.RerankerClient(model="qwen3-reranker", base_url="http://unused.invalid/v1/rerank",
                                            api_key="unused", instruct="relevant passages").rank("query", ["a", "b"], 1)
        self.assertEqual(result, [(1, 0.9)])
        self.assertEqual(self.slots, [("embedding", "qwen3-embedding"), ("reranker", "qwen3-reranker")])
        self.assertEqual(self.requests[-1], ("/v1/rerank", {
            "model": "qwen3-reranker", "query": "query", "documents": ["a", "b"],
            "top_n": 1, "instruct": "relevant passages",
        }))
