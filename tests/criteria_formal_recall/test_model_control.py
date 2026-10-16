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
from unittest.mock import AsyncMock, patch

import httpx
import redis

from endpoints.runtime import llm_semaphore
from endpoints.runtime.llm_semaphore import ModelSemaphoreConfig, ModelSemaphoreLease, RedisModelSemaphore
from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout, task_execution
from endpoints.runtime.model_calls import execute_model
from loggers.call_log import logical_call


def _worker_requests(redis_url, prefix, http_url, result_queue):
    async def run():
        semaphores = {kind: RedisModelSemaphore(kind, ModelSemaphoreConfig(
            redis_url, f"{prefix}:{kind}", limit, 30, 0.005))
            for kind, limit in (("llm", 5), ("embedding", 3), ("reranker", 10))}
        try:
            with patch.dict(llm_semaphore._SEMAPHORES, semaphores):
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    async def one(kind):
                        async def send(remaining):
                            response = await client.post(f"{http_url}/{kind}", json={}, timeout=remaining)
                            response.raise_for_status()
                            return response.json(), response.status_code
                        with logical_call(f"test.{kind}", "local-test"):
                            return await execute_model(model_type=kind, model="local-test", caller=f"test.{kind}",
                                request={}, send=send, budget=ModelCallBudget(timeout_seconds=15, max_retries=0))
                    await asyncio.gather(*(one(kind) for kind in semaphores for _ in range(8)))
        finally:
            for semaphore in semaphores.values():
                client = semaphore._clients.pop(asyncio.get_running_loop(), None)
                if client:
                    await client.aclose()
    try:
        asyncio.run(run())
        result_queue.put(None)
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
                try:
                    if kind == "trickle":
                        self.send_response(200)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        for _ in range(1000):
                            self.wfile.write(b" ")
                            self.wfile.flush()
                            time.sleep(0.02)
                    else:
                        time.sleep(0.1)
                        status = 429 if kind == "retry" and attempt == 1 else 200
                        body = json.dumps({"ok": True, "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
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
    def test_task_deadline_caps_new_call_and_restores_environment(self):
        before = os.getenv("CONTRACT_TASK_DEADLINE")
        with task_execution(time.time() + 0.1, task_id="budget-test"):
            budget = ModelCallBudget(timeout_seconds=60)
            self.assertLessEqual(budget.remaining(), 0.1)
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

            async def release_async(self, lease):
                self.cleanup_task = asyncio.current_task()
                self.releasing.set()
                await asyncio.sleep(delay)
                self.released = True
        return SlowSemaphore()

    async def test_deadline_during_release_waits_for_cleanup_before_returning(self):
        semaphore = self._slow_semaphore(0.15)
        async def send(_):
            return {}, 200
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}):
            with self.assertRaises(ModelCallTimeout) as caught:
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=0.08, max_retries=0))
        self.assertEqual(caught.exception.failure_stage, "semaphore_release")
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
                self.assertLogs("endpoints.runtime.llm_semaphore", level="ERROR"):
            with self.assertRaises(ModelCallTimeout) as caught:
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=0.04, max_retries=3))
        self.assertEqual(caught.exception.failure_stage, "request")
        self.assertEqual(len(sends), 1)
        release.assert_awaited_once()

    async def test_external_cancellation_survives_redis_release_failure(self):
        semaphore = self._slow_semaphore(0)
        sending = asyncio.Event()
        async def send(_):
            sending.set()
            await asyncio.Event().wait()
        release = AsyncMock(side_effect=redis.RedisError("release unavailable"))
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), \
                patch.object(semaphore, "release_async", release), \
                self.assertLogs("endpoints.runtime.llm_semaphore", level="ERROR"):
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
        with self.assertLogs("endpoints.runtime.llm_semaphore", level="ERROR"):
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

    async def test_waiting_uses_call_budget_and_does_not_remove_other_owner(self):
        owner = await self.semaphore.acquire_async(caller_name="owner", model="test", budget=ModelCallBudget())
        sends = []
        async def send(remaining):
            sends.append(remaining)
            return {}, 200
        started = time.monotonic()
        with self.assertRaises(ModelCallTimeout):
            await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                budget=ModelCallBudget(timeout_seconds=0.3))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(sends, [])
        self.assertEqual(self.redis.zcard(self.semaphore.config.key), 1)
        self._assert_no_waiters()
        await self.semaphore.release_async(owner)

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
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    validate=reject, budget=ModelCallBudget(timeout_seconds=3, max_retries=1))
            self.assertEqual(self.http.counts["retry"], 2)
            self.assertEqual(logical.sent_count, 2)
            self.assertIsNone(logical.payload["attempts"][0]["input_tokens"])
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

    async def test_backoff_cannot_extend_budget(self):
        count = 0
        async def send(_):
            nonlocal count
            count += 1
            response = httpx.Response(429, headers={"Retry-After": "10"}, request=httpx.Request("POST", self.http.url))
            response.raise_for_status()
        started = time.monotonic()
        with self.assertRaises(ModelCallTimeout) as caught:
            await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                budget=ModelCallBudget(timeout_seconds=1.0, max_retries=1))
        self.assertEqual(caught.exception.failure_stage, "retry_backoff")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(count, 1)

    async def test_deadline_during_real_redis_release_leaves_no_cleanup_task(self):
        original_release = self.semaphore.release_async
        cleanup_tasks = []
        async def slow_release(lease):
            cleanup_tasks.append(asyncio.current_task())
            await asyncio.sleep(0.15)
            await original_release(lease)
        async def send(_):
            return {}, 200
        with patch.object(self.semaphore, "release_async", slow_release):
            with self.assertRaises(ModelCallTimeout) as caught:
                await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                    budget=ModelCallBudget(timeout_seconds=0.08, max_retries=0))
        self.assertEqual(caught.exception.failure_stage, "semaphore_release")
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


if __name__ == "__main__":
    unittest.main()
