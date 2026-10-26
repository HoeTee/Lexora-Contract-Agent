"""Verify the human timeline follows actual admission, cleanup and validation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from endpoints.runtime.model_budget import ModelCallBudget
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.model_limits import ModelSemaphoreLease
from loggers.call_log import logical_call, reset_call_context, set_call_context


class ModelEventTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = Path(self.temp.name) / "logs"
        token = set_call_context(self.logs / "events.log", criterion_id="H04", criterion_attempt=2)
        self.addCleanup(reset_call_context, token)
        self.leases = []

        async def acquire(**kwargs):
            lease = ModelSemaphoreLease("llm", "test:model", str(len(self.leases)), 0, 1)
            self.leases.append(lease)
            return lease

        async def release(lease):
            lease.acquired = False

        async def renew(lease):
            await asyncio.sleep(60)

        self.semaphore = SimpleNamespace(acquire_async=AsyncMock(side_effect=acquire),
                                         release_async=AsyncMock(side_effect=release), keep_alive=renew)
        changes = [
            patch.dict("endpoints.runtime.model_limits._SEMAPHORES", {kind: self.semaphore for kind in ("llm", "embedding", "reranker")}),
            patch("endpoints.runtime.model_calls.count_model_input", return_value=SimpleNamespace(
                input_tokens=9, input_token_counts=[9], metadata={"source": "test"})),
            patch("loggers.call_log.LOGGING_DETAILED", False),
            patch("endpoints.runtime.model_calls._retry_delay", return_value=0),
        ]
        for change in changes:
            change.start()
            self.addCleanup(change.stop)

    def events(self):
        return (self.logs / "events.log").read_text(encoding="utf-8")

    async def test_received_response_can_fail_validation_then_retry_under_same_number(self):
        responses = iter([{"answer": "bad"}, {"answer": "ok"}])

        async def send(remaining):
            return {**next(responses), "usage": {"prompt_tokens": 9, "completion_tokens": 2}}, 200

        def validate(response):
            if response["answer"] != "ok":
                raise ValueError("schema rejected")
            return response

        with logical_call("agent", "fake", source="SubAgent_H04", stage="review-initial") as logical:
            await execute_model(model_type="llm", model="fake", caller="agent", request={},
                                send=send, validate=validate, budget=ModelCallBudget(max_retries=1))
        events = self.events()
        self.assertLess(events.index("Response 1 received"), events.index("failed output validation"))
        self.assertLess(events.index("failed output validation"), events.index("Request 2 sent"))
        self.assertEqual(events.count("Model slot released."), 2)
        self.assertEqual(events.count("Validation passed."), 1)
        self.assertTrue(all(line.startswith("1 ") for line in events.splitlines()))
        self.assertTrue(all("agent H04/SubAgent_H04/review-initial" in line for line in events.splitlines()))
        record = json.loads(logical.path.read_text(encoding="utf-8"))
        self.assertEqual([item["logical_no"] for item in record["attempts"]], [1, 1])
        self.assertEqual(record["known_input_tokens"], 18)
        self.assertEqual(record["criterion_attempt"], 2)
        self.assertFalse((self.logs / "calls").exists())

    async def test_timeout_releases_before_retry_and_preserves_failed_input(self):
        sends = 0

        async def send(remaining):
            nonlocal sends
            sends += 1
            if sends == 1:
                await asyncio.sleep(1)
            return {"usage": {"prompt_tokens": 9, "completion_tokens": 2}}, 200

        with logical_call("agent", "fake") as logical:
            await execute_model(model_type="llm", model="fake", caller="agent", request={}, send=send,
                                budget=ModelCallBudget(timeout_seconds=0.02, max_retries=1))
        events = self.events()
        self.assertIn("Request 1 timed out", events)
        self.assertIn("Output: unknown", events)
        self.assertLess(events.index("Model slot released."), events.index("Retry scheduled"))
        self.assertTrue(all(not lease.acquired for lease in self.leases))
        self.assertEqual(logical.payload["known_input_tokens"], 18)
        self.assertFalse(logical.payload["usage_complete"])

    async def test_cancellation_and_logging_failure_cannot_skip_release(self):
        ready = asyncio.Event()

        async def send(remaining):
            ready.set()
            await asyncio.sleep(60)

        with self.assertRaises(asyncio.CancelledError), logical_call("agent", "fake"):
            operation = asyncio.create_task(execute_model(model_type="llm", model="fake", caller="agent",
                request={}, send=send, budget=ModelCallBudget(max_retries=0)))
            await ready.wait()
            def fail_cleanup_log(*args, **kwargs):
                raise OSError("Log disk unavailable after request started")
            self.leases[0].release_event = fail_cleanup_log
            with self.assertLogs("endpoints.runtime.model_limits", level="ERROR"):
                operation.cancel()
                await operation
        self.assertTrue(all(not lease.acquired for lease in self.leases))
        self.assertEqual(self.semaphore.release_async.await_count, 1)

    async def test_basic_events_cover_all_three_model_types(self):
        async def send(remaining):
            return {"usage": {"prompt_tokens": 9}}, 200

        for kind in ("llm", "embedding", "reranker"):
            with logical_call(f"docxindex.{kind}", "fake"):
                await execute_model(model_type=kind, model="fake", caller=f"docxindex.{kind}",
                                    request={}, send=send, budget=ModelCallBudget(max_retries=0))
        events = self.events()
        self.assertEqual(events.count("Model slot released."), 3)
        self.assertIn("docxindex.indexing.llm", events)
        self.assertIn("docxindex.indexing.embedding", events)
        self.assertIn("docxindex.retrieval.reranker", events)
        responses = [line for line in events.splitlines() if "Response 1 received" in line]
        self.assertEqual(sum("Output: not applicable" in line for line in responses), 2)


if __name__ == "__main__":
    unittest.main()
