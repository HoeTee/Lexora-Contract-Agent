from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from threading import BoundedSemaphore, Lock
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import yaml
from docx import Document
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel

import config
from agents.base_agent import Agent, Settings
from endpoints.review import task_store
from endpoints.review.review_state import ReviewStateStore
from loggers.call_log import logical_call, map_with_context, new_call, reset_call_context, set_call_context
from loggers.trace_logger import NOOP_TRACE
from routing.config import RouteConfig
from tools.docxindex.indexing.config import IndexingConfig
from tools.docxindex.indexing.token_budget import estimate_tokens
from tools.docxindex.llm.client import LLMClient, LLMClientError, LLMResponseValidationError, LLMSettings
from tools.docxindex.llm.schemas import RerankResponse
from tools.docxindex.retrieval.config import RetrievalConfig
from tools.docxindex.retrieval.rerank import _candidate_batches, _rerank_prompt, rerank_matches
from tools.docxindex.retrieval.scan import scan_matches
from tools.docxindex.retriever import DocxIndexRetriever
from tools.docxindex.vector.client import EmbeddingClient, EmbeddingSettings
from tools.document.reporting.docx_report import DocxReportGenerator


class Answer(BaseModel):
    answer: str


def chat_response(content='{"answer":"ok"}', usage=None):
    return {"id": "test", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": usage}


def prepare_client(client, sdk):
    client.client = sdk
    client._limiter = BoundedSemaphore(2)
    client._start_lock = Lock()
    client._next_start = 0.0
    client._start_interval_seconds = 0.0
    client._rate_limit_retries = 1
    return client


class LogicalCallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = Path(self.temp.name) / "logs"
        token = set_call_context(self.logs / "events.log", criterion_id="C1", stage="route.1.region")
        self.addCleanup(reset_call_context, token)
        for target in ("tools.docxindex.llm.client._global_model_slot", "tools.docxindex.vector.client._global_model_slot"):
            patcher = patch(target, return_value=nullcontext())
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("tools.docxindex.llm.client.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, handler):
        sdk = OpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(sdk.close)
        client = LLMClient.__new__(LLMClient)
        client.settings = LLMSettings(model="fake")
        return prepare_client(client, sdk)

    def records(self, name="logical_calls"):
        return [json.loads(path.read_text(encoding="utf-8")) for path in (self.logs / name).glob("*.json")]

    def test_schema_and_http_retries_share_one_logical_call_in_both_modes(self):
        initial_ids = set()
        for detailed in (False, True):
            with self.subTest(detailed=detailed), patch("loggers.call_log.LOGGING_DETAILED", detailed):
                sequence = iter([
                    httpx.Response(429, json={"error": {"message": "rate limited"}}),
                    httpx.Response(200, json=chat_response("invalid", {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80})),
                    httpx.Response(201, json=chat_response(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})),
                ])
                client = self.client(lambda request, sequence=sequence: next(sequence))
                result = client.complete_model("query", Answer, retries=1)
                self.assertEqual(result.answer, "ok")
                record = next(r for r in self.records() if r["logical_call_id"] not in initial_ids)
                self.assertEqual((record["retry_count"], record["input_tokens"], record["output_tokens"], record["input_output_tokens"]), (2, 100, 20, 120))
                self.assertEqual(record["source"], "docxindex_C1")
                self.assertEqual(record["category"], "docxindex.retrieval.llm")
                self.assertIsNone(record["error"])
                self.assertEqual(set(record), {"logical_call_id", "category", "source", "model", "started_at", "finished_at", "status", "retry_count", "input_tokens", "output_tokens", "input_output_tokens", "error"})
                attempts = sorted(self.records("calls"), key=lambda r: r["attempt"])
                if detailed:
                    self.assertEqual([r["attempt"] for r in attempts], [1, 2, 3])
                    self.assertEqual([r["is_retry"] for r in attempts], [False, True, True])
                    self.assertEqual({r["logical_call_id"] for r in attempts}, {record["logical_call_id"]})
                    self.assertIsNone(attempts[1]["input_tokens"])
                    self.assertEqual(attempts[1]["failure_stage"], "output_validation")
                    self.assertEqual(attempts[2]["http_status"], 201)
                else:
                    self.assertFalse((self.logs / "calls").exists())
                initial_ids = {r["logical_call_id"] for r in self.records()}
        self.assertFalse(list(self.logs.rglob("model_usage_summary.json")))
        self.assertFalse(list(self.logs.rglob("*.tmp")))

    def test_exhausted_validation_has_null_tokens_and_real_http_status(self):
        client = self.client(lambda request: httpx.Response(200, json=chat_response("invalid", {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12})))
        with self.assertRaises(LLMResponseValidationError):
            client.complete_model("query", Answer, retries=1)
        record, = self.records()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["retry_count"], 1)
        self.assertEqual(record["error"]["http_status"], 200)
        self.assertIsNone(record["input_output_tokens"])
        self.assertLessEqual(len(record["error"]["message"]), 200)

    def test_timeout_has_no_status_or_tokens(self):
        def fail(request):
            raise httpx.ReadTimeout("timeout", request=request)
        with self.assertRaises(LLMClientError):
            self.client(fail).complete("query")
        record, = self.records()
        self.assertEqual(record["retry_count"], 0)
        self.assertIsNone(record["error"]["http_status"])
        self.assertIsNone(record["input_tokens"])
        self.assertIsNone(record["output_tokens"])

    def test_exhausted_http_retries_are_counted(self):
        with self.assertRaises(LLMClientError):
            self.client(lambda request: httpx.Response(429, json={"error": {"message": "rate limit"}})).complete("query")
        record, = self.records()
        self.assertEqual(record["retry_count"], 1)
        self.assertEqual(record["error"]["http_status"], 429)
        self.assertIsNone(record["input_tokens"])

    def test_semaphore_failure_does_not_count_unsent_retry(self):
        client = self.client(lambda request: self.fail("request must not be sent"))
        with patch("tools.docxindex.llm.client._global_model_slot", side_effect=TimeoutError("lease wait timeout")):
            with self.assertRaises(LLMClientError):
                client.complete("query")
        self.assertEqual(self.records()[0]["retry_count"], 0)

    def test_timeout_after_invalid_response_does_not_inherit_previous_http_status(self):
        client = self.client(lambda request: httpx.Response(200, json=chat_response("invalid")))
        with patch("tools.docxindex.llm.client._global_model_slot", side_effect=[nullcontext(), TimeoutError("lease wait timeout")]):
            with self.assertRaises(LLMClientError) as raised:
                client.complete_model("query", Answer, retries=1)
        record, = self.records()
        self.assertIsNone(record["error"]["http_status"])
        self.assertEqual(record["retry_count"], 0)
        self.assertEqual(raised.exception.call_id, record["logical_call_id"])
        self.assertTrue(Path(raised.exception.call_path).exists())

    def test_success_without_usage_does_not_estimate_tokens(self):
        self.client(lambda request: httpx.Response(200, json=chat_response())).complete("query")
        record, = self.records()
        self.assertEqual(record["status"], "succeeded")
        self.assertIsNone(record["input_tokens"])
        self.assertIsNone(record["output_tokens"])
        self.assertIsNone(record["input_output_tokens"])

    def test_scan_batch_retry_keeps_one_logical_call(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                raise httpx.ReadTimeout("timeout", request=request)
            return httpx.Response(200, json=chat_response('{"findings":[]}', {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}))
        client = self.client(handler)
        index = {"nodes": [{"node_id": "body/a"}], "anchor_map": {"p_0001": {"node_id": "body/a", "text": "text", "body_child_index": 1}}}
        with patch("tools.docxindex.retrieval.scan.time.sleep"):
            scan_matches(index, "query", client, concurrency=1)
        record, = self.records()
        self.assertEqual((record["status"], record["retry_count"], record["input_output_tokens"]), ("succeeded", 1, 22))

    def test_embedding_records_input_only(self):
        sdk = OpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
                         "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                         "model": "fake", "object": "list", "usage": {"prompt_tokens": 11, "total_tokens": 11}}))))
        self.addCleanup(sdk.close)
        client = EmbeddingClient.__new__(EmbeddingClient)
        client.settings = EmbeddingSettings(model="fake")
        prepare_client(client, sdk)
        self.assertEqual(client.embed_batch(["text"]), [[0.1, 0.2]])
        record, = self.records()
        self.assertEqual(record["category"], "docxindex.retrieval.embedding")
        self.assertEqual(record["input_tokens"], 11)
        self.assertIsNone(record["output_tokens"])
        self.assertEqual(record["input_output_tokens"], 11)

    def test_concurrent_calls_are_separate_and_atomic(self):
        def run(item):
            with logical_call("docxindex.llm", "fake"):
                record = new_call(component="docxindex.llm", model="fake", request={})
                record.sent()
                record.succeeded({"usage": {"prompt_tokens": item, "completion_tokens": 1}})
        with ThreadPoolExecutor(max_workers=4) as pool:
            map_with_context(pool, run, range(20))
        records = self.records()
        self.assertEqual(len(records), 20)
        self.assertEqual(len({r["logical_call_id"] for r in records}), 20)
        self.assertEqual(sorted(r["input_tokens"] for r in records), list(range(20)))
        self.assertFalse(list(self.logs.rglob("*.tmp")))

    def test_rerank_requires_all_candidates_and_retries_with_one_id(self):
        responses = iter([
            {"nodes": [{"node_id": "a", "score": 0.8}]},
            {"nodes": [{"node_id": "a", "score": 0.8}, {"node_id": "b", "score": 0.9}]},
        ])
        client = self.client(lambda request: httpx.Response(200, json=chat_response(json.dumps(next(responses)), {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23})))
        ranked = rerank_matches({"nodes": []}, "query", [{"node_id": "a"}, {"node_id": "b"}], client, 20_000, None, max_selected_nodes=1)
        self.assertEqual([r["node_id"] for r in ranked], ["b", "a"])
        record, = self.records()
        self.assertEqual((record["category"], record["retry_count"], record["input_output_tokens"]), ("docxindex.retrieval.reranker", 1, 23))

    def test_logging_disabled_does_not_create_logical_or_detailed_files(self):
        with patch("loggers.call_log.LOGGING_ENABLED", False), patch("loggers.call_log.LOGGING_DETAILED", True):
            self.client(lambda request: httpx.Response(200, json=chat_response())).complete("query")
        self.assertFalse(self.logs.exists())


class MinimalLoggingTests(unittest.TestCase):
    def test_task_manifest_points_to_logical_calls_without_trace(self):
        with tempfile.TemporaryDirectory() as tmp, patch("endpoints.review.task_store.DATA_DIR", tmp), patch("endpoints.review.task_store.LOGGING_DETAILED", False):
            client, task = "api", "20261012-120000-a003"
            source = Path(tmp) / "source.docx"
            source.write_bytes(b"source")
            output = task_store.output_dir(client, task) / "result.docx"
            task_store.ensure_task_dirs(client, task)
            manifest = task_store.create_task(client_id="test", client_dir=client, task_id=task, contract_filename=source.name, contract_path=source, criteria_source="mandatory", criteria_filename=None, criteria_path=None, result_filename=output.name, result_path=output)
            self.assertEqual(manifest["logs"]["logical_calls_dir"], str(task_store.logs_dir(client, task) / "logical_calls"))
            for key in ("calls_dir", "trace_path", "route_events_dir", "conversation_log_dir", "mcp_log_path", "annotation_events_path", "review_outputs_path"):
                self.assertIsNone(manifest["logs"][key])
            self.assertIsNone(manifest["state"]["docxindex_dir"])

    def test_basic_paths_and_recovery_keep_only_required_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch("endpoints.review.task_store.DATA_DIR", tmp), patch("endpoints.review.task_store.LOGGING_DETAILED", False), patch("endpoints.review.review_state.LOGGING_DETAILED", False):
            client, task = "api", "20261012-120000-a001"
            task_store.ensure_task_dirs(client, task)
            store = ReviewStateStore(client, task)
            store.save_plan([{"id": "C1"}])
            store.save_criterion_input({"id": "C1"}, attempt=1, max_attempts=2)
            store.save_criterion_error({"id": "C1"}, TimeoutError(), attempt=1, max_attempts=2)
            store.save_criterion_output({"criterion_id": "C1", "tokens": 42, "issues": []}, attempt=1)
            self.assertEqual(store.load_criterion_output("C1")["tokens"], 42)
            self.assertIsNotNone(store.load_plan())
            self.assertFalse(store.criterion_input_path("C1").exists())
            self.assertFalse(store.criterion_error_path("C1").exists())
            for func in (task_store.task_trace_path, task_store.task_review_outputs_path, task_store.task_annotation_events_path, task_store.task_conversation_log_dir, task_store.task_mcp_log_file):
                self.assertIsNone(func(client, task))
            logs = task_store.logs_dir(client, task)
            self.assertEqual({p.name for p in logs.iterdir()}, {"logical_calls"})

    def test_disabled_logs_do_not_disable_business_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, patch("endpoints.review.task_store.DATA_DIR", tmp), patch("endpoints.review.task_store.LOGGING_ENABLED", False):
            store = ReviewStateStore("api", "20261012-120000-a002")
            store.save_criterion_output({"criterion_id": "C1"}, attempt=1)
            self.assertIsNotNone(store.load_criterion_output("C1"))
            self.assertIsNone(task_store.task_api_events_path(store.client_dir, store.task_id))

    def test_annotation_statistics_are_available_without_log_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch("tools.document.reporting.docx_report.LOGGING_DETAILED", False):
            root = Path(tmp)
            source = root / "source.docx"
            doc = Document()
            doc.add_paragraph("合同文本")
            doc.save(source)
            stats = {}
            output = DocxReportGenerator.generate_annotated_docx(str(source), [{"criterion_id": "C1", "issues": []}], str(root / "result.docx"), annotation_events_path=str(root / "annotation_events.json"), annotation_summary=stats)
            self.assertIsNotNone(output)
            self.assertIn("total_issues", stats["stats"])
            self.assertEqual(stats["skipped_count"], 0)
            self.assertFalse((root / "annotation_events.json").exists())

    def test_simplified_config_preserves_defaults_and_optional_overrides(self):
        self.assertEqual(IndexingConfig.from_sources(Path(config.CONFIG_PATH)), IndexingConfig())
        self.assertEqual(set(RouteConfig.load().methods), {"title", "region", "rule", "scan", "hybrid"})
        self.assertEqual((config.CHUNK_SIZE, config.CHUNK_OVERLAP, config.SIMILARITY_TOP_K, config.RERANK_TOP_N), (512, 100, 10, 5))
        shared = {"enabled": True, "redis_url": "redis://shared/2", "lease_seconds": 321, "limits": {"llm": 7, "embedding": 3, "reranker": 9}, "llm": {"key": "custom", "lease_seconds": 123}}
        with patch.dict(config.CONFIG, {"model_semaphore": shared}):
            llm = config._model_semaphore_config("llm", default_max=10)
            embed = config._model_semaphore_config("embedding", default_max=5)
        self.assertEqual((llm["max_concurrent_requests"], llm["lease_seconds"], llm["key"]), (7, 123, "custom"))
        self.assertEqual((embed["max_concurrent_requests"], embed["lease_seconds"], embed["key"]), (3, 321, "contract_agent:model:embedding:semaphore"))
        example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        self.assertFalse(example["logging"]["detailed"])
        self.assertNotIn("indexing", example["retrieval"]["docxindex"])


class GlobalRankingTests(unittest.TestCase):
    def test_full_coverage_routes_keep_more_nodes_than_top_k(self):
        routes = [
            {"method": "title", "all_titles": True},
            {"method": "region", "regions": ["body"], "coverage": "complete"},
            {"method": "scan"},
        ]
        for route in routes:
            with self.subTest(route=route):
                retriever = DocxIndexRetriever.__new__(DocxIndexRetriever)
                retriever._index = {"nodes": []}
                retriever.llm_client = object()
                retriever.route_config = RouteConfig()
                retriever.retrieval_config = RetrievalConfig(semantic={"max_selected_nodes": 1})
                rows = [{"node_id": f"node{i}"} for i in range(3)]
                with patch("tools.docxindex.retriever.title_matches", return_value=rows), patch("tools.docxindex.retriever._complete_scope_matches", return_value=rows), patch("tools.docxindex.retriever.scan_matches", return_value=rows), patch("tools.docxindex.retriever.rerank_matches") as rank, patch("tools.docxindex.retriever.build_content_context", return_value={"content_context": [], "pagination": {"next_part": None, "has_more": False}}) as content, patch("tools.docxindex.retriever._format_anchor_context", return_value="text"), patch("tools.docxindex.retriever._route_event"):
                    retriever._search_sync("query", {"steps": [route]})
                rank.assert_not_called()
                self.assertEqual(len(content.call_args.args[1]), 3)

    def test_rerank_disabled_caps_merged_candidates_without_model_call(self):
        retriever = DocxIndexRetriever.__new__(DocxIndexRetriever)
        retriever._index = {"nodes": []}
        retriever.route_config = RouteConfig()
        retriever.retrieval_config = RetrievalConfig(semantic={"max_selected_nodes": 1}, rerank={"enabled": False})
        with patch.object(retriever, "_semantic_matches", return_value=[{"node_id": "a"}, {"node_id": "b"}]), patch("tools.docxindex.retriever.rerank_matches") as rank, patch("tools.docxindex.retriever.build_content_context", return_value={"content_context": [], "pagination": {"next_part": None, "has_more": False}}) as content, patch("tools.docxindex.retriever._format_anchor_context", return_value="text"), patch("tools.docxindex.retriever._route_event") as event:
            retriever._search_sync("query", {"steps": [{"method": "hybrid"}]})
        rank.assert_not_called()
        self.assertEqual([r["node_id"] for r in content.call_args.args[1]], ["a"])
        self.assertFalse(event.call_args.kwargs["rerank_enabled"])
        self.assertFalse(event.call_args.kwargs["rerank_triggered"])

    def test_all_steps_are_deduplicated_before_global_ranking_and_cap(self):
        retriever = DocxIndexRetriever.__new__(DocxIndexRetriever)
        retriever._index = {"nodes": []}
        retriever.retrieval_config = RetrievalConfig(semantic={"max_selected_nodes": 2})
        retriever.route_config = RouteConfig()
        retriever.llm_client = SimpleNamespace(settings=SimpleNamespace(model="fake"))
        groups = [[{"node_id": "early", "sources": ["structure"]}, {"node_id": "shared", "sources": ["structure"]}],
                  [{"node_id": "shared", "sources": ["vector"]}, {"node_id": "late", "sources": ["vector"]}]]
        with patch.object(retriever, "_semantic_matches", side_effect=groups), patch("tools.docxindex.retriever.rerank_matches", side_effect=lambda i, q, rows, *a, **kw: list(reversed(rows))) as rank, patch("tools.docxindex.retriever.build_content_context", return_value={"content_context": [], "pagination": {"next_part": None, "has_more": False}}) as content, patch("tools.docxindex.retriever._format_anchor_context", return_value="text"), patch("tools.docxindex.retriever._route_event") as event:
            retriever._search_sync("query", {"steps": [{"method": "region", "regions": ["body"], "coverage": "relevant"}, {"method": "region", "regions": ["attachments"], "coverage": "relevant"}]})
        rank.assert_called_once()
        candidates = rank.call_args.args[2]
        self.assertEqual([r["node_id"] for r in candidates], ["early", "shared", "late"])
        self.assertEqual(candidates[1]["sources"], ["region", "structure", "vector"])
        self.assertEqual([r["node_id"] for r in content.call_args.args[1]], ["late", "shared"])
        self.assertEqual(event.call_args.kwargs["candidate_count_before_rerank"], 3)
        self.assertEqual(event.call_args.kwargs["selected_node_count"], 2)
        self.assertTrue(event.call_args.kwargs["rerank_enabled"])
        self.assertTrue(event.call_args.kwargs["rerank_triggered"])

    def test_budget_batches_include_late_candidates_and_rank_finalists_again(self):
        candidates = [{"node_id": f"node{i:02d}", "title": "candidate"} for i in range(20)]
        index = {"nodes": candidates}
        views = _candidate_batches(index, "query", candidates[:3], 20_000)[0]
        budget = estimate_tokens(_rerank_prompt("query", json.dumps(views, ensure_ascii=False))) + 2
        prompts = []
        def complete(prompt, schema):
            prompts.append(prompt)
            rows = json.loads(prompt.split("候选 node：\n", 1)[1].split("\n\n重排序规则：", 1)[0])
            return RerankResponse(nodes=[{"node_id": row["node_id"], "score": 0.9} for row in sorted(rows, key=lambda row: row["node_id"], reverse=True)])
        client = SimpleNamespace(settings=SimpleNamespace(model="fake"), complete_model=complete)
        ranked = rerank_matches(index, "query", candidates, client, budget, None, max_selected_nodes=2)
        self.assertIn("node19", {r["node_id"] for r in ranked[:2]})
        self.assertTrue(all(estimate_tokens(prompt) <= budget for prompt in prompts))
        self.assertTrue(all(any(candidate["node_id"] in prompt for prompt in prompts) for candidate in candidates))
        self.assertGreater(len(prompts), 2)


class AgentRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_retries_are_observable_and_keep_role_source(self):
        @asynccontextmanager
        async def semaphore(**kwargs):
            yield SimpleNamespace(enabled=False)
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(500, json={"error": {"message": "retry"}})
            return httpx.Response(200, json=chat_response(usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}))
        with tempfile.TemporaryDirectory() as tmp, patch("agents.base_agent.llm_global_semaphore", semaphore), patch("agents.base_agent.MODEL_CALL_MAX_RETRIES", 1), patch("agents.base_agent._model_retry_delay", return_value=0):
            sdk = AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            agent = Agent.__new__(Agent)
            agent.client, agent.settings, agent.name, agent.messages = sdk, Settings(api_key="fake", model="fake"), "Reflector_C1", []
            token = set_call_context(Path(tmp) / "logs" / "events.log", stage="agent")
            try:
                async with NOOP_TRACE.span() as span:
                    await agent._request_completion({"model": "fake", "messages": []}, span)
            finally:
                reset_call_context(token)
                await sdk.close()
            records = list((Path(tmp) / "logs" / "logical_calls").glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual((record["source"], record["category"], record["retry_count"], record["input_output_tokens"]), ("Reflector_C1", "agent", 1, 9))


if __name__ == "__main__":
    unittest.main()
