"""Admission and unlimited-budget regression tests; Redis cases use UUID keys only."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import math
import multiprocessing
import os
import queue
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import redis

from endpoints.runtime import model_limits
from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout, TaskDeadlineExceeded, task_execution
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.errors import ModelInputTooLong
from endpoints.runtime.model_limits import ModelSemaphoreConfig, RedisModelSemaphore
from loggers.call_log import logical_call


def _shared_worker(redis_url, key, output, barrier):
    async def run():
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(redis_url, key, 2, 3, 0.01, 5))
        client = semaphore._async_client()
        try:
            barrier.wait(timeout=10)
            async def one(index):
                lease = await semaphore.acquire_async(caller_name=str(index), model="test",
                    budget=ModelCallBudget(timeout_seconds=None, wait_timeout_seconds=None))
                lease.sent = True
                output.put(("admitted", lease.member))
                await asyncio.sleep(0.05)
                await semaphore.release_async(lease)
            await asyncio.gather(*(one(index) for index in range(3)))
            output.put(("done", os.getpid()))
        finally:
            await client.aclose()
    try:
        asyncio.run(run())
    except BaseException as exc:
        output.put(("error", repr(exc)))


class UnlimitedBudgetTests(unittest.TestCase):
    def test_expired_request_timer_uses_task_source_even_before_rounded_deadline(self):
        with patch("endpoints.runtime.model_budget.time.monotonic", return_value=100.249):
            budget = ModelCallBudget(timeout_seconds=1, started=100)
            budget._task_deadline = 100.25
            budget.deadline = 101
            self.assertIsInstance(budget.timeout_error("request", request_timer_expired=True), TaskDeadlineExceeded)
            # A plain network timeout did not expire our timer, so it cannot
            # claim that the still-future contract deadline was reached.
            self.assertNotIsInstance(budget.timeout_error("request"), TaskDeadlineExceeded)

    def test_expired_request_timer_preserves_earlier_model_source(self):
        with patch("endpoints.runtime.model_budget.time.monotonic", return_value=100.099):
            budget = ModelCallBudget(timeout_seconds=0.1, started=100)
            budget._task_deadline = 100.25
            budget.deadline = 100.1
            error = budget.timeout_error("request", request_timer_expired=True)
            self.assertIsInstance(error, ModelCallTimeout)
            self.assertNotIsInstance(error, TaskDeadlineExceeded)

    def test_completed_request_trailer_uses_task_source_even_if_model_deadline_was_earlier(self):
        with patch("endpoints.runtime.model_budget.time.monotonic", return_value=100.249):
            budget = ModelCallBudget(timeout_seconds=0.1, started=100)
            budget._task_deadline = 100.25
            budget.deadline = 100.1
            budget._request_finished = 100.05
            self.assertIsInstance(budget.timeout_error("request", request_timer_expired=True), TaskDeadlineExceeded)

    def test_none_disables_request_wait_and_retry_caps(self):
        with task_execution(None):
            budget = ModelCallBudget(timeout_seconds=None, wait_timeout_seconds=None, max_retries=None)
            self.assertTrue(math.isinf(budget.waiting_remaining()))
            for _ in range(20):
                budget.reserve_send()
                self.assertTrue(math.isinf(budget.remaining()))
                budget.received_content()
                budget.finish_request()
                self.assertTrue(budget.can_retry)
            self.assertEqual(budget.sends, 20)

    def test_unlimited_request_is_still_capped_by_contract(self):
        with task_execution(time.time() + 10):
            budget = ModelCallBudget(timeout_seconds=None, wait_timeout_seconds=None)
            budget.reserve_send()
            self.assertLessEqual(budget.remaining(), 10)
            self.assertLessEqual(budget.waiting_remaining(), 10)

    def test_no_deadline_context_clears_environment_and_restores_it(self):
        with patch.dict(os.environ, {"CONTRACT_TASK_DEADLINE": "123", "CONTRACT_TASK_ID": "prior"}):
            with task_execution(None, "current"):
                self.assertNotIn("CONTRACT_TASK_DEADLINE", os.environ)
                self.assertEqual(os.environ["CONTRACT_TASK_ID"], "current")
            self.assertEqual(os.environ["CONTRACT_TASK_DEADLINE"], "123")
            self.assertEqual(os.environ["CONTRACT_TASK_ID"], "prior")


class InputAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_error_is_logged_before_admission_and_never_spends_retry(self):
        budget = ModelCallBudget(max_retries=None)
        counted = SimpleNamespace(input_tokens=11, input_token_counts=[11], metadata={"tokenizer_id": "test"})
        with patch("endpoints.runtime.model_calls.count_model_input", return_value=counted), \
                patch("endpoints.runtime.model_calls.model_semaphore") as admission:
            with logical_call("test.input", "test") as logical:
                with self.assertRaises(ModelInputTooLong) as caught:
                    await execute_model(model_type="llm", model="test", caller="test.input", request={},
                        send=None, budget=budget, input_limit=10)
                self.assertEqual(caught.exception.failure_stage, "input_validation")
                self.assertEqual(logical.payload["attempts"][0]["local_input_tokens"], 11)
                self.assertFalse(logical.payload["attempts"][0]["sent"])
        self.assertEqual(budget.sends, 0)
        admission.assert_not_called()

    async def test_batch_sum_does_not_replace_per_item_limit(self):
        @asynccontextmanager
        async def admitted(*args, **kwargs):
            yield SimpleNamespace(key="test")
        counted = SimpleNamespace(input_tokens=18, input_token_counts=[9, 9], metadata={})
        async def send(_remaining):
            return {"data": []}, 200
        with patch("endpoints.runtime.model_calls.count_model_input", return_value=counted), \
                patch("endpoints.runtime.model_calls.model_semaphore", admitted):
            actual = await execute_model(model_type="embedding", model="test", caller="test.input", request={},
                send=send, budget=ModelCallBudget(), input_limit=10)
        self.assertEqual(actual, {"data": []})

    async def test_explicit_none_limit_disables_global_cap(self):
        @asynccontextmanager
        async def admitted(*args, **kwargs):
            yield SimpleNamespace(key="test")
        counted = SimpleNamespace(input_tokens=50000, input_token_counts=[50000], metadata={})
        async def send(_remaining):
            return {}, 200
        with patch("endpoints.runtime.model_calls.count_model_input", return_value=counted), \
                patch.dict("endpoints.runtime.model_calls.MODEL_INPUT_LIMITS", {"llm": 1}), \
                patch("endpoints.runtime.model_calls.model_semaphore", admitted):
            await execute_model(model_type="llm", model="test", caller="test.input", request={},
                send=send, budget=ModelCallBudget(), input_limit=None)


@unittest.skipUnless(os.getenv("TEST_MODEL_REDIS_URL"), "Set TEST_MODEL_REDIS_URL to isolated Redis")
class ModelLimitsRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis_url = os.environ["TEST_MODEL_REDIS_URL"]
        self.key = "model-limits-test:" + uuid.uuid4().hex
        self.redis = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self.redis.ping()
        self.semaphores = []

    def semaphore(self, capacity=2, rpm=2, lease=3):
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(
            self.redis_url, self.key, capacity, lease, 0.005, rpm))
        self.semaphores.append(semaphore)
        return semaphore

    def server_ms(self):
        seconds, micros = self.redis.time()
        return seconds * 1000 + micros // 1000

    async def asyncTearDown(self):
        for semaphore in self.semaphores:
            await semaphore._async_client().aclose()
        keys = list(self.redis.scan_iter(self.key + "*"))
        if keys:
            self.redis.delete(*keys)
        self.redis.close()

    async def acquire(self, semaphore, name="test", wait=None):
        return await semaphore.acquire_async(caller_name=name, model="test",
            budget=ModelCallBudget(timeout_seconds=0.01, wait_timeout_seconds=wait))

    async def wait_until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_rpm_blocks_without_holding_slot_and_uses_sliding_boundary(self):
        semaphore = self.semaphore(rpm=1)
        first = await self.acquire(semaphore)
        first.sent = True
        await semaphore.release_async(first)
        waiting = asyncio.create_task(self.acquire(semaphore, "second"))
        await self.wait_until(lambda: self.redis.zcard(self.key + ":waiting") == 1)
        self.assertEqual(self.redis.zcard(self.key), 0)
        self.assertFalse(waiting.done())
        # Age an already-sent event to just inside the window. It must continue
        # blocking until its own timestamp expires, independent of minute marks.
        self.redis.zadd(self.key + ":rpm", {first.member: self.server_ms() - 59850})
        await asyncio.sleep(0.04)
        self.assertFalse(waiting.done())
        second = await asyncio.wait_for(waiting, 1)
        self.assertGreater(second.rpm_wait_seconds, 0.05)
        self.assertEqual(second.window_count, 1)
        await semaphore.release_async(second)
        self.assertEqual(self.redis.zcard(self.key + ":rpm"), 0)

    async def test_unused_admission_is_refunded_but_sent_failure_is_retained(self):
        semaphore = self.semaphore(rpm=2)
        unused = await self.acquire(semaphore)
        await semaphore.release_async(unused)
        self.assertEqual(self.redis.zcard(self.key + ":rpm"), 0)
        async def fail(_remaining):
            raise RuntimeError("HTTP invocation failed")
        with patch.dict(model_limits._SEMAPHORES, {"llm": semaphore}):
            with self.assertRaisesRegex(RuntimeError, "HTTP invocation failed"):
                await execute_model(model_type="llm", model="test", caller="test", request={},
                    send=fail, budget=ModelCallBudget(timeout_seconds=None, max_retries=0))
        self.assertEqual(self.redis.zcard(self.key), 0)
        self.assertEqual(self.redis.zcard(self.key + ":rpm"), 1)

    async def test_cancelled_rpm_waiter_leaves_no_slot_or_waiter(self):
        semaphore = self.semaphore(rpm=1)
        owner = await self.acquire(semaphore)
        owner.sent = True
        await semaphore.release_async(owner)
        pending = asyncio.create_task(self.acquire(semaphore))
        await self.wait_until(lambda: self.redis.zcard(self.key + ":waiting") == 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        for suffix in ("", ":waiting", ":deadlines"):
            self.assertEqual(self.redis.zcard(self.key + suffix), 0)
        self.assertEqual(self.redis.zcard(self.key + ":rpm"), 1)

    async def test_unlimited_waiter_heartbeat_and_expired_fifo_head(self):
        semaphore = self.semaphore(capacity=1, rpm=None)
        first = await self.acquire(semaphore)
        self.redis.zadd(self.key + ":waiting", {"crashed": 0})
        self.redis.zadd(self.key + ":deadlines", {"crashed": self.server_ms() + 80})
        following = asyncio.create_task(self.acquire(semaphore, "following"))
        await self.wait_until(lambda: self.redis.zcard(self.key + ":waiting") == 2)
        live = [member for member in self.redis.zrange(self.key + ":waiting", 0, -1) if member != "crashed"][0]
        original_deadline = self.redis.zscore(self.key + ":deadlines", live)
        await asyncio.sleep(0.12)
        self.assertIsNone(self.redis.zscore(self.key + ":waiting", "crashed"))
        self.assertGreater(self.redis.zscore(self.key + ":deadlines", live), original_deadline)
        await semaphore.release_async(first)
        next_lease = await asyncio.wait_for(following, 1)
        await semaphore.release_async(next_lease)

    async def test_nullable_capacity_and_rpm_allow_parallel_admission(self):
        semaphore = self.semaphore(capacity=None, rpm=None)
        leases = await asyncio.gather(*(self.acquire(semaphore, str(index)) for index in range(10)))
        self.assertEqual(self.redis.zcard(self.key), 10)
        await asyncio.gather(*(semaphore.release_async(lease) for lease in leases))
        self.assertEqual(self.redis.zcard(self.key), 0)

    async def test_three_processes_share_both_limits(self):
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        barrier = context.Barrier(3)
        processes = [context.Process(target=_shared_worker, args=(self.redis_url, self.key, output, barrier))
                     for _ in range(3)]
        events = []
        try:
            for process in processes:
                process.start()
            async with asyncio.timeout(20):
                while len(events) < 5:
                    try:
                        event = output.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                        continue
                    self.assertNotEqual(event[0], "error", event)
                    if event[0] == "admitted":
                        events.append(event[1])
                    self.assertLessEqual(self.redis.zcard(self.key), 2)
            await asyncio.sleep(0.15)
            self.assertEqual(self.redis.zcard(self.key + ":rpm"), 5)
            self.assertEqual(self.redis.zcard(self.key), 0)
            self.assertEqual(self.redis.zcard(self.key + ":waiting"), 4)
            now = self.server_ms()
            self.redis.zadd(self.key + ":rpm", {member: now - 59950 for member in events})
            done = 0
            async with asyncio.timeout(10):
                while done < 3:
                    try:
                        event = output.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                        continue
                    self.assertNotEqual(event[0], "error", event)
                    if event[0] == "admitted":
                        events.append(event[1])
                    if event[0] == "done":
                        done += 1
                    self.assertLessEqual(self.redis.zcard(self.key), 2)
                    self.assertLessEqual(self.redis.zcard(self.key + ":rpm"), 5)
            self.assertEqual(len(events), 9)
            self.assertEqual(self.redis.zcard(self.key), 0)
            self.assertEqual(self.redis.zcard(self.key + ":waiting"), 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                await asyncio.to_thread(process.join, 3)
            output.close()


if __name__ == "__main__":
    unittest.main()
