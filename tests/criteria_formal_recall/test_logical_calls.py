from __future__ import annotations

import json
import asyncio
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import yaml
from docx import Document
from openai import AsyncOpenAI
from pydantic import BaseModel

import config
from agents.base_agent import Agent, Settings
from agents.json_utils import AgentOutputValidationError, agent_call_purpose, chat_until_valid_json
from agents.reflector import ReflectorAgent
from endpoints.review import task_store
from endpoints.review.review_state import ReviewStateStore
from endpoints.runtime import model_budget
from endpoints.runtime.model_budget import ModelCallBudget
from loggers.call_log import (
    LogicalCall, call_stage, criterion_call_context, current_call_context,
    logical_call, map_with_context, new_call, reset_call_context, set_call_context,
)
from loggers.trace_logger import NOOP_TRACE
from routing.config import RouteConfig
from tools.docxindex.indexing.config import IndexingConfig
from tools.docxindex.llm.client import LLMClient, LLMClientError, LLMResponseValidationError, LLMSettings
from tools.docxindex.reranker import RerankerClient
from tools.docxindex.retrieval.config import RetrievalConfig
from tools.docxindex.retrieval.llm_query import llm_query
from tools.docxindex.retrieval.rerank import _candidate_batches, _rerank_tokens, rerank_matches
from tools.docxindex.retrieval.scan import scan_matches
from tools.docxindex.retriever import DocxIndexRetriever
from tools.docxindex.vector.client import EmbeddingClient, EmbeddingSettings
from tools.document.reporting.docx_report import DocxReportGenerator
from tests.criteria_formal_recall._model_test_support import available_slot, model_transport, reranker_transport


class Answer(BaseModel):
    answer: str


