"""Summary fallbacks retain shared send limits and independent request timers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest import TestCase
from unittest.mock import patch

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from endpoints.runtime.model_budget import ModelCallBudget
from loggers.call_log import current_logical_call, logical_call, reset_call_context, set_call_context
from tests.criteria_formal_recall._model_test_support import model_transport
from tools.docxindex.indexing.llm_summary import (
    SUMMARY_OUTPUT_MAX_TOKENS,
    _generate_summary,
    _SummaryTask,
    _summarize_batch,
)
from tools.docxindex.indexing.token_budget import estimate_tokens
from tools.docxindex.llm.cache import JsonlCache
from tools.docxindex.llm.client import LLMClient, LLMClientError, LLMResponseValidationError, LLMSettings
from tools.docxindex.schema import DocumentNode


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "test", "object": "chat.completion", "created": 0, "model": "fake",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    })


class SummaryControlTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.logs = Path(self.temporary.name) / "logs"
        token = set_call_context(self.logs / "events.log", stage="index-summary")
        self.addCleanup(reset_call_context, token)
        self.timeout = 2
        self.retries = 1
        budget_patch = patch("loggers.call_log.ModelCallBudget", side_effect=lambda: ModelCallBudget(
            timeout_seconds=self.timeout, max_retries=self.retries))
        budget_patch.start()
        self.addCleanup(budget_patch.stop)
        self.client = LLMClient(LLMSettings(model="fake", api_key="fake", base_url="https://model.test/v1"))
        self.sends = []
        self.tasks = [self.task(number) for number in (1, 2)]
        self.long_summary = "合同导航摘要 " * 300
        self.assertGreater(estimate_tokens(self.long_summary), SUMMARY_OUTPUT_MAX_TOKENS)

    @staticmethod
    def task(number: int) -> _SummaryTask:
        node = DocumentNode(node_id=f"body/{number}", node_type="section", title=f"条款{number}",
                            text="合同内容", start_anchor=f"p{number}", end_anchor=f"p{number}")
        return _SummaryTask(node=node, content=node.text, cache_key=str(number))

    def records(self):
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.logs / "logical_calls").glob("*.json")]

    def batch(self):
        return _summarize_batch(self.tasks, self.client, JsonlCache(None))

    def test_batch_http_retry_exhaustion_does_not_start_individual_summaries(self):
        def busy(request):
            self.sends.append(json.loads(request.content))
            return httpx.Response(429, json={"error": {"message": "busy"}})

        with model_transport("tools.docxindex.llm.client", busy):
            with self.assertRaises(LLMClientError) as caught:
                self.batch()
        self.assertEqual(caught.exception.failure_stage, "request")
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual([task.node.summary for task in self.tasks], ["", ""])

    def test_exhausted_request_timeouts_do_not_start_individual_summaries(self):
        self.timeout = 0.15
        cancelled = []

        async def slow(request):
            self.sends.append(json.loads(request.content))
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        with model_transport("tools.docxindex.llm.client", slow):
            with self.assertRaises(LLMClientError) as caught:
                self.batch()
        self.assertEqual(caught.exception.error_type, "ModelCallTimeout")
        self.assertEqual(len(self.sends), 2, str(caught.exception))
        self.assertEqual(cancelled, [True, True])
        self.assertEqual(len(self.records()), 1)

    def test_batch_redis_failure_does_not_start_individual_summaries(self):
        with model_transport("tools.docxindex.llm.client", lambda request: self.fail("HTTP must not be sent")):
            with patch("endpoints.runtime.model_calls.model_semaphore", side_effect=RedisConnectionError("unavailable")):
                with self.assertRaises(LLMClientError) as caught:
                    self.batch()
        self.assertEqual(caught.exception.failure_stage, "semaphore_wait")
        self.assertEqual(self.records()[0].get("send_count", 0), 0)
        self.assertEqual(len(self.records()), 1)

    def test_batch_exhausted_send_allowance_does_not_start_another_operation(self):
        with model_transport("tools.docxindex.llm.client", lambda request: self.fail("HTTP must not be sent")):
            with logical_call("docxindex.llm", "fake") as logical:
                logical.budget.max_retries = 0
                logical.budget.sends = 1
                with self.assertRaises(LLMClientError) as caught:
                    self.batch()
        self.assertEqual(caught.exception.error_type, "ModelRetryLimitError")
        self.assertEqual(len(self.records()), 1)

    def test_batch_invalid_output_does_not_restart_as_individual_calls(self):
        def invalid(request):
            self.sends.append(json.loads(request.content))
            return completion("invalid JSON")

        with model_transport("tools.docxindex.llm.client", invalid):
            with self.assertRaises(LLMResponseValidationError):
                self.batch()
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(len(self.records()), 1)

    def test_valid_batch_missing_one_node_starts_only_that_nodes_summary(self):
        def success(request):
            self.sends.append(json.loads(request.content))
            content = ({"summaries": [{"node_id": "body/1", "summary": "第一项摘要"}]}
                       if len(self.sends) == 1 else {"summary": "第二项摘要"})
            return completion(json.dumps(content, ensure_ascii=False))

        with model_transport("tools.docxindex.llm.client", success):
            fallback_nodes = self.batch()
        self.assertEqual(fallback_nodes, 1)
        self.assertEqual([task.node.summary for task in self.tasks], ["第一项摘要", "第二项摘要"])
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual([record["send_count"] for record in self.records()], [1, 1])

    def test_long_summary_repair_uses_one_operation_and_shared_allowance(self):
        def success(request):
            self.sends.append(json.loads(request.content))
            summary = self.long_summary if len(self.sends) == 1 else "压缩后的摘要"
            return completion(json.dumps({"summary": summary}, ensure_ascii=False))

        with model_transport("tools.docxindex.llm.client", success):
            result = _generate_summary(self.client, self.tasks[0].node, "合同内容")
        self.assertEqual(result, {"summary": "压缩后的摘要"})
        self.assertEqual(len(self.sends), 2)
        self.assertIn("Summary exceeds 200 tokens", self.sends[1]["messages"][0]["content"])
        record, = self.records()
        self.assertEqual((record["send_count"], record["retry_count"], record["status"]), (2, 1, "succeeded"))

    def test_overlong_candidate_is_truncated_only_after_shared_output_allowance(self):
        for retries in (0, 1, 3):
            with self.subTest(max_retries=retries):
                self.retries = retries
                sends = []

                def overlong(request, *, sends=sends):
                    sends.append(json.loads(request.content))
                    return completion(json.dumps({"summary": self.long_summary}, ensure_ascii=False))

                with model_transport("tools.docxindex.llm.client", overlong):
                    result = _generate_summary(self.client, self.tasks[0].node, "合同内容")
                self.assertEqual(len(sends), retries + 1)
                self.assertTrue(result["summary"])
                self.assertLessEqual(estimate_tokens(result["summary"]), SUMMARY_OUTPUT_MAX_TOKENS)
                self.assertTrue(self.long_summary.startswith(result["summary"]))
        self.assertEqual(len(self.records()), 3)
        self.assertEqual(sorted(record["send_count"] for record in self.records()), [1, 2, 4])

    def test_deadline_after_overlong_response_propagates_without_truncation(self):
        self.timeout = 0.35
        cancelled = []

        async def slow(request):
            self.sends.append(json.loads(request.content))
            if len(self.sends) == 1:
                await asyncio.sleep(0.1)
                return completion(json.dumps({"summary": self.long_summary}, ensure_ascii=False))
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        started = time.monotonic()
        with model_transport("tools.docxindex.llm.client", slow):
            with self.assertRaises(LLMClientError) as caught:
                _generate_summary(self.client, self.tasks[0].node, "合同内容")
        self.assertEqual(caught.exception.error_type, "ModelCallTimeout")
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(cancelled, [True])
        self.assertLess(time.monotonic() - started, 0.8)
        record, = self.records()
        self.assertEqual(record["status"], "failed")

    def test_synchronous_length_validation_cannot_mask_expired_task_deadline(self):
        def overlong(request):
            self.sends.append(json.loads(request.content))
            return completion(json.dumps({"summary": self.long_summary}, ensure_ascii=False))

        def length_check_past_deadline(text):
            # Simulate synchronous validation crossing the contract deadline without
            # sleeping or changing the event loop's global monotonic clock.
            logical = current_logical_call()
            if logical is not None:
                self.assertEqual(logical.sent_count, 1)
                logical.budget._task_deadline = time.monotonic() - 1
            return estimate_tokens(text)

        with model_transport("tools.docxindex.llm.client", overlong):
            with patch("tools.docxindex.indexing.llm_summary.estimate_tokens", length_check_past_deadline):
                with self.assertRaises(LLMClientError) as caught:
                    _generate_summary(self.client, self.tasks[0].node, "合同内容")
        self.assertEqual(caught.exception.error_type, "TaskDeadlineExceeded")
        self.assertEqual(caught.exception.failure_stage, "task_deadline")
        self.assertEqual(len(self.sends), 1)
        record, = self.records()
        self.assertEqual((record["status"], record["send_count"]), ("failed", 1))
        self.assertEqual(record["attempts"][0]["error"]["type"], "TaskDeadlineExceeded")

    def test_invalid_or_blank_output_without_candidate_still_fails(self):
        for content in ("invalid JSON", '{"summary":"   "}'):
            with self.subTest(content=content):
                with model_transport("tools.docxindex.llm.client", lambda request, content=content: completion(content)):
                    with self.assertRaises(LLMResponseValidationError):
                        _generate_summary(self.client, self.tasks[0].node, "合同内容")
