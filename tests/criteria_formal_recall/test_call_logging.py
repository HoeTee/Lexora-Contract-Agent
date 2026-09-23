from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import BoundedSemaphore, Lock
from types import SimpleNamespace
from unittest.mock import patch

from loggers.call_log import map_with_context, new_call, reset_call_context, set_call_context
from loggers.trace_logger import TraceLogger, reset_current_trace, set_current_trace
from mcp_service.client.client import MinimalMCPClient
from tools.docxindex.llm.client import LLMClient, LLMClientError
from tools.docxindex.retriever import DocxIndexRetriever


class CallLoggingTests(unittest.TestCase):
    def test_docxindex_timeout_retains_input_without_inventing_http_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events_path = Path(temp_dir) / "logs" / "events.log"
            token = set_call_context(events_path, operation_id="op-1", criterion_id="H12", stage="route.1.scan")
            client = object.__new__(LLMClient)
            client.settings = SimpleNamespace(model="fake", temperature=0, enable_thinking=False)
            client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kwargs: (_ for _ in ()).throw(TimeoutError("request timed out")))))
            client._limiter = BoundedSemaphore(1)
            client._start_lock = Lock()
            client._next_start = 0.0
            client._start_interval_seconds = 0.0
            client._rate_limit_retries = 0
            try:
                with patch("tools.docxindex.llm.client._global_model_slot", return_value=nullcontext()):
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
