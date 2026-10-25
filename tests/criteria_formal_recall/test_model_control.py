"""Real Redis/HTTP verification; set TEST_MODEL_REDIS_URL for integration cases.

Use an isolated Redis database. Tests only remove UUID-prefixed keys they create.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import redis

from endpoints.runtime import llm_semaphore, model_budget
from endpoints.runtime.llm_semaphore import ModelSemaphoreConfig, ModelSemaphoreLease, RedisModelSemaphore
from endpoints.runtime.model_budget import (
    ModelCallBudget,
    ModelCallTimeout,
    ModelRetryLimitError,
    ModelSemaphoreTimeout,
    TaskDeadlineExceeded,
    task_execution,
)
from endpoints.runtime.model_calls import execute_model
from loggers.call_log import logical_call


def _worker_requests(redis_url, prefix, http_url, result_queue, *, request_count=8,
                     timeout_seconds=15, wait_timeout_seconds=180,
                     model_limits=(("llm", 5), ("embedding", 3), ("reranker", 10)), request_path=None,
                     start_barrier=None, report_timings=False):
    async def run():
        semaphores = {kind: RedisModelSemaphore(kind, ModelSemaphoreConfig(
            redis_url, f"{prefix}:{kind}", limit, 30, 0.005))
            for kind, limit in model_limits}
        try:
            with patch.dict(llm_semaphore._SEMAPHORES, semaphores):
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    async def one(kind):
                        async def send(remaining):
                            response = await client.post(f"{http_url}/{request_path or kind}", json={}, timeout=remaining)
                            response.raise_for_status()
                            return response.json(), response.status_code
                        with logical_call(f"test.{kind}", "local-test"):
                            budget = ModelCallBudget(timeout_seconds=timeout_seconds,
                                wait_timeout_seconds=wait_timeout_seconds, max_retries=0)
                            await execute_model(model_type=kind, model="local-test", caller=f"test.{kind}",
                                request={}, send=send, budget=budget)
                            return {"wait_seconds": budget.wait_seconds, "sends": budget.sends}
                    if start_barrier is not None:
                        start_barrier.wait(timeout=10)
                    results = await asyncio.gather(*(one(kind) for kind in semaphores for _ in range(request_count)))
                    return results if report_timings else None
        finally:
            for semaphore in semaphores.values():
                client = semaphore._clients.pop(asyncio.get_running_loop(), None)
                if client:
                    await client.aclose()
    try:
        result_queue.put(asyncio.run(run()))
    except BaseException as exc:
        result_queue.put(repr(exc))


def _worker_holds_slot(redis_url, key, lease_seconds, result_queue):
    async def run():
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(redis_url, key, 1, lease_seconds, 0.005))
        lease = await semaphore.acquire_async(caller_name="killed-owner", model="test", budget=ModelCallBudget())
        result_queue.put(lease.member)
        await asyncio.Event().wait()
    asyncio.run(run())


class _HTTPFixture:
    def __init__(self):
        self.active = {}
        self.peaks = {}
        self.counts = {}
        self.lock = threading.Lock()
        self.disconnected = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                kind = self.path.strip("/")
                with fixture.lock:
                    fixture.counts[kind] = fixture.counts.get(kind, 0) + 1
                    attempt = fixture.counts[kind]
                    fixture.active[kind] = fixture.active.get(kind, 0) + 1
                    fixture.peaks[kind] = max(fixture.peaks.get(kind, 0), fixture.active[kind])
                active = True
                try:
                    if kind == "trickle" or kind == "timeout-once" and attempt == 1:
                        self.send_response(200)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        for _ in range(1000):
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.02)
                    else:
                        time.sleep(0.8 if kind == "slow-model" else 0.1)
                        status = 429 if kind == "retry" and attempt == 1 else 200
                        body = json.dumps({"ok": True, "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
                        # End server work before the client can receive the
                        # response and acquire the next slot on another thread.
                        with fixture.lock:
                            fixture.active[kind] -= 1
                        active = False
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Retry-After", "0")
                        self.end_headers()
                        self.wfile.write(body)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    fixture.disconnected.set()
                finally:
                    if active:
                        with fixture.lock:
                            fixture.active[kind] -= 1
                    self.close_connection = True

            def log_message(self, *_):
                pass

        class Server(ThreadingHTTPServer):
            request_queue_size = 128
            daemon_threads = True
        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        clock = SimpleNamespace(monotonic=lambda: self.clock, time=lambda: self.clock + 900)
        clock_patch = patch.object(model_budget, "time", clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def test_waiting_accumulates_across_attempts_without_starting_request_timer(self):
        budget = ModelCallBudget(timeout_seconds=60, wait_timeout_seconds=180, started=self.clock)
        self.clock += 10
        with budget.waiting():
            self.clock += 120
            self.assertAlmostEqual(budget.remaining(), 60)
            self.assertAlmostEqual(budget.waiting_remaining(), 60)
        budget.reserve_send()
        self.clock += 20
        budget.finish_request()
        with budget.waiting():
            self.clock += 40
            self.assertAlmostEqual(budget.waiting_remaining(), 20)
        self.assertAlmostEqual(budget.wait_seconds, 160)
        self.assertAlmostEqual(budget.model_elapsed_seconds, 20)
        self.assertAlmostEqual(budget.remaining(), 40)

    def test_wait_budget_expires_without_consuming_model_or_send_allowance(self):
        budget = ModelCallBudget(timeout_seconds=60, wait_timeout_seconds=180, started=self.clock)
        with budget.waiting():
            self.clock += 100
        with self.assertRaises(ModelSemaphoreTimeout):
            with budget.waiting():
                self.clock += 81
                budget.waiting_remaining()
        self.assertEqual(budget.sends, 0)
        self.assertAlmostEqual(budget.model_remaining_seconds, 60)

    def test_each_send_gets_full_timeout_but_reuses_shared_send_allowance(self):
        budget = ModelCallBudget(timeout_seconds=60, wait_timeout_seconds=180, max_retries=1, started=self.clock)
        budget.reserve_send()
        self.clock += 38
        self.assertAlmostEqual(budget.remaining(), 22)
        budget.finish_request()
        self.clock += 75  # Validation and backoff do not consume a future HTTP request's time.
        with budget.waiting():
            self.clock += 100
        budget.reserve_send()
        self.assertEqual(budget.sends, 2)
        self.assertAlmostEqual(budget.remaining(), 60)
        self.assertFalse(budget.can_retry)
        self.clock += 61
        with self.assertRaises(ModelCallTimeout):
            budget.remaining()
        budget.finish_request()
        self.assertAlmostEqual(budget.remaining_seconds, 0)
        self.assertAlmostEqual(budget.model_elapsed_seconds, 99)
        with self.assertRaises(ModelRetryLimitError):
            budget.reserve_send()

    def test_expired_request_does_not_disable_remaining_retries(self):
        budget = ModelCallBudget(timeout_seconds=60, max_retries=3, started=self.clock)
        for send_number in range(1, 5):
            budget.reserve_send()
            self.assertEqual(budget.sends, send_number)
            self.assertAlmostEqual(budget.remaining(), 60)
            self.clock += 61
            with self.assertRaises(ModelCallTimeout):
                budget.remaining()
            budget.finish_request()
            self.assertEqual(budget.can_retry, send_number < 4)
        with self.assertRaises(ModelRetryLimitError):
            budget.reserve_send()

    def test_task_deadline_caps_waiting_without_extending_it_when_wait_ends(self):
        with task_execution(self.clock + 900 + 10):
            budget = ModelCallBudget(timeout_seconds=60, wait_timeout_seconds=180, started=self.clock)
            with budget.waiting():
                self.clock += 5
                self.assertAlmostEqual(budget.remaining(), 5)
                self.assertAlmostEqual(budget.waiting_remaining(), 5)
            self.clock += 6
            with self.assertRaises(TaskDeadlineExceeded):
                budget.waiting_remaining()
            with self.assertRaises(TaskDeadlineExceeded):
                budget.remaining()

    def test_task_deadline_caps_new_call_and_restores_environment(self):
        before = os.getenv("CONTRACT_TASK_DEADLINE")
        with task_execution(self.clock + 900 + 0.1, task_id="budget-test"):
            budget = ModelCallBudget(timeout_seconds=60, started=self.clock)
            self.assertAlmostEqual(budget.remaining(), 0.1)
            self.assertEqual(os.getenv("CONTRACT_TASK_ID"), "budget-test")
        self.assertEqual(os.getenv("CONTRACT_TASK_DEADLINE"), before)


class CleanupCancellationTests(unittest.IsolatedAsyncioTestCase):
    def _slow_semaphore(self, delay):
        class SlowSemaphore:
            def __init__(self):
                self.releasing = asyncio.Event()
                self.released = False
                self.cleanup_task = None

            async def acquire_async(self, **kwargs):
                return ModelSemaphoreLease("llm", "cleanup-probe", "own-member", 0, 1)

            async def keep_alive(self, lease):
                await asyncio.Event().wait()

            async def release_async(self, lease):
                self.cleanup_task = asyncio.current_task()
                self.releasing.set()
                await asyncio.sleep(delay)
                self.released = True
        return SlowSemaphore()

    async def test_slow_release_does_not_turn_successful_request_into_request_timeout(self):
        semaphore = self._slow_semaphore(0.15)
        async def send(_):
            return {}, 200
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}):
            result = await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                         budget=ModelCallBudget(timeout_seconds=0.08, max_retries=0))
        self.assertEqual(result, {})
        self.assertTrue(semaphore.released)
        self.assertTrue(semaphore.cleanup_task.done())

    async def test_repeated_cancellation_during_release_finishes_same_cleanup(self):
        semaphore = self._slow_semaphore(0.1)
        async def operation():
            async with llm_semaphore.model_semaphore("llm", "test", "test"):
                pass
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}):
            operation_task = asyncio.create_task(operation())
            await semaphore.releasing.wait()
            cleanup_task = semaphore.cleanup_task
            operation_task.cancel()
            await asyncio.sleep(0.01)
            operation_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation_task
        self.assertTrue(semaphore.released)
        self.assertIs(semaphore.cleanup_task, cleanup_task)
        self.assertTrue(cleanup_task.done())

    async def test_request_timeout_finishes_release_before_returning(self):
        semaphore = self._slow_semaphore(0.04)
        async def send(_):
            await asyncio.Event().wait()
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}):
            with self.assertRaises(ModelCallTimeout) as caught:
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=0.04, max_retries=0))
        self.assertEqual(caught.exception.failure_stage, "request")
        self.assertTrue(semaphore.released)
        self.assertTrue(semaphore.cleanup_task.done())

    async def test_request_timeout_preserves_timeout_when_redis_release_fails(self):
        semaphore = self._slow_semaphore(0)
        sends = []
        async def send(_):
            sends.append(True)
            await asyncio.Event().wait()
        release = AsyncMock(side_effect=redis.RedisError("release unavailable"))
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), \
                patch.object(semaphore, "release_async", release), \
                self.assertLogs("endpoints.runtime.model_limits", level="ERROR"):
            with self.assertRaises(ModelCallTimeout) as caught:
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=0.04, max_retries=3))
        self.assertEqual(caught.exception.failure_stage, "request")
        self.assertEqual(len(sends), 4)
        self.assertEqual(release.await_count, 4)

    async def test_external_cancellation_survives_redis_release_failure(self):
        semaphore = self._slow_semaphore(0)
        sending = asyncio.Event()
        async def send(_):
            sending.set()
            await asyncio.Event().wait()
        release = AsyncMock(side_effect=redis.RedisError("release unavailable"))
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), \
                patch.object(semaphore, "release_async", release), \
                self.assertLogs("endpoints.runtime.model_limits", level="ERROR"):
            operation = asyncio.create_task(execute_model(model_type="llm", model="test", caller="test",
                request={}, send=send, budget=ModelCallBudget(timeout_seconds=1, max_retries=0)))
            await sending.wait()
            operation.cancel("external stop")
            with self.assertRaisesRegex(asyncio.CancelledError, "external stop"):
                await operation
        release.assert_awaited_once()

    async def test_repeated_cancellation_does_not_restart_cleanup_deadline(self):
        started = asyncio.Event()
        finished = asyncio.Event()
        async def bounded_cleanup():
            try:
                async with asyncio.timeout(0.12):
                    started.set()
                    await asyncio.Event().wait()
            finally:
                finished.set()
        operation = asyncio.create_task(llm_semaphore._finish_cleanup(bounded_cleanup()))
        await started.wait()
        before = time.monotonic()
        operation.cancel()
        await asyncio.sleep(0.03)
        operation.cancel()
        with self.assertLogs("endpoints.runtime.model_limits", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                await operation
        self.assertTrue(finished.is_set())
        self.assertLess(time.monotonic() - before, 0.5)

    async def test_acquisition_cancel_race_finishes_cleanup_despite_second_cancel(self):
        acquiring = asyncio.Event()
        cleaning = asyncio.Event()
        cleaned = asyncio.Event()
        removals = []
        class PendingRedis:
            async def eval(self, *args):
                acquiring.set()
                await asyncio.Event().wait()
        async def slow_remove(member, *, include_owner):
            removals.append((member, include_owner))
            cleaning.set()
            await asyncio.sleep(0.08)
            cleaned.set()
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig("redis://unused/2", "own-key", 1, 30, 0.01))
        with patch.object(semaphore, "_async_client", return_value=PendingRedis()), \
                patch.object(semaphore, "_remove", slow_remove):
            operation = asyncio.create_task(semaphore.acquire_async(caller_name="test", model="test", budget=ModelCallBudget()))
            await acquiring.wait()
            operation.cancel()
            await cleaning.wait()
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
        self.assertTrue(cleaned.is_set())
        self.assertEqual(len(removals), 1)
        self.assertTrue(removals[0][1], "A cancellation-raced EVAL may already have acquired the caller's slot")


class RequestRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.acquired = 0
        self.released = 0
        self.active = 0
        case = self

        class Semaphore:
            async def acquire_async(self, **kwargs):
                case.acquired += 1
                case.active += 1
                return ModelSemaphoreLease("llm", "request-retry", str(case.acquired), 0, 1)

            async def keep_alive(self, lease):
                await asyncio.Event().wait()

            async def release_async(self, lease):
                case.active -= 1
                case.released += 1
        semaphore_patch = patch.dict(llm_semaphore._SEMAPHORES, {"llm": Semaphore()})
        semaphore_patch.start()
        self.addCleanup(semaphore_patch.stop)
        delay_patch = patch("endpoints.runtime.model_calls._retry_delay", return_value=0)
        delay_patch.start()
        self.addCleanup(delay_patch.stop)

    async def test_first_request_timeout_then_second_request_succeeds_with_full_timeout(self):
        timeouts = []
        cancelled = []
        async def send(remaining):
            self.assertEqual(self.active, 1)
            timeouts.append(remaining)
            if len(timeouts) == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            return {"ok": True}, 200
        budget = ModelCallBudget(timeout_seconds=0.2, max_retries=1)
        result = await execute_model(model_type="llm", model="test", caller="test", request={},
                                     send=send, budget=budget)
        self.assertTrue(result["ok"])
        self.assertEqual(cancelled, [True])
        self.assertEqual((budget.sends, self.acquired, self.released, self.active), (2, 2, 2, 0))
        self.assertTrue(all(timeout > 0.15 for timeout in timeouts), timeouts)

    async def test_three_extra_retries_send_exactly_four_requests_and_release_every_slot(self):
        timeouts = []
        async def send(remaining):
            timeouts.append(remaining)
            await asyncio.Event().wait()
        budget = ModelCallBudget(timeout_seconds=0.1, max_retries=3)
        with self.assertRaises(ModelCallTimeout) as caught:
            await execute_model(model_type="llm", model="test", caller="test", request={},
                                send=send, budget=budget)
        self.assertEqual(caught.exception.failure_stage, "request")
        self.assertEqual((len(timeouts), budget.sends, self.acquired, self.released, self.active), (4, 4, 4, 4, 0))
        self.assertTrue(all(timeout > 0.07 for timeout in timeouts), timeouts)

    async def test_38_second_invalid_response_does_not_reduce_repair_timeout(self):
        clock = [100.0]
        timeouts = []
        validations = []
        async def send(remaining):
            timeouts.append(remaining)
            clock[0] += 38 if len(timeouts) == 1 else 50
            return {"attempt": len(timeouts)}, 200
        def validate(response):
            validations.append(response["attempt"])
            clock[0] += 70  # Even expensive local validation cannot consume the next send's 60 seconds.
            if response["attempt"] == 1:
                raise ValueError("repair schema")
            return response
        with patch.object(model_budget, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)):
            budget = ModelCallBudget(timeout_seconds=60, max_retries=1, started=clock[0])
            result = await execute_model(model_type="llm", model="test", caller="test", request={},
                                         send=send, validate=validate, budget=budget)
        self.assertEqual(result, {"attempt": 2})
        self.assertEqual(timeouts, [60, 60])
        self.assertEqual(validations, [1, 2])
        self.assertEqual(budget.model_elapsed_seconds, 88)
        self.assertEqual(self.released, 2)

    async def test_synchronous_request_overrun_preserves_received_status_and_usage_before_retry(self):
        clock = [100.0]
        sends = []
        async def send(remaining):
            sends.append(remaining)
            clock[0] += 61 if len(sends) == 1 else 10
            return {"ok": True, "usage": {"prompt_tokens": 3, "completion_tokens": 2}}, 200
        with patch.object(model_budget, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)):
            with logical_call("test.llm", "test") as logical:
                budget = ModelCallBudget(timeout_seconds=60, max_retries=1, started=clock[0])
                result = await execute_model(model_type="llm", model="test", caller="test", request={},
                                             send=send, budget=budget)
                first, second = logical.payload["attempts"]
        self.assertTrue(result["ok"])
        self.assertEqual(sends, [60, 60])
        self.assertEqual((first["http_status"], first["input_tokens"], first["output_tokens"]), (200, 3, 2))
        self.assertEqual(first["failure_stage"], "request")
        self.assertEqual(first["error"]["type"], "ModelCallTimeout")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(self.released, 2)

    async def test_wait_timeout_does_not_consume_sends_or_retry_acquisition(self):
        semaphore = AsyncMock(side_effect=ModelSemaphoreTimeout("wait exhausted"))
        send = AsyncMock()
        budget = ModelCallBudget(timeout_seconds=0.05, max_retries=3)
        with patch("endpoints.runtime.model_calls.model_semaphore", side_effect=semaphore.side_effect) as acquire:
            with self.assertRaises(ModelSemaphoreTimeout):
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=budget)
        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(budget.sends, 0)
        send.assert_not_awaited()

    async def test_task_deadline_stops_request_without_retries(self):
        sends = []
        async def send(_):
            sends.append(True)
            await asyncio.Event().wait()
        with task_execution(time.time() + 0.05):
            with self.assertRaises(TaskDeadlineExceeded):
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=1, max_retries=3))
        self.assertEqual((len(sends), self.acquired, self.released), (1, 1, 1))

    async def test_external_cancellation_never_starts_a_retry(self):
        started = asyncio.Event()
        async def send(_):
            started.set()
            await asyncio.Event().wait()
        budget = ModelCallBudget(timeout_seconds=1, max_retries=3)
        operation = asyncio.create_task(execute_model(model_type="llm", model="test", caller="test", request={},
                                                     send=send, budget=budget))
        await started.wait()
        operation.cancel("user cancellation")
        with self.assertRaisesRegex(asyncio.CancelledError, "user cancellation"):
            await operation
        self.assertEqual((budget.sends, self.acquired, self.released), (1, 1, 1))


@unittest.skipUnless(os.getenv("TEST_MODEL_REDIS_URL"), "Set TEST_MODEL_REDIS_URL to an isolated Redis")
class ModelControlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis_url = os.environ["TEST_MODEL_REDIS_URL"]
        self.redis = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self.redis.ping()
        self.prefix = "contract-control-test:" + uuid.uuid4().hex
        self.semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(
            self.redis_url, self.prefix + ":llm", 1, 30, 0.005))
        self.patch = patch.dict(llm_semaphore._SEMAPHORES, {"llm": self.semaphore})
        self.patch.start()
        await self.semaphore._async_client().ping()
        self.http = _HTTPFixture()

    async def asyncTearDown(self):
        await llm_semaphore.close_model_clients()
        self.patch.stop()
        await asyncio.to_thread(self.http.close)
        keys = list(self.redis.scan_iter(self.prefix + "*"))
        if keys:
            self.redis.delete(*keys)
        self.redis.close()

    async def _send(self, path, remaining):
        async with httpx.AsyncClient(trust_env=False, timeout=remaining, verify=False) as client:
            response = await client.post(self.http.url + path, json={})
            response.raise_for_status()
            return response.json(), response.status_code

    def _assert_no_waiters(self):
        self.assertEqual(self.redis.zcard(self.semaphore.config.key + ":waiting"), 0)
        self.assertEqual(self.redis.zcard(self.semaphore.config.key + ":deadlines"), 0)

    async def _wait_for_waiters(self, count):
        async with asyncio.timeout(3):
            while self.redis.zcard(self.semaphore.config.key + ":waiting") != count:
                await asyncio.sleep(0.005)

    async def test_waiting_uses_separate_budget_and_does_not_remove_other_owner(self):
        owner = await self.semaphore.acquire_async(caller_name="owner", model="test", budget=ModelCallBudget())
        sends = []
        async def send(remaining):
            sends.append(remaining)
            return {}, 200
        started = time.monotonic()
        budget = ModelCallBudget(timeout_seconds=5, wait_timeout_seconds=0.3)
        with self.assertRaises(ModelSemaphoreTimeout) as caught:
            await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                budget=budget)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(caught.exception.failure_stage, "semaphore_wait")
        self.assertEqual(sends, [])
        self.assertEqual(budget.sends, 0)
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 1)
        self.assertIsNotNone(self.redis.zscore(self.semaphore.config.key, owner.member))
        self._assert_no_waiters()
        await self.semaphore.release_async(owner)

    async def test_wait_longer_than_model_budget_then_complete_real_http_request(self):
        owner = await self.semaphore.acquire_async(caller_name="owner", model="test", budget=ModelCallBudget())
        request_budgets = []
        async def send(remaining):
            request_budgets.append(remaining)
            return await self._send("/llm", remaining)
        budget = ModelCallBudget(timeout_seconds=0.5, wait_timeout_seconds=2, max_retries=0)
        with logical_call("test.llm", "test") as logical:
            operation = asyncio.create_task(execute_model(model_type="llm", model="test", caller="test",
                request={}, send=send, budget=budget))
            try:
                await self._wait_for_waiters(1)
                await asyncio.sleep(0.65)
                self.assertFalse(operation.done(), "Waiting must outlive the shorter model budget")
                await self.semaphore.release_async(owner)
                result = await operation
            finally:
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                await self.semaphore.release_async(owner)
        self.assertTrue(result["ok"])
        self.assertEqual(logical.sent_count, 1)
        self.assertGreater(request_budgets[0], 0.35)
        timing = logical.payload["attempts"][0]["timing"]
        self.assertGreaterEqual(timing["queue_wait_seconds"], 0.65)
        self.assertGreaterEqual(timing["request_seconds"], 0.1)
        self.assertGreater(timing["total_seconds"], budget.timeout_seconds)
        self.assertLess(timing["model_elapsed_seconds"], budget.timeout_seconds)
        self.assertAlmostEqual(timing["waiting_total_seconds"], timing["queue_wait_seconds"], places=5)
        self.assertAlmostEqual(timing["request_seconds"], timing["model_elapsed_seconds"], delta=0.005)
        self.assertGreaterEqual(timing["total_seconds"] + 0.005,
            timing["waiting_total_seconds"] + timing["model_elapsed_seconds"])
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)
        self._assert_no_waiters()

    async def test_schema_network_and_timeout_retries_share_cumulative_wait_allowance(self):
        for retry_kind in ("schema", "network", "timeout"):
            with self.subTest(retry_kind=retry_kind):
                owner = await self.semaphore.acquire_async(caller_name="first-owner", model="test", budget=ModelCallBudget())
                original_release = self.semaphore.release_async
                blockers = []
                sends = []
                async def replace_owner(lease, *, original_release=original_release, owner=owner, blockers=blockers):
                    await original_release(lease)
                    if lease.member != owner.member and not blockers:
                        blockers.append(await self.semaphore.acquire_async(caller_name="second-owner", model="test",
                            budget=ModelCallBudget()))
                async def send(_, *, sends=sends, retry_kind=retry_kind):
                    sends.append(True)
                    if retry_kind == "network":
                        response = httpx.Response(429, headers={"Retry-After": "0"},
                            request=httpx.Request("POST", self.http.url))
                        response.raise_for_status()
                    if retry_kind == "timeout":
                        raise httpx.ReadTimeout("model response timed out", request=httpx.Request("POST", self.http.url))
                    return {}, 200
                def validate(_):
                    raise ValueError("repair schema")
                budget = ModelCallBudget(timeout_seconds=5, wait_timeout_seconds=2, max_retries=3)
                with logical_call("test.llm", "test") as logical:
                    with patch.object(self.semaphore, "release_async", replace_owner):
                        operation = asyncio.create_task(execute_model(model_type="llm", model="test", caller="test",
                            request={}, send=send, validate=validate if retry_kind == "schema" else None, budget=budget))
                        try:
                            await self._wait_for_waiters(1)
                            await asyncio.sleep(0.9)
                            await original_release(owner)
                            with self.assertRaises(ModelSemaphoreTimeout):
                                await operation
                        finally:
                            if not operation.done():
                                operation.cancel()
                                await asyncio.gather(operation, return_exceptions=True)
                            await original_release(owner)
                    self.assertEqual(logical.sent_count, 1)
                    self.assertEqual(len(logical.payload["attempts"]), 2)
                    self.assertFalse(logical.payload["attempts"][1]["sent"])
                    self.assertLess(logical.payload["attempts"][1]["timing"]["queue_wait_seconds"], 1.5,
                        "The second attempt only has the first attempt's unused wait allowance")
                self.assertEqual(len(sends), 1)
                self.assertGreater(budget.wait_seconds, 1.8)
                self.assertEqual(len(blockers), 1)
                self.assertIsNotNone(self.redis.zscore(self.semaphore.config.key, blockers[0].member))
                self._assert_no_waiters()
                await original_release(blockers[0])

    async def test_task_deadline_stops_wait_before_wait_allowance_expires(self):
        owner = await self.semaphore.acquire_async(caller_name="owner", model="test", budget=ModelCallBudget())
        send = AsyncMock()
        try:
            with task_execution(time.time() + 0.25):
                budget = ModelCallBudget(timeout_seconds=5, wait_timeout_seconds=5)
                with self.assertRaises(TaskDeadlineExceeded) as caught:
                    await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                        budget=budget)
            self.assertEqual(caught.exception.failure_stage, "task_deadline")
            self.assertEqual(budget.sends, 0)
            send.assert_not_awaited()
            self.assertIsNotNone(self.redis.zscore(self.semaphore.config.key, owner.member))
            self._assert_no_waiters()
        finally:
            await self.semaphore.release_async(owner)

    async def test_task_deadline_still_stops_request_after_waiting(self):
        owner = await self.semaphore.acquire_async(caller_name="owner", model="test", budget=ModelCallBudget())
        request_started = asyncio.Event()
        request_cancelled = asyncio.Event()
        async def send(_):
            request_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                request_cancelled.set()
        # Redis round trips can exceed the old 0.7-second fixture deadline on
        # Docker Desktop. Leave time to enter the request before testing its
        # cancellation by the original contract deadline.
        with task_execution(time.time() + 3):
            budget = ModelCallBudget(timeout_seconds=5, wait_timeout_seconds=5)
            operation = asyncio.create_task(execute_model(model_type="llm", model="test", caller="test",
                request={}, send=send, budget=budget))
            try:
                await self._wait_for_waiters(1)
                await asyncio.sleep(0.3)
                await self.semaphore.release_async(owner)
                await asyncio.wait_for(request_started.wait(), timeout=2)
                with self.assertRaises(TaskDeadlineExceeded) as caught:
                    await operation
            finally:
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                await self.semaphore.release_async(owner)
        self.assertEqual(caught.exception.failure_stage, "task_deadline")
        self.assertTrue(request_started.is_set())
        self.assertTrue(request_cancelled.is_set())
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)
        self._assert_no_waiters()

    async def test_request_timeout_releases_only_its_own_slot(self):
        original = self.semaphore.config
        self.semaphore.config = ModelSemaphoreConfig(original.redis_url, original.key, 2,
            original.lease_seconds, original.poll_interval_seconds)
        owner = await self.semaphore.acquire_async(caller_name="other-contract", model="test", budget=ModelCallBudget())
        async def send(_):
            self.assertEqual(self.redis.zcard(self.semaphore.config.key), 2)
            await asyncio.Event().wait()
        try:
            with self.assertRaises(ModelCallTimeout) as caught:
                await execute_model(model_type="llm", model="test", caller="timed-out-contract", request={}, send=send,
                    budget=ModelCallBudget(timeout_seconds=0.2, wait_timeout_seconds=2, max_retries=0))
            self.assertEqual(caught.exception.failure_stage, "request")
            self.assertEqual(self.redis.zcard(self.semaphore.config.key), 1)
            self.assertIsNotNone(self.redis.zscore(self.semaphore.config.key, owner.member))
            self._assert_no_waiters()
        finally:
            await self.semaphore.release_async(owner)

    async def test_timeout_retry_requeues_behind_other_contract_without_bypassing_shared_limit(self):
        order = []
        request_budgets = []
        first_started = asyncio.Event()
        async def retrying_send(remaining):
            request_budgets.append(remaining)
            order.append(f"retrying-{len(request_budgets)}")
            self.assertEqual(self.redis.zcard(self.semaphore.config.key), 1)
            if len(request_budgets) == 1:
                first_started.set()
                await asyncio.Event().wait()
            return {"ok": True}, 200
        async def other_send(_):
            order.append("other-contract")
            self.assertEqual(self.redis.zcard(self.semaphore.config.key), 1)
            await asyncio.sleep(0.05)
            return {}, 200
        budget = ModelCallBudget(timeout_seconds=0.3, wait_timeout_seconds=4, max_retries=1)
        with patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
            operation = asyncio.create_task(execute_model(model_type="llm", model="test", caller="retrying",
                request={}, send=retrying_send, budget=budget))
            competitor = None
            try:
                await asyncio.wait_for(first_started.wait(), 2)
                competitor = asyncio.create_task(execute_model(model_type="llm", model="test", caller="other",
                    request={}, send=other_send, budget=ModelCallBudget(timeout_seconds=1, max_retries=0)))
                await self._wait_for_waiters(1)
                await asyncio.gather(operation, competitor)
            finally:
                pending = [task for task in (operation, competitor) if task is not None and not task.done()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        self.assertEqual(order, ["retrying-1", "other-contract", "retrying-2"])
        self.assertEqual(budget.sends, 2)
        self.assertTrue(all(timeout > 0.25 for timeout in request_budgets), request_budgets)
        self.assertGreater(budget.wait_seconds, 0.05)
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)
        self._assert_no_waiters()

    async def test_fifo_and_cancelled_waiter_cleanup(self):
        owner = await self.semaphore.acquire_async(caller_name="owner", model="test", budget=ModelCallBudget())
        order = []
        async def wait(name):
            lease = await self.semaphore.acquire_async(caller_name=name, model="test", budget=ModelCallBudget())
            order.append(name)
            await asyncio.sleep(0.01)
            await self.semaphore.release_async(lease)
        first = asyncio.create_task(wait("first"))
        while self.redis.zcard(self.semaphore.config.key + ":waiting") != 1:
            await asyncio.sleep(0.005)
        cancelled = asyncio.create_task(wait("cancelled"))
        while self.redis.zcard(self.semaphore.config.key + ":waiting") != 2:
            await asyncio.sleep(0.005)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        second = asyncio.create_task(wait("second"))
        while self.redis.zcard(self.semaphore.config.key + ":waiting") != 2:
            await asyncio.sleep(0.005)
        await self.semaphore.release_async(owner)
        await asyncio.gather(first, second)
        self.assertEqual(order, ["first", "second"])
        self._assert_no_waiters()
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)

    async def test_http_retry_and_schema_repair_share_send_allowance(self):
        def reject(_):
            raise ValueError("schema requires another repair")
        async def send(remaining):
            return await self._send("/retry", remaining)
        with logical_call("test.llm", "test") as logical:
            with self.assertRaisesRegex(ValueError, "schema requires"):
                await execute_model(model_type="llm", model="test", caller="test",
                                    request={"messages": [{"role": "user", "content": "test"}]}, send=send,
                                    validate=reject, budget=ModelCallBudget(timeout_seconds=3, max_retries=1))
            self.assertEqual(self.http.counts["retry"], 2)
            self.assertEqual(logical.sent_count, 2)
            self.assertGreater(logical.payload["attempts"][0]["input_tokens"], 0)
            self.assertEqual(logical.payload["attempts"][0]["input_tokens"],
                             logical.payload["attempts"][0]["local_input_tokens"])
            self.assertEqual(logical.payload["attempts"][1]["input_tokens"], 3)

    async def test_trickle_response_is_cancelled_at_total_deadline(self):
        started = time.monotonic()
        with self.assertRaises(ModelCallTimeout):
            await execute_model(model_type="llm", model="test", caller="test", request={},
                send=lambda remaining: self._send("/trickle", remaining),
                budget=ModelCallBudget(timeout_seconds=0.4, max_retries=0))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(await asyncio.to_thread(self.http.disconnected.wait, 1))
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)

    async def test_real_http_request_timeout_is_retried_with_fresh_request_deadline(self):
        request_budgets = []
        async def send(remaining):
            request_budgets.append(remaining)
            return await self._send("/timeout-once", remaining)
        with patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
            result = await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                         budget=ModelCallBudget(timeout_seconds=0.4, max_retries=1))
        self.assertTrue(result["ok"])
        self.assertEqual(self.http.counts["timeout-once"], 2)
        self.assertTrue(all(timeout > 0.35 for timeout in request_budgets), request_budgets)
        self.assertTrue(await asyncio.to_thread(self.http.disconnected.wait, 1))
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)
        self._assert_no_waiters()

    async def test_backoff_does_not_consume_next_request_timeout(self):
        count = 0
        request_budgets = []
        async def send(remaining):
            nonlocal count
            count += 1
            request_budgets.append(remaining)
            if count == 1:
                response = httpx.Response(429, headers={"Retry-After": "0.15"}, request=httpx.Request("POST", self.http.url))
                response.raise_for_status()
            return {"ok": True}, 200
        started = time.monotonic()
        result = await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                     budget=ModelCallBudget(timeout_seconds=0.1, max_retries=1))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(count, 2)
        self.assertTrue(all(remaining > 0.08 for remaining in request_budgets), request_budgets)

    async def test_task_deadline_stops_backoff_without_another_send(self):
        count = 0
        async def send(_):
            nonlocal count
            count += 1
            response = httpx.Response(429, headers={"Retry-After": "10"}, request=httpx.Request("POST", self.http.url))
            response.raise_for_status()
        with task_execution(time.time() + 0.3):
            with self.assertRaises(TaskDeadlineExceeded) as caught:
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=1, max_retries=3))
        self.assertEqual(caught.exception.failure_stage, "task_deadline")
        self.assertEqual(count, 1)

    async def test_real_redis_release_can_finish_after_request_timeout_duration(self):
        original_release = self.semaphore.release_async
        cleanup_tasks = []
        async def slow_release(lease):
            cleanup_tasks.append(asyncio.current_task())
            await asyncio.sleep(0.15)
            await original_release(lease)
        async def send(_):
            return {}, 200
        with patch.object(self.semaphore, "release_async", slow_release):
            result = await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                         budget=ModelCallBudget(timeout_seconds=0.08, max_retries=0))
        self.assertEqual(result, {})
        self.assertEqual(len(cleanup_tasks), 1)
        self.assertTrue(cleanup_tasks[0].done())
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)
        self._assert_no_waiters()

    async def test_killed_slot_owner_is_reclaimed_after_lease_expiry(self):
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        key = self.prefix + ":killed-owner"
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, key, 1, 1, 0.005))
        process = context.Process(target=_worker_holds_slot, args=(self.redis_url, key, 1, result_queue))
        process.start()
        try:
            owner = await asyncio.to_thread(result_queue.get, True, 5)
            process.terminate()
            await asyncio.to_thread(process.join, 5)
            self.assertFalse(process.is_alive())
            self.assertEqual(self.redis.zcard(key), 1, "Hard process termination cannot actively release its slot")
            lease = await semaphore.acquire_async(caller_name="successor", model="test",
                                                   budget=ModelCallBudget(timeout_seconds=3, max_retries=0))
            self.assertIsNone(self.redis.zscore(key, owner))
            self.assertEqual(self.redis.zcard(key), 1)
            await semaphore.release_async(lease)
            self.assertEqual(self.redis.zcard(key), 0)
            self.assertEqual(self.redis.zcard(key + ":waiting"), 0)
            self.assertEqual(self.redis.zcard(key + ":deadlines"), 0)
        finally:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
            client = semaphore._clients.pop(asyncio.get_running_loop(), None)
            if client:
                await client.aclose()
            result_queue.close()

    async def test_real_slot_release_error_does_not_resend_model_request(self):
        sends = []
        async def send(_):
            sends.append(True)
            return {}, 200
        with patch.object(self.semaphore, "_remove", AsyncMock(side_effect=redis.RedisError("release unavailable"))):
            with self.assertRaisesRegex(redis.RedisError, "release unavailable") as caught:
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=1, max_retries=3))
        self.assertEqual(caught.exception.failure_stage, "semaphore_release")
        self.assertEqual(len(sends), 1)
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 1, "An unavailable Redis requires lease recovery")

    async def test_three_processes_share_all_model_caps(self):
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        processes = [context.Process(target=_worker_requests,
            args=(self.redis_url, self.prefix, self.http.url, result_queue)) for _ in range(3)]
        for process in processes:
            process.start()
        try:
            for process in processes:
                await asyncio.to_thread(process.join, 35)
                self.assertFalse(process.is_alive(), "Model test worker did not finish")
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([result_queue.get(timeout=2) for _ in processes], [None] * 3)
            for kind, limit in (("llm", 5), ("embedding", 3), ("reranker", 10)):
                self.assertEqual(self.http.counts[kind], 24)
                self.assertLessEqual(self.http.peaks[kind], limit)
                self.assertGreater(self.http.peaks[kind], 1 if limit > 1 else 0)
                self.assertEqual(self.redis.zcard(f"{self.prefix}:{kind}"), 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            result_queue.close()

    async def test_three_processes_queue_fifteen_requests_with_full_model_budget(self):
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        start_barrier = context.Barrier(3)
        processes = [context.Process(target=_worker_requests,
            args=(self.redis_url, self.prefix, self.http.url, result_queue), kwargs={
                "request_count": 5, "timeout_seconds": 1.2, "wait_timeout_seconds": 6,
                "model_limits": (("llm", 5),), "request_path": "slow-model", "start_barrier": start_barrier,
                "report_timings": True,
            }) for _ in range(3)]
        for process in processes:
            process.start()
        try:
            for process in processes:
                await asyncio.to_thread(process.join, 20)
                self.assertFalse(process.is_alive(), "Queued model test worker did not finish")
                self.assertEqual(process.exitcode, 0)
            results = [result_queue.get(timeout=2) for _ in processes]
            self.assertTrue(all(isinstance(result, list) for result in results), results)
            calls = [call for result in results for call in result]
            self.assertEqual(len(calls), 15)
            self.assertTrue(all(call["sends"] == 1 for call in calls))
            self.assertGreater(max(call["wait_seconds"] for call in calls), 1.2,
                "At least one successful call must queue longer than its execution budget")
            self.assertEqual(self.http.counts["slow-model"], 15)
            self.assertEqual(self.http.peaks["slow-model"], 5)
            self.assertEqual(self.redis.zcard(self.semaphore.config.key), 0)
            self._assert_no_waiters()
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 5)
            result_queue.close()


if __name__ == "__main__":
    unittest.main()