def chat_response(content='{"answer":"ok"}', usage=None):
    return {"id": "test", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": usage}


def assert_attempt_totals(case, record):
    sent = [attempt for attempt in record["attempts"] if attempt["sent"]]
    inputs = sum(attempt.get("input_tokens") or 0 for attempt in sent)
    outputs = sum(attempt.get("output_tokens") or 0 for attempt in sent)
    case.assertEqual(record["known_input_tokens"], inputs)
    case.assertEqual(record["known_output_tokens"], outputs)
    case.assertEqual(record["input_output_tokens"], inputs + outputs)


class LogicalCallTests(unittest.TestCase):
    def setUp(self):
        # These accounting fixtures exercise a declared two-send allowance.
        budget = patch("loggers.call_log.ModelCallBudget", side_effect=lambda: ModelCallBudget(max_retries=1))
        budget.start()
        self.addCleanup(budget.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = Path(self.temp.name) / "logs"
        token = set_call_context(self.logs / "events.log", criterion_id="C1", stage="route.1.region")
        self.addCleanup(reset_call_context, token)

    def client(self, handler):
        transport = model_transport("tools.docxindex.llm.client", handler)
        transport.__enter__()
        self.addCleanup(transport.__exit__, None, None, None)
        return LLMClient(LLMSettings(model="fake", base_url="https://model.test/v1", api_key="fake"))

    def records(self, name="logical_calls"):
        return [json.loads(path.read_text(encoding="utf-8")) for path in (self.logs / name).glob("*.json")]

    def test_schema_and_http_retries_share_one_send_allowance_in_both_modes(self):
        initial_ids = set()
        for detailed in (False, True):
            with self.subTest(detailed=detailed), patch("loggers.call_log.LOGGING_DETAILED", detailed):
                sequence = iter([
                    httpx.Response(429, json={"error": {"message": "rate limited"}}),
                    httpx.Response(200, json=chat_response("invalid", {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80})),
                ])
                client = self.client(lambda request, sequence=sequence: next(sequence))
                with self.assertRaises(LLMResponseValidationError):
                    client.complete_model("query", Answer)
                record = next(r for r in self.records() if r["logical_call_id"] not in initial_ids)
                self.assertEqual(record["retry_count"], 1)
                assert_attempt_totals(self, record)
                self.assertEqual(record["source"], "docxindex_C1")
                self.assertEqual(record["category"], "docxindex.retrieval.llm")
                self.assertEqual(record["status"], "failed")
                self.assertEqual(len(record["attempts"]), 2)
                self.assertEqual([r["send_number"] for r in record["attempts"]], [1, 2])
                self.assertGreater(record["known_input_tokens"], 50)
                self.assertEqual(record["known_output_tokens"], 30)
                self.assertFalse(record["usage_complete"])
                attempts = sorted((r for r in self.records("calls") if r["logical_call_id"] == record["logical_call_id"]), key=lambda r: r["attempt"])
                if detailed:
                    self.assertEqual([r["attempt"] for r in attempts], [1, 2])
                    self.assertEqual([r["is_retry"] for r in attempts], [False, True])
                    self.assertEqual({r["logical_call_id"] for r in attempts}, {record["logical_call_id"]})
                    self.assertEqual(attempts[1]["input_tokens"], 50)
                    self.assertEqual(attempts[1]["failure_stage"], "output_validation")
                    self.assertEqual(attempts[1]["http_status"], 200)
                else:
                    self.assertFalse((self.logs / "calls").exists())
                initial_ids = {r["logical_call_id"] for r in self.records()}
        self.assertFalse(list(self.logs.rglob("model_usage_summary.json")))
        self.assertFalse(list(self.logs.rglob("*.tmp")))

    def test_exhausted_validation_preserves_tokens_and_real_http_status(self):
        client = self.client(lambda request: httpx.Response(200, json=chat_response("invalid", {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12})))
        with self.assertRaises(LLMResponseValidationError):
            client.complete_model("query", Answer)
        record, = self.records()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["retry_count"], 1)
        self.assertEqual(record["error"]["http_status"], 200)
        assert_attempt_totals(self, record)
        self.assertLessEqual(len(record["error"]["message"]), 200)

    def test_timeout_preserves_input_and_unknown_output(self):
        def fail(request):
            raise httpx.ReadTimeout("timeout", request=request)
        with self.assertRaises(LLMClientError):
            self.client(fail).complete("query")
        record, = self.records()
        self.assertEqual(record["retry_count"], 1)
        self.assertIsNone(record["error"]["http_status"])
        self.assertGreater(record["input_tokens"], 0)
        self.assertIsNone(record["output_tokens"])

    def test_exhausted_http_retries_are_counted(self):
        with self.assertRaises(LLMClientError):
            self.client(lambda request: httpx.Response(429, json={"error": {"message": "rate limit"}})).complete("query")
        record, = self.records()
        self.assertEqual(record["retry_count"], 1)
        self.assertEqual(record["error"]["http_status"], 429)
        self.assertGreater(record["input_tokens"], 0)

    def test_semaphore_failure_does_not_count_unsent_retry(self):
        client = self.client(lambda request: self.fail("request must not be sent"))
        with patch("endpoints.runtime.model_calls.model_semaphore", side_effect=TimeoutError("lease wait timeout")):
            with self.assertRaises(LLMClientError):
                client.complete("query")
        self.assertEqual(self.records()[0]["retry_count"], 0)

    def test_timeout_after_invalid_response_does_not_inherit_previous_http_status(self):
        client = self.client(lambda request: httpx.Response(200, json=chat_response("invalid")))
        with patch("loggers.call_log.LOGGING_DETAILED", False), \
                patch("endpoints.runtime.model_calls.model_semaphore", side_effect=[available_slot(), TimeoutError("lease wait timeout")]):
            with self.assertRaises(LLMClientError) as raised:
                client.complete_model("query", Answer)
        record, = self.records()
        self.assertIsNone(record["error"]["http_status"])
        self.assertEqual(record["retry_count"], 0)
        self.assertEqual(raised.exception.call_id, record["logical_call_id"])
        self.assertTrue(Path(raised.exception.call_path).exists())

    def test_success_without_usage_records_explicit_local_estimates(self):
        self.client(lambda request: httpx.Response(200, json=chat_response())).complete("query")
        record, = self.records()
        self.assertEqual(record["status"], "succeeded")
        self.assertGreater(record["input_tokens"], 0)
        self.assertGreater(record["output_tokens"], 0)
        self.assertFalse(record["usage_complete"])
        self.assertEqual(record["attempts"][0]["output_token_source"], "local")
        assert_attempt_totals(self, record)

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
        scan_matches(index, "query", client, concurrency=1)
        record, = self.records()
        self.assertEqual((record["status"], record["retry_count"]), ("succeeded", 1))
        self.assertGreater(record["input_tokens"], 20)
        assert_attempt_totals(self, record)

    def test_one_scan_step_records_each_batch_and_preserves_thread_context(self):
        client = self.client(lambda request: httpx.Response(200, json=chat_response('{"findings":[]}')))
        index = {"nodes": [{"node_id": "body/a"}], "anchor_map": {
            f"p_{number}": {"node_id": "body/a", "text": "contract paragraph " * 3, "body_child_index": number}
            for number in range(1, 4)}}
        token = set_call_context(self.logs / "events.log", criterion_id="H03", stage="route.2.scan")
        try:
            scan_matches(index, "query", client, batch_tokens=1, concurrency=3)
        finally:
            reset_call_context(token)
        records = self.records()
        self.assertEqual({r["stage"] for r in records}, {f"scan-step2-batch{i}" for i in range(1, 4)})
        self.assertEqual({r["source"] for r in records}, {"docxindex_H03"})
        self.assertEqual({r["retry_count"] for r in records}, {0})

    def test_structure_parts_are_distinguishable_in_logical_calls(self):
        client = self.client(lambda request: httpx.Response(200, json=chat_response('{"nodes":[]}')))
        index = {"nodes": [], "structure_tree": [
            {"node_id": f"body/{i}", "title": f"section {i}", "summary": "summary", "children": []}
            for i in range(2)]}
        llm_query(index, ["query"], client, None, input_tokens=1)
        self.assertEqual({r["stage"] for r in self.records()}, {"structure-step1-part1", "structure-step1-part2"})

    def test_index_call_names_distinguish_model_purpose(self):
        token = set_call_context(self.logs / "events.log", stage="indexing")
        try:
            for kind in ("llm", "embedding"):
                with call_stage(f"index-{kind}-batch1"), logical_call(f"docxindex.{kind}", "fake"):
                    record = new_call(component=f"docxindex.{kind}", model="fake", request={})
                    record.sent()
                    record.succeeded({"usage": {"prompt_tokens": 2, "completion_tokens": 1}})
        finally:
            reset_call_context(token)
        self.assertEqual({r["source"] for r in self.records()}, {"docxindex_indexing_llm", "docxindex_indexing_embedding"})
        self.assertEqual({p.name for p in (self.logs / "logical_calls").glob("*.json")}, {
            "1__docxindex.indexing.llm__docxindex_indexing_llm__index-llm-batch1.json",
            "2__docxindex.indexing.embedding__docxindex_indexing_embedding__index-embedding-batch1.json",
        })

    def test_embedding_records_input_only(self):
        def handler(request):
            return httpx.Response(200, json={
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "model": "fake", "object": "list", "usage": {"prompt_tokens": 11, "total_tokens": 11}})
        client = EmbeddingClient(EmbeddingSettings(model="fake"))
        with model_transport("tools.docxindex.vector.client", handler):
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
        self.assertEqual(sorted(r["logical_no"] for r in records), list(range(1, 21)))
        self.assertEqual(sorted(r["input_tokens"] for r in records), list(range(20)))
        self.assertFalse(list(self.logs.rglob("*.tmp")))

    def test_rerank_requires_all_candidates_and_retries_with_one_id(self):
        responses = iter([
            {"results": [{"index": 0, "relevance_score": 0.8}]},
            {"results": [{"index": 0, "relevance_score": 0.8}, {"index": 1, "relevance_score": 0.9}]},
        ])
        sent = []
        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=next(responses))
        client = RerankerClient(model="fake-reranker", base_url="https://model.test/rerank", provider="higress_qwen")
        with reranker_transport(handler):
            ranked = rerank_matches({"nodes": []}, "query", [{"node_id": "a"}, {"node_id": "b"}], client, 20_000, None, max_selected_nodes=1)
        self.assertEqual([r["node_id"] for r in ranked], ["b", "a"])
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(item["model"] == "fake-reranker" and item["top_n"] == 2 for item in sent))
        record, = self.records()
        self.assertEqual((record["category"], record["retry_count"]), ("docxindex.retrieval.reranker", 1))
        assert_attempt_totals(self, record)
        self.assertEqual({attempt["model_type"] for attempt in record["attempts"]}, {"reranker"})

    def test_logging_disabled_does_not_create_logical_or_detailed_files(self):
        with patch("loggers.call_log.LOGGING_ENABLED", False), patch("loggers.call_log.LOGGING_DETAILED", True):
            self.client(lambda request: httpx.Response(200, json=chat_response())).complete("query")
        self.assertFalse(self.logs.exists())

    def test_natural_number_and_criterion_attempt_are_shared_by_all_attempt_files(self):
        with patch("loggers.call_log.LOGGING_DETAILED", True), criterion_call_context("H04", 3):
            with logical_call("agent", "fake", source="SubAgent_H04", stage="review-initial") as logical:
                for count in (4, 7):
                    record = new_call(component="agent", model="fake", request={})
                    record.sent()
                    record.succeeded({"usage": {"prompt_tokens": count, "completion_tokens": 2}})
        expected = (1, "H04", 3)
        payload, = self.records()
        self.assertEqual((payload["logical_no"], payload["criterion_id"], payload["criterion_attempt"]), expected)
        self.assertEqual(logical.path.name, "1__agent__SubAgent_H04__review-initial.json")
        for attempt in payload["attempts"] + self.records("calls"):
            self.assertEqual((attempt["logical_no"], attempt["criterion_id"], attempt["criterion_attempt"]), expected)
            self.assertEqual(attempt["logical_call_id"], payload["logical_call_id"])
            self.assertEqual(attempt["category"], "agent")
        self.assertEqual(payload["known_input_tokens"], 11)

    def test_nested_and_resumed_schema_group_keeps_one_number_and_one_finish(self):
        with logical_call("agent", "fake", source="SubAgent_H04", finalize=False) as logical:
            with logical_call("docxindex.llm", "fake") as nested:
                self.assertIs(nested, logical)
            record = new_call(component="agent", model="fake", request={})
            record.sent()
            record.succeeded({"usage": {"prompt_tokens": 4, "completion_tokens": 2}})
        logical.reject_response(ValueError("invalid JSON"))
        with logical_call("agent", "fake", record=logical, finalize=False):
            repaired = new_call(component="agent", model="fake", request={})
            repaired.sent()
            repaired.succeeded({"usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        logical.finish()
        logical.finish()
        payload, = self.records()
        self.assertEqual(payload["logical_no"], 1)
        self.assertEqual({item["logical_no"] for item in payload["attempts"]}, {1})
        events = (self.logs / "events.log").read_text(encoding="utf-8")
        self.assertEqual(events.count("Operation started."), 1)
        self.assertEqual(events.count("failed JSON/schema validation."), 1)
        self.assertEqual(events.count("Validation passed."), 1)
        self.assertEqual(payload["attempts"][0]["failure_stage"], "output_validation")

    def test_failed_input_counts_survive_and_finish_marks_output_incomplete(self):
        with self.assertRaises(TimeoutError):
            with logical_call("agent", "fake", source="SubAgent_H04"):
                record = new_call(component="agent", model="fake", request={})
                record.record_local_input(17)
                record.sent()
                record.failed(TimeoutError("model timeout"))
                raise TimeoutError("model timeout")
        payload, = self.records()
        self.assertEqual((payload["known_input_tokens"], payload["attempts"][0]["input_tokens"]), (17, 17))
        self.assertIsNone(payload["attempts"][0]["output_tokens"])
        events = (self.logs / "events.log").read_text(encoding="utf-8")
        self.assertIn("Operation failed. Requests: 1. Total input: 17 tokens.", events)
        self.assertIn("Known output: 0 tokens (incomplete).", events)

    def test_repeated_finish_persists_outer_error_without_duplicate_event(self):
        logical = LogicalCall("agent", "fake", self.logs / "calls", "SubAgent_H04")
        logical.finish(ValueError("invalid response"))
        first, = self.records()
        before = (self.logs / "events.log").read_text(encoding="utf-8")
        logical.finish(AgentOutputValidationError("schema repair attempts exhausted"))
        final, = self.records()
        self.assertEqual(final["error"]["type"], "AgentOutputValidationError")
        self.assertEqual(final["error"]["message"], "schema repair attempts exhausted")
        self.assertEqual(final["finished_at"], first["finished_at"])
        self.assertEqual((self.logs / "events.log").read_text(encoding="utf-8"), before)

    def test_explicit_agent_directory_and_criterion_do_not_need_parent_context(self):
        token = set_call_context(None)
        try:
            logical = LogicalCall("agent", "fake", self.logs / "calls", "Reflector_H07",
                                  criterion_id="H07", criterion_attempt=2)
            logical.finish()
        finally:
            reset_call_context(token)
        payload, = self.records()
        self.assertEqual((payload["logical_no"], payload["criterion_id"], payload["criterion_attempt"]), (1, "H07", 2))
        self.assertTrue((self.logs / "events.log").exists())

    def test_explicit_logical_criterion_wins_over_outer_context_for_attempts(self):
        with logical_call("agent", "fake", source="Reflector_H07",
                          criterion_id="H07", criterion_attempt=2):
            record = new_call(component="agent", model="fake", request={})
            record.record_local_input(3)
            record.sent()
            record.succeeded({"usage": {"prompt_tokens": 3, "completion_tokens": 1}})
        payload, = self.records()
        self.assertEqual((payload["criterion_id"], payload["criterion_attempt"]), ("H07", 2))
        self.assertEqual((payload["attempts"][0]["criterion_id"], payload["attempts"][0]["criterion_attempt"]),
                         ("H07", 2))

    def test_criterion_context_preserves_paths_and_restores_parent_metadata(self):
        original = current_call_context()
        with criterion_call_context("H06", 2):
            context = current_call_context()
            self.assertEqual(context["directory"], original["directory"])
            self.assertEqual(context["events_path"], original["events_path"])
            self.assertEqual((context["criterion_id"], context["criterion_attempt"]), ("H06", 2))
            context["criterion_id"] = "must-not-mutate-context"
            self.assertEqual(current_call_context()["criterion_id"], "H06")
        self.assertEqual(current_call_context(), original)


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
            self.assertNotIn("tokens", store.load_criterion_output("C1"))
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
        self.assertEqual(IndexingConfig.from_sources(Path(config.CONFIG_PATH)).summary.batch_max_tokens, 20000)
        self.assertIsNone(IndexingConfig().summary.batch_max_tokens)
        self.assertEqual(set(RouteConfig.load().methods), {"title", "region", "rule", "scan", "hybrid"})
        self.assertEqual((config.CHUNK_SIZE, config.CHUNK_OVERLAP, config.SIMILARITY_TOP_K, config.RERANK_TOP_N), (512, 100, None, None))
        per_type = {"llm": {"max_concurrent_requests": 7, "redis_url": "redis://shared/2", "key": "custom", "lease_seconds": 123},
                    "embedding": {"max_concurrent_requests": 3, "redis_url": "redis://shared/2", "lease_seconds": 321}}
        with patch.dict(config.CONFIG, {"model_semaphore": per_type}):
            llm = config._model_semaphore_config("llm", default_max=10)
            embed = config._model_semaphore_config("embedding", default_max=5)
        self.assertEqual((llm["max_concurrent_requests"], llm["lease_seconds"], llm["key"]), (7, 123, "custom"))
        self.assertEqual((embed["max_concurrent_requests"], embed["lease_seconds"], embed["key"]), (3, 321, "contract_agent:model:embedding:semaphore"))
        example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        self.assertFalse(example["logging"]["detailed"])
        self.assertEqual(example["retrieval"]["docxindex"]["indexing"]["summary"]["batch_max_tokens"], 20000)


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
        retriever.reranker_client = SimpleNamespace(model="fake-reranker")
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

    def test_batches_include_late_candidates_without_ranking_finalists_again(self):
        candidates = [{"node_id": f"node{i:02d}", "title": "candidate"} for i in range(20)]
        index = {"nodes": candidates}
        views = _candidate_batches(index, "query", candidates[:3], 20_000)[0]
        budget = _rerank_tokens("query", json.dumps(views, ensure_ascii=False)) + 2
        batches = []
        def rank(query, documents):
            rows = [json.loads(document) for document in documents]
            batches.append(rows)
            return [(i, float(row["node_id"][4:])) for i, row in sorted(enumerate(rows), key=lambda item: item[1]["node_id"], reverse=True)]
        client = SimpleNamespace(model="fake-reranker", rank=rank)
        ranked = rerank_matches(index, "query", candidates, client, budget, None, max_selected_nodes=2)
        self.assertIn("node19", {r["node_id"] for r in ranked[:2]})
        self.assertTrue(all(len(rows) <= 10 for rows in batches))
        self.assertTrue(all(any(candidate["node_id"] in {row["node_id"] for row in batch} for batch in batches) for candidate in candidates))
        self.assertEqual(len(batches), 2)


class AgentRetryTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def harness(self, responses, *, detailed=False, criterion_id="H03", mcp_client=None,
                      timeout=None, response_delay=0, response_handler=None):
        sequence = iter(responses)
        async def handler(request):
            if response_handler is not None:
                return await response_handler(request)
            await asyncio.sleep(response_delay)
            return httpx.Response(200, json=next(sequence))

        sdk = AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                          http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        def budget_factory():
            return ModelCallBudget() if timeout is None else ModelCallBudget(timeout_seconds=timeout)
        with tempfile.TemporaryDirectory() as tmp, patch("agents.base_agent.AsyncOpenAI", return_value=sdk), patch("endpoints.runtime.model_calls.model_semaphore", available_slot), patch("loggers.call_log.LOGGING_DETAILED", detailed), patch("loggers.call_log.ModelCallBudget", side_effect=budget_factory):
            logs = Path(tmp) / "logs"
            token = set_call_context(logs / "events.log", stage="agent")
            agent = Agent(name=f"SubAgent_{criterion_id}", criterion_id=criterion_id,
                          settings=Settings(api_key="fake", model="fake"), mcp_client=mcp_client)
            agent.call_stage = "review-initial"
            try:
                yield agent, logs
            finally:
                reset_call_context(token)
                await sdk.close()

    def read_records(self, logs, directory="logical_calls"):
        return [json.loads(p.read_text(encoding="utf-8")) for p in (logs / directory).glob("*.json")]

    @patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", 2)
    async def test_three_send_schema_allowance_succeeds_on_third_send(self):
        async with self.harness([chat_response("invalid"), chat_response("{}"), chat_response()]) as (agent, logs):
            result, _ = await chat_until_valid_json(agent, "review", Answer, '{}')
            self.assertEqual(result, {"answer": "ok"})
            record, = self.read_records(logs)
            self.assertEqual(record["status"], "succeeded")
            self.assertEqual([attempt["send_number"] for attempt in record["attempts"]], [1, 2, 3])
            self.assertEqual(record["retry_count"], 2)

    @patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", 2)
    async def test_three_send_schema_allowance_never_sends_a_fourth_request(self):
        responses = [chat_response("invalid")] * 3 + [chat_response()]
        async with self.harness(responses) as (agent, logs):
            with self.assertRaisesRegex(AgentOutputValidationError, "after 3 attempts"):
                await chat_until_valid_json(agent, "review", Answer, '{}')
            record, = self.read_records(logs)
            self.assertEqual(record["status"], "failed")
            self.assertEqual([attempt["send_number"] for attempt in record["attempts"]], [1, 2, 3])

    async def test_json_schema_repairs_receive_independent_60_second_request_budgets(self):
        # Reproduce a 38-second invalid response, followed by two repairs. The
        # fake clock avoids waiting minutes while the real SDK parses responses.
        clock = [100.0]
        budgets = []
        sends = []
        request_budgets = []
        def budget_factory():
            budget = ModelCallBudget(timeout_seconds=60, max_retries=3, started=clock[0])
            budgets.append(budget)
            return budget
        async def handler(request):
            sends.append(request)
            request_budgets.append(budgets[0].remaining())
            clock[0] += (38, 50, 59)[len(sends) - 1]
            return httpx.Response(200, json=chat_response("invalid" if len(sends) < 3 else '{"answer":"ok"}'))
        budget_clock = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        with patch.object(model_budget, "time", budget_clock):
            async with self.harness([], response_handler=handler) as (agent, logs):
                with patch("loggers.call_log.ModelCallBudget", side_effect=budget_factory):
                    result, _ = await asyncio.wait_for(chat_until_valid_json(agent, "review", Answer, '{}'), timeout=5)
                record, = self.read_records(logs)
                self.assertEqual(result, {"answer": "ok"})
                self.assertEqual(len(budgets), 1)
                self.assertEqual(budgets[0].sends, 3)
                self.assertEqual(request_budgets, [60, 60, 60])
                self.assertEqual(len(sends), 3)
                self.assertEqual(record["status"], "succeeded")
                self.assertEqual([attempt["send_number"] for attempt in record["attempts"]], [1, 2, 3])
                self.assertTrue(all(attempt["remaining_seconds"] == 60 for attempt in record["attempts"]))

    async def test_agent_request_timeout_then_json_repair_share_send_count_only(self):
        sends = []
        cancelled = []
        async def handler(request):
            sends.append(request)
            if len(sends) == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            return httpx.Response(200, json=chat_response("invalid" if len(sends) == 2 else '{"answer":"ok"}'))
        async with self.harness([], response_handler=handler) as (agent, logs):
            with patch("loggers.call_log.ModelCallBudget", side_effect=lambda: ModelCallBudget(
                    timeout_seconds=3, max_retries=2)), \
                    patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
                result, _ = await chat_until_valid_json(agent, "review", Answer, '{}')
            self.assertEqual(result, {"answer": "ok"})
            self.assertEqual((len(sends), cancelled), (3, [True]))
            record, = self.read_records(logs)
            self.assertEqual((record["status"], record["send_count"], record["retry_count"]), ("succeeded", 3, 2))
            self.assertEqual([attempt["send_number"] for attempt in record["attempts"]], [1, 2, 3])
            self.assertEqual(record["attempts"][0]["failure_stage"], "request")
            self.assertEqual(record["attempts"][1]["failure_stage"], "output_validation")

    async def test_agent_schema_repairs_sum_all_attempts_in_both_modes(self):
        for detailed in (False, True):
            responses = [chat_response("not JSON", {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}),
                         chat_response(usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})]
            with self.subTest(detailed=detailed):
                async with self.harness(responses, detailed=detailed) as (agent, logs):
                    result, _ = await chat_until_valid_json(agent, "review", Answer, '{"answer":"ok"}')
                    self.assertEqual(result, {"answer": "ok"})
                    records = self.read_records(logs)
                    self.assertEqual(len(records), 1)
                    record = records[0]
                    self.assertEqual((record["source"], record["stage"], record["retry_count"], record["input_output_tokens"]),
                                     ("SubAgent_H03", "review-initial", 1, 139))
                    self.assertIn("JSON/schema", record["reason"])
                    self.assertLessEqual(len(record["reason"]), 200)
                    self.assertIsNone(record["error"])
                    self.assertEqual((record["known_input_tokens"], record["known_output_tokens"]), (107, 32))
                    self.assertTrue(record["usage_complete"])
                    self.assertEqual(next((logs / "logical_calls").glob("*.json")).name,
                                     "1__agent__SubAgent_H03__review-initial.json")
                    attempts = sorted(self.read_records(logs, "calls"), key=lambda x: x["attempt"])
                    if detailed:
                        self.assertEqual([r["is_retry"] for r in attempts], [False, True])
                        self.assertEqual(attempts[0]["error"]["http_status"], 200)
                        self.assertEqual(attempts[0]["input_tokens"], 100)
                        self.assertEqual({r["logical_call_id"] for r in attempts}, {record["logical_call_id"]})
                        self.assertEqual({r["criterion_id"] for r in attempts}, {"H03"})
                    else:
                        self.assertFalse((logs / "calls").exists())

    async def test_exhausted_schema_repairs_keep_usage_and_preserve_http_status(self):
        responses = [chat_response("invalid", {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})] * 2
        async with self.harness(responses) as (agent, logs):
            with self.assertRaises(AgentOutputValidationError) as caught:
                await chat_until_valid_json(agent, "review", Answer, '{}', max_attempts=2)
            record, = self.read_records(logs)
            self.assertEqual((record["status"], record["retry_count"], record["error"]["http_status"]), ("failed", 1, 200))
            assert_attempt_totals(self, record)
            self.assertEqual(caught.exception.call_id, record["logical_call_id"])
            self.assertTrue(Path(caught.exception.call_path).is_file())

    async def test_content_revision_remains_a_separate_call_with_reason(self):
        async with self.harness([chat_response(), chat_response()]) as (agent, logs):
            await chat_until_valid_json(agent, "initial", Answer, '{}')
            with agent_call_purpose(agent, "review-initial-output-repair1", "覆盖校验：遗漏原文 p1"):
                await chat_until_valid_json(agent, "repair", Answer, '{}')
            records = self.read_records(logs)
            self.assertEqual(len(records), 2)
            self.assertEqual({r["retry_count"] for r in records}, {0})
            repair = next(r for r in records if r["stage"].endswith("output-repair1"))
            self.assertEqual(repair["reason"], "覆盖校验：遗漏原文 p1")

    async def test_tool_and_followup_requests_do_not_share_schema_group(self):
        tool_response = chat_response(None, {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6})
        tool_response["choices"][0]["message"]["tool_calls"] = [{"id": "tool1", "type": "function", "function": {"name": "contract_search", "arguments": '{"query":"test","criterion_id":"wrong"}'}}]
        tool_response["choices"][0]["finish_reason"] = "tool_calls"
        async def tool_message(tool):
            args = json.loads(tool.function.arguments)
            self.assertEqual(args["criterion_id"], "H03")
            self.assertIn("events.log", args["api_events_path"])
            token = set_call_context(args["api_events_path"], criterion_id=args["criterion_id"], stage="route.1.region")
            try:
                with logical_call("docxindex.llm", "fake"):
                    request = new_call(component="docxindex.llm", model="fake", request={})
                    request.sent()
                    request.succeeded({"usage": {"prompt_tokens": 2, "completion_tokens": 1}})
            finally:
                reset_call_context(token)
            return {"content": "contract evidence"}
        mcp = SimpleNamespace(tools=[{"type": "function", "function": {"name": "contract_search", "parameters": {"type": "object", "properties": {}}}}], tool_message_from_call=tool_message)
        responses = [tool_response, chat_response("invalid"), chat_response(usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11})]
        async with self.harness(responses, mcp_client=mcp) as (agent, logs):
            result, _ = await chat_until_valid_json(agent, "initial", Answer, '{}')
            self.assertEqual(result, {"answer": "ok"})
            records = self.read_records(logs)
            self.assertEqual(len(records), 3)
            self.assertEqual(len({r["logical_call_id"] for r in records}), 3)
            followup = next(r for r in records if r["stage"].endswith("tool-result1"))
            self.assertEqual(followup["retry_count"], 1)
            self.assertGreater(followup["input_tokens"], 9)
            assert_attempt_totals(self, followup)
            self.assertEqual({r["category"] for r in records}, {"agent", "docxindex.retrieval.llm"})

    async def test_concurrent_reflectors_keep_criterion_and_round_separate(self):
        responses = [chat_response('{"status":"PASS","feedback":"","finding_decisions":[],"coverage_gaps":[]}')] * 2
        async with self.harness(responses, detailed=True) as (agent, logs):
            with patch("agents.base_agent.AsyncOpenAI", return_value=agent.client):
                reflectors = [ReflectorAgent(criterion_id=cid, settings=agent.settings) for cid in ("H01", "H02")]
            for reflector in reflectors:
                reflector.call_stage = "reflection-round2"
            await asyncio.gather(*(reflector.review("evidence") for reflector in reflectors))
            records = self.read_records(logs)
            self.assertEqual({r["source"] for r in records}, {"Reflector_H01", "Reflector_H02"})
            self.assertEqual({r["stage"] for r in records}, {"reflection-round2"})
            self.assertEqual({r["criterion_id"] for r in self.read_records(logs, "calls")}, {"H01", "H02"})

    async def test_successful_final_schema_fallback_keeps_usage(self):
        class StrictAnswer(BaseModel):
            answer: str
            extra: str
        async with self.harness([chat_response(usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})]) as (agent, logs):
            await chat_until_valid_json(agent, "review", StrictAnswer, '{}', max_attempts=1, final_attempt_schema=Answer)
            record, = self.read_records(logs)
            self.assertEqual((record["status"], record["input_output_tokens"]), ("succeeded", 9))

    async def test_sdk_retries_are_observable_and_keep_role_source(self):
        count = 0
        async def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(500, json={"error": {"message": "retry"}})
            return httpx.Response(200, json=chat_response(usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}))
        with tempfile.TemporaryDirectory() as tmp, patch("endpoints.runtime.model_calls.model_semaphore", available_slot), patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
            sdk = AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            agent = Agent.__new__(Agent)
            agent.client, agent.settings, agent.name, agent.messages = sdk, Settings(api_key="fake", model="fake"), "Reflector_C1", []
            agent.MAX_INPUT_TOKENS = None
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
            self.assertEqual((record["source"], record["category"], record["retry_count"]), ("Reflector_C1", "agent", 1))
            self.assertGreater(record["input_tokens"], 7)
            assert_attempt_totals(self, record)


if __name__ == "__main__":
    unittest.main()
