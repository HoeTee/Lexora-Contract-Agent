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
from unittest.mock import patch

import httpx
import redis

from endpoints.runtime import llm_semaphore
from endpoints.runtime.llm_semaphore import ModelSemaphoreConfig, RedisModelSemaphore
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
