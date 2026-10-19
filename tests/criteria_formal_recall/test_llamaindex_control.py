"""Exercise the retained LlamaIndex backend through real SDK/HTTP parsing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from llama_index.core.base.llms.types import ChatMessage
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from openai import AsyncOpenAI

from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout, ModelRetryLimitError, task_execution
from loggers.call_log import logical_call
from tools.retrieval.llamaindex import limited_embedding, limited_llm, qwen_reranker


class LlamaIndexControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.client_options = []
        self.slots = []
        self.http_timeouts = []
        self.http_handler = self.success

        def http_client(timeout):
            self.http_timeouts.append(timeout)
            return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(self.http_handler))

        for module in (limited_llm, limited_embedding):
            transport_patch = patch.object(module, "async_http_client", http_client)
            transport_patch.start()
            self.addCleanup(transport_patch.stop)

        @asynccontextmanager
        async def slot(model_type, caller, model, **kwargs):
            self.slots.append((model_type, caller, model))
            yield SimpleNamespace(key=f"test:{model_type}")

        self.slot_patch = patch("endpoints.runtime.model_calls.model_semaphore", slot)
        self.slot_patch.start()
        self.addCleanup(self.slot_patch.stop)

    def llm(self, **kwargs):
        return limited_llm.UnifiedOpenAILike(
            model="test-llm",
            api_key="unused",
            api_base="http://unused.invalid/v1",
            is_chat_model=True,
            max_retries=7,
            **kwargs,
        )

    def embedding(self):
        return limited_embedding.SemaphoreOpenAILikeEmbedding(
            model_name="test-embedding",
            api_key="unused",
            api_base="http://unused.invalid/v1",
            max_retries=7,
        )

    def client_factory(self, handler):
        self.http_handler = handler

        def create(**kwargs):
            self.client_options.append(kwargs)
            return AsyncOpenAI(**kwargs)

        return create

    async def success(self, request):
        body = json.loads(request.content)
        self.calls.append((request.url.path, body))
        if request.url.path.endswith("/embeddings"):
            data = [
                {"object": "embedding", "index": index, "embedding": [float(index), 0.5]}
                for index in reversed(range(len(body["input"])))
            ]
            response = {
                "object": "list",
                "model": body["model"],
                "data": data,
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            }
        elif request.url.path.endswith("/chat/completions"):
            response = {
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "retrieval answer"},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        else:
            response = {
                "id": "completion-test",
                "object": "text_completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "text": "legacy completion"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        return httpx.Response(200, json=response)

    async def test_sync_and_async_chat_and_completion_use_shared_controller(self):
        llm = self.llm(additional_kwargs={"extra_body": {"enable_thinking": False}})
        with patch.object(limited_llm, "AsyncOpenAI", self.client_factory(self.success)):
            self.assertEqual(llm.chat([ChatMessage(role="user", content="query")]).message.content, "retrieval answer")
            self.assertEqual(
                (await llm.achat([ChatMessage(role="user", content="query")])).message.content, "retrieval answer"
            )
            self.assertEqual(llm.complete("query").text, "retrieval answer")
            self.assertEqual((await llm.acomplete("query")).text, "retrieval answer")
            self.assertEqual(llm.complete("query", use_chat_completions=False).text, "legacy completion")
            self.assertEqual((await llm.acomplete("query", use_chat_completions=False)).text, "legacy completion")

        self.assertEqual(len(self.calls), 6)
        self.assertEqual([kind for kind, _, _ in self.slots], ["llm"] * 6)
        self.assertTrue(all(options["max_retries"] == 0 for options in self.client_options))
        self.assertTrue(all(body["stream"] is False and body["enable_thinking"] is False for _, body in self.calls))
        self.assertEqual(llm.max_retries, 0)
        self.assertEqual(self.http_timeouts, [options["timeout"] for options in self.client_options])

    async def test_all_embedding_entry_points_use_shared_controller_and_preserve_input_order(self):
        embedding = self.embedding()
        with patch.object(limited_embedding, "AsyncOpenAI", self.client_factory(self.success)):
            self.assertEqual(embedding.get_query_embedding("q\nuery"), [0.0, 0.5])
            self.assertEqual(embedding.get_text_embedding("text"), [0.0, 0.5])
            self.assertEqual(embedding.get_text_embedding_batch(["a", "b"]), [[0.0, 0.5], [1.0, 0.5]])
            self.assertEqual(await embedding.aget_query_embedding("query"), [0.0, 0.5])
            self.assertEqual(await embedding.aget_text_embedding("text"), [0.0, 0.5])
            self.assertEqual(await embedding.aget_text_embedding_batch(["a", "b"]), [[0.0, 0.5], [1.0, 0.5]])

        self.assertEqual(len(self.calls), 6)
        self.assertEqual([kind for kind, _, _ in self.slots], ["embedding"] * 6)
        self.assertEqual(self.calls[0][1]["input"], ["q uery"])
        self.assertEqual(embedding.max_retries, 0)
        self.assertTrue(all(options["max_retries"] == 0 for options in self.client_options))

    async def test_rate_limit_retry_uses_only_shared_allowance(self):
        count = 0

        async def rate_limited(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(
                    429, headers={"retry-after": "0"}, json={"error": {"message": "busy", "type": "rate_limit_error"}}
                )
            return await self.success(request)

        with patch.object(limited_llm, "AsyncOpenAI", self.client_factory(rate_limited)):
            with logical_call("llamaindex.llm", "test-llm") as logical:
                logical.budget = ModelCallBudget(timeout_seconds=2, max_retries=1)
                response = await self.llm().achat([ChatMessage(role="user", content="query")])
                self.assertEqual(response.message.content, "retrieval answer")
                self.assertEqual(logical.budget.sends, 2)
                self.assertEqual(logical.tokens["input_tokens"], 3)

        self.assertEqual(count, 2)
        self.assertEqual(len(self.slots), 2)
        self.assertEqual([options["max_retries"] for options in self.client_options], [0, 0])

    async def test_timeout_cancels_actual_async_http_for_llm_and_embedding(self):
        for module, kind, synchronous in [
            (limited_llm, "llm", False),
            (limited_embedding, "embedding", False),
            (limited_llm, "llm", True),
            (limited_embedding, "embedding", True),
        ]:
            with self.subTest(model_type=kind, synchronous=synchronous):
                cancelled = []

                async def slow(request, cancelled=cancelled):
                    try:
                        await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        cancelled.append(True)
                        raise

                with patch.object(module, "AsyncOpenAI", self.client_factory(slow)):
                    with logical_call(f"llamaindex.{kind}", f"test-{kind}") as logical:
                        logical.budget = ModelCallBudget(timeout_seconds=0.1, max_retries=1)
                        with self.assertRaises(ModelCallTimeout):
                            if synchronous and kind == "llm":
                                self.llm().chat([ChatMessage(role="user", content="query")])
                            elif synchronous:
                                self.embedding().get_query_embedding("query")
                            elif kind == "llm":
                                await self.llm().achat([ChatMessage(role="user", content="query")])
                            else:
                                await self.embedding().aget_query_embedding("query")
                self.assertEqual(cancelled, [True, True])

    async def test_timeout_retry_receives_full_request_budget_in_sync_and_async_adapters(self):
        for module, kind, synchronous in [
            (limited_llm, "llm", False),
            (limited_embedding, "embedding", False),
            (limited_llm, "llm", True),
            (limited_embedding, "embedding", True),
        ]:
            with self.subTest(model_type=kind, synchronous=synchronous):
                sends = []
                cancelled = []
                async def timeout_then_success(request, *, sends=sends, cancelled=cancelled):
                    sends.append(request)
                    if len(sends) == 1:
                        try:
                            await asyncio.Event().wait()
                        finally:
                            cancelled.append(True)
                    return await self.success(request)
                before = len(self.http_timeouts)
                with patch.object(module, "AsyncOpenAI", self.client_factory(timeout_then_success)), \
                        patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
                    with logical_call(f"llamaindex.{kind}", f"test-{kind}") as logical:
                        logical.budget = ModelCallBudget(timeout_seconds=0.15, max_retries=1)
                        if synchronous and kind == "llm":
                            result = self.llm().chat([ChatMessage(role="user", content="query")]).message.content
                        elif synchronous:
                            result = self.embedding().get_query_embedding("query")
                        elif kind == "llm":
                            result = (await self.llm().achat([ChatMessage(role="user", content="query")])).message.content
                        else:
                            result = await self.embedding().aget_query_embedding("query")
                        self.assertEqual(result, "retrieval answer" if kind == "llm" else [0.0, 0.5])
                        self.assertEqual(logical.budget.sends, 2)
                self.assertEqual((len(sends), cancelled), (2, [True]))
                self.assertEqual(len(self.http_timeouts[before:]), 2)
                self.assertTrue(all(timeout > 0.13 for timeout in self.http_timeouts[before:]))

    async def test_nested_logical_calls_cannot_reset_send_allowance(self):
        with patch.object(limited_llm, "AsyncOpenAI", self.client_factory(self.success)):
            with logical_call("llamaindex.llm", "test-llm") as logical:
                logical.budget = ModelCallBudget(timeout_seconds=2, max_retries=0)
                llm = self.llm()
                llm.chat([ChatMessage(role="user", content="first")])
                with self.assertRaises(ModelRetryLimitError):
                    await llm.achat([ChatMessage(role="user", content="second")])
                self.assertEqual(logical.budget.sends, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.client_options), 1)

    async def test_task_deadline_is_inherited_by_sync_adapter(self):
        cancelled = []

        async def slow(request):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        llm = self.llm()
        with patch.object(limited_llm, "AsyncOpenAI", self.client_factory(slow)):
            with task_execution(time.time() + 0.1):
                with self.assertRaises(ModelCallTimeout):
                    llm.chat([ChatMessage(role="user", content="query")])
        self.assertEqual(cancelled, [True])
        self.assertLessEqual(self.http_timeouts[0], 0.1)

    async def test_streaming_paths_fail_before_any_request(self):
        llm = self.llm()
        with patch.object(limited_llm, "AsyncOpenAI") as client:
            with self.assertRaises(NotImplementedError):
                llm.stream_chat([ChatMessage(role="user", content="query")])
            with self.assertRaises(NotImplementedError):
                llm.stream_complete("query")
            with self.assertRaises(NotImplementedError):
                await llm.astream_chat([ChatMessage(role="user", content="query")])
            with self.assertRaises(NotImplementedError):
                await llm.astream_complete("query")
            client.assert_not_called()

    async def test_reranker_adapter_preserves_provider_and_index_mapping(self):
        nodes = [NodeWithScore(node=TextNode(text="first")), NodeWithScore(node=TextNode(text="second"))]
        reranker = qwen_reranker.QwenRerankPostprocessor(
            api_key="unused",
            base_url="http://unused.invalid/v1/rerank",
            model="test-reranker",
            provider="higress_qwen",
            instruct="contract policy",
            top_n=1,
        )
        with patch.object(qwen_reranker, "RerankerClient") as client:
            client.return_value.rank.return_value = [(1, 0.9)]
            result = reranker.postprocess_nodes(nodes, query_bundle=QueryBundle(query_str="query"))
        client.assert_called_once_with(
            model="test-reranker",
            base_url="http://unused.invalid/v1/rerank",
            api_key="unused",
            instruct="contract policy",
            provider="higress_qwen",
        )
        client.return_value.rank.assert_called_once_with("query", ["first", "second"], 1)
        self.assertEqual(result[0].node.node_id, nodes[1].node.node_id)
        self.assertEqual(result[0].score, 0.9)


if __name__ == "__main__":
    unittest.main()
