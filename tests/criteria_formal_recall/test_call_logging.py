from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from endpoints.runtime.model_calls import execute_model
from loggers.call_log import logical_call, map_with_context, new_call, reset_call_context, set_call_context, summarize_model_attempts
from loggers.trace_logger import TraceLogger, reset_current_trace, set_current_trace
from mcp_service.client.client import MinimalMCPClient
from tools.docxindex.llm.client import LLMClient, LLMClientError, LLMSettings
from tools.docxindex.retriever import DocxIndexRetriever
from tests.criteria_formal_recall._model_test_support import model_transport


class CallLoggingTests(unittest.TestCase):
    def setUp(self):
        for module in ("loggers.call_log", "loggers.trace_logger", "mcp_service.client.client"):
            patcher = patch(f"{module}.LOGGING_DETAILED", True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_docxindex_timeout_retains_input_without_inventing_http_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = Path(temp_dir) / "logs" / "events.log"
            token = set_call_context(events_path, operation_id="op-1", criterion_id="H12", stage="route.1.scan")
            client = LLMClient(LLMSettings(model="fake"))
            def fail(request):
                raise httpx.ReadTimeout("request timed out", request=request)
            try:
                with model_transport("tools.docxindex.llm.client", fail):
                    with self.assertRaises(LLMClientError) as captured:
                        client.complete("contract excerpt")
            finally:
                reset_call_context(token)

            error = captured.exception
            self.assertIsNone(error.http_status)
            self.assertEqual(error.failure_stage, "request")
            record = json.loads(Path(error.call_path).read_text(encoding="utf-8"))
            self.assertEqual(record["criterion_id"], "H12")
            self.assertEqual(record["stage"], "route.1.scan")
            self.assertEqual(record["request"]["messages"][0]["content"], "contract excerpt")
            self.assertIsNone(record["error"]["http_status"])
            self.assertRegex(Path(error.call_path).name, rf"^docxindex_H12__route-1-scan__\d{{8}}T\d{{9}}Z__{error.call_id}\.json$")

    def test_received_usage_survives_slot_release_failure_without_resending(self) -> None:
        @asynccontextmanager
        async def failed_release(*args, **kwargs):
            yield SimpleNamespace(key="test:model:semaphore")
            raise RedisConnectionError("Redis unavailable while releasing this slot")

        async def execute():
            sends = []

            async def send(remaining):
                sends.append(remaining)
                return {"usage": {"prompt_tokens": 40, "completion_tokens": 5}}, 200

            with patch("endpoints.runtime.model_calls.model_semaphore", failed_release):
                try:
                    await execute_model(model_type="llm", model="fake", caller="docxindex.llm",
                                        request={"messages": []}, send=send)
                finally:
                    self.assertEqual(len(sends), 1)

        for detailed in (False, True):
            with self.subTest(detailed=detailed), tempfile.TemporaryDirectory() as directory, \
                 patch("loggers.call_log.LOGGING_DETAILED", detailed):
                with self.assertRaises(RedisConnectionError) as caught:
                    with logical_call("docxindex.llm", "fake", directory=Path(directory) / "calls") as logical:
                        asyncio.run(execute())
                self.assertEqual(caught.exception.failure_stage, "semaphore_release")
                record = logical.last_record
                self.assertEqual(record.payload["status"], "failed")
                self.assertEqual(record.payload["failure_stage"], "semaphore_release")
                self.assertEqual(record.payload["http_status"], 200)
                self.assertEqual((record.payload["input_tokens"], record.payload["output_tokens"]), (40, 5))
                self.assertEqual(logical.payload["retry_count"], 0)
                self.assertEqual((logical.payload["known_input_tokens"], logical.payload["known_output_tokens"]), (40, 5))
                self.assertTrue(logical.payload["usage_complete"])
                persisted = json.loads(logical.path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["status"], "failed")
                self.assertEqual(len(persisted["attempts"]), 1)
                self.assertEqual(persisted["attempts"][0]["failure_stage"], "semaphore_release")
                self.assertEqual(persisted["attempts"][0]["input_output_tokens"], 45)
                self.assertEqual(persisted["attempts"][0]["http_status"], 200)
                self.assertIsNone(persisted["attempts"][0]["error"]["http_status"])
                self.assertTrue(persisted["attempts"][0]["sent"])
                self.assertEqual(persisted["send_count"], 1)

    def test_normal_log_mode_persists_success_http_status_without_request_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("loggers.call_log.LOGGING_DETAILED", False):
            calls = Path(directory) / "calls"
            with logical_call("agent", "fake", directory=calls) as logical:
                record = new_call(component="agent", model="fake", request={"messages": [{"content": "contract"}]})
                record.sent()
                record.succeeded({"usage": {"prompt_tokens": 4, "completion_tokens": 1}}, http_status=200)
            persisted = json.loads(logical.path.read_text(encoding="utf-8"))
            self.assertEqual(list(calls.glob("*.json")), [])
        self.assertEqual(persisted["status"], "succeeded")
        self.assertEqual(persisted["send_count"], 1)
        self.assertEqual(persisted["attempts"][0]["http_status"], 200)
        self.assertTrue(persisted["attempts"][0]["sent"])
        self.assertNotIn("request", persisted["attempts"][0])

    def test_llm_input_usage_does_not_make_unknown_output_usage_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with logical_call("agent", "fake", directory=Path(directory) / "calls") as logical:
                record = new_call(component="agent", model="fake", request={})
                record.sent()
                record.succeeded({"usage": {"prompt_tokens": 40}}, http_status=200)
            persisted = json.loads(logical.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "succeeded")
        self.assertTrue(persisted["input_usage_complete"])
        self.assertFalse(persisted["output_usage_complete"])
        self.assertFalse(persisted["usage_complete"])
        self.assertEqual(persisted["known_input_tokens"], 40)
        self.assertIsNone(persisted["output_tokens"])
        self.assertIsNone(persisted["attempts"][0]["output_tokens"])

    def test_failure_preserves_local_input_partial_output_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("loggers.call_log.LOGGING_DETAILED", False):
            with logical_call("agent", "fake", directory=Path(directory) / "calls") as logical:
                record = new_call(component="agent", model="fake", request={})
                record.record_local_input(40, {"tokenizer": "cl100k_base", "estimated": True})
                record.sent()
                record.record_received_output(7)
                record.failed(TimeoutError("stream stalled"))
                logical.failed(TimeoutError("stream stalled"))
            persisted = json.loads(logical.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual((persisted["input_tokens"], persisted["output_tokens"]), (40, 7))
        self.assertFalse(persisted["usage_complete"])
        attempt = persisted["attempts"][0]
        self.assertEqual(attempt["output_completeness"], "partial")
        self.assertEqual(attempt["input_token_source"], "local")
        self.assertEqual(attempt["output_token_source"], "local")

    def test_all_sent_attempts_count_once_but_local_rejection_is_not_service_usage(self):
        with logical_call("agent", "fake") as logical:
            rejected = new_call(component="agent", model="fake", request={})
            rejected.record_local_input(100)
            rejected.failed(ValueError("input too long"), stage="input_validation")
            first = new_call(component="agent", model="fake", request={})
            first.record_local_input(40)
            first.sent()
            first.failed(TimeoutError("no output"))
            second = new_call(component="agent", model="fake", request={})
            second.record_local_input(41)
            second.sent()
            second.succeeded({"usage": {"prompt_tokens": 42, "completion_tokens": 5}})
        self.assertEqual((logical.payload["send_count"], logical.payload["retry_count"]), (2, 1))
        self.assertEqual(logical.payload["input_tokens"], 82)
        self.assertEqual(logical.payload["output_tokens"], 5)
        self.assertFalse(logical.payload["input_usage_complete"])
        self.assertFalse(logical.payload["output_count_complete"])
        self.assertFalse(logical.payload["attempts"][0]["sent"])

    def test_shared_summary_counts_failed_retries_input_only_and_deduplicates_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / "calls"
            with logical_call("agent", "fake", directory=logs, source="Planner") as first:
                record = new_call(component="agent", model="fake", request={})
                record.record_local_input(40)
                record.sent()
                record.failed(TimeoutError("unknown output"))
                first.failed(TimeoutError("unknown output"))
            with logical_call("docxindex.embedding", "fake", directory=logs) as second:
                record = new_call(component="docxindex.embedding", model="fake", request={})
                record.record_local_input(12)
                record.sent()
                record.succeeded({"data": []})
            self.assertEqual(second.payload["input_output_tokens"], 12)
            duplicate = first.path.with_name("duplicate.json")
            duplicate.write_text(first.path.read_text(encoding="utf-8"), encoding="utf-8")
            summary = summarize_model_attempts(first.path.parent)
        self.assertEqual(summary["sent_attempts"], 2)
        self.assertEqual(summary["planner"], 40)
        self.assertEqual(summary["retrieval"], 12)
        self.assertEqual((summary["input"], summary["output"], summary["total"]), (52, 0, 52))
        self.assertEqual(summary["unknown_output_attempts"], 1)
        self.assertEqual(summary["local_fallback_attempts"], 2)

    def test_unreadable_or_missing_logs_do_not_claim_complete_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = summarize_model_attempts(Path(directory) / "missing")
            (Path(directory) / "broken.json").write_text("{partial", encoding="utf-8")
            damaged = summarize_model_attempts(directory)
        for summary in (missing, damaged):
            self.assertFalse(summary["output_complete"])
            self.assertFalse(summary["stats_complete"])
            self.assertTrue(summary["total_is_lower_bound"])
        self.assertEqual(len(damaged["unreadable_records"]), 1)

    def test_nested_workers_keep_criterion_context_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = Path(temp_dir) / "logs" / "events.log"
            token = set_call_context(events_path, operation_id="op-2", criterion_id="H15", stage="route.1.scan")
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    records = map_with_context(executor, lambda item: new_call(
                        component="docxindex.llm", model="fake", request={"item": item}), [1, 2])
            finally:
                reset_call_context(token)
            self.assertEqual({record.payload["criterion_id"] for record in records}, {"H15"})
            self.assertEqual(len({record.call_id for record in records}), 2)
            self.assertTrue(all(record.path.is_file() for record in records))
            self.assertTrue(all(record.path.name.startswith("H15__route-1-scan__") for record in records))

    def test_task_level_agent_uses_role_in_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = new_call(component="agent", model="fake", request={"messages": []},
                              directory=Path(temp_dir), stage="agent_model_call", source="Summarizer")
            self.assertTrue(record.path.name.startswith("Summarizer__agent_model_call__"))
            self.assertEqual(record.payload["source"], "Summarizer")
            self.assertIsNone(record.payload["criterion_id"])

    def test_retrieval_error_returns_structured_docxindex_origin(self) -> None:
        retriever = object.__new__(DocxIndexRetriever)
        error = LLMClientError("request timed out", http_status=None, error_type="APITimeoutError")
        error.call_id = "call-123"
        error.call_path = "/tmp/calls/call-123.json"
        error.failure_stage = "request"
        with patch.object(retriever, "_search_sync", side_effect=error):
            result = asyncio.run(retriever.search("H12", criterion_id="H12", return_metadata=True))
        payload = json.loads(result)["error"]
        self.assertEqual(payload["origin"], "docxindex")
        self.assertEqual(payload["call_id"], "call-123")
        self.assertIsNone(payload["http_status"])

    def test_mcp_imports_inner_calls_under_tool_span(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logs = Path(temp_dir) / "logs"
            token = set_call_context(logs / "events.log", operation_id="op-3", criterion_id="H12")
            try:
                record = new_call(component="docxindex.llm", model="fake", request={"messages": []})
                record.succeeded({"content": "ok"})
            finally:
                reset_call_context(token)
            trace = TraceLogger(logs / "trace.json")
            trace_token = set_current_trace(trace)
            try:
                client = object.__new__(MinimalMCPClient)
                client._import_operation_records(str(logs / "events.log"), "op-3", "mcp-run")
            finally:
                reset_current_trace(trace_token)
            self.assertEqual(len(trace.runs), 1)
            self.assertEqual(trace.runs[0]["parent_id"], "mcp-run")
            self.assertEqual(trace.runs[0]["outputs"]["call_id"], record.call_id)

    def test_mcp_import_reads_legacy_uuid_only_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logs = Path(temp_dir) / "logs"
            calls = logs / "calls"
            calls.mkdir(parents=True)
            old = calls / ("a" * 32 + ".json")
            old.write_text(json.dumps({
                "call_id": "a" * 32, "operation_id": "old-op", "component": "docxindex.llm",
                "model": "fake", "status": "failed", "started_at": "2026-09-23T00:00:00+00:00",
                "error": {"type": "TimeoutError", "message": "timeout", "http_status": None},
            }), encoding="utf-8")
            trace = TraceLogger(logs / "trace.json")
            token = set_current_trace(trace)
            try:
                object.__new__(MinimalMCPClient)._import_operation_records(
                    str(logs / "events.log"), "old-op", "mcp-old")
            finally:
                reset_current_trace(token)
            self.assertEqual(trace.runs[0]["outputs"]["call_path"], str(old))
