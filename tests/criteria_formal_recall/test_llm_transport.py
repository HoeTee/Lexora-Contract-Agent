"""Exercise wire streaming through SDK parsing, deadlines, retries and slots."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from llama_index.core.base.llms.types import ChatMessage
from openai import APIConnectionError, AsyncOpenAI
import redis
import yaml

from agents.base_agent import Agent, Settings
from endpoints.runtime import llm_client
from endpoints.runtime import llm_semaphore
from endpoints.runtime.llm_semaphore import ModelSemaphoreConfig, RedisModelSemaphore
from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout
from endpoints.runtime.model_calls import execute_model
from loggers.call_log import logical_call, reset_call_context, set_call_context
from tools.docxindex.llm.client import LLMClient, LLMSettings
from tools.retrieval.llamaindex.limited_llm import UnifiedOpenAILike


def completion(content="answer"):
    return {"id": "test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}}


def chunk(delta=None, *, finish=None, index=0, usage=None):
    return {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "test",
            "choices": [{"index": index, "delta": delta or {}, "finish_reason": finish}] if usage is None else [],
            **({"usage": usage} if usage is not None else {})}


def event(data):
    return ("data: " + (json.dumps(data) if isinstance(data, dict) else data) + "\n\n").encode()


class ResponseBody(httpx.AsyncByteStream):
    def __init__(self, parts, *, delay=0, error=None):
        self.parts, self.delay, self.error = parts, delay, error
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield part
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def streamed(parts, **kwargs):
    body = ResponseBody([event(part) for part in parts], **kwargs)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body), body


def success(request):
    if json.loads(request.content)["stream"]:
        return streamed([chunk({"role": "assistant", "content": "ans"}), chunk({"content": "wer"}),
                         chunk(finish="stop"), chunk(usage=completion()["usage"]), "[DONE]"])[0]
    return httpx.Response(200, json=completion())


class LLMStreamConfigurationTests(unittest.TestCase):
    def test_startup_reads_the_single_switch_and_defaults_to_nonstreaming(self):
        root = Path(__file__).resolve().parents[2]
        config = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            shutil.copy2(root / "config.py", folder / "config.py")
            (folder / ".env").write_text("LLM_API_KEY=fake\nEMBED_API_KEY=fake\nSESSION_SECRET_KEY=fake\n")
            for value in (False, True, "absent", None, "invalid"):
                with self.subTest(value=value):
                    if value == "absent":
                        config["llm"].pop("stream", None)
                    else:
                        config["llm"]["stream"] = value
                    (folder / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
                    result = subprocess.run([sys.executable, "-c", "import config; print('STREAM='+str(config.LLM_STREAM))"],
                                            cwd=folder, capture_output=True, text=True, timeout=15)
                    if value is None or value == "invalid":
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("llm.stream must be true or false", result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("STREAM=" + str(value is True), result.stdout)


class LLMTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.active = self.acquired = self.released = 0
        self.requests = []
        self.bodies = []
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        token = set_call_context(Path(self.temp.name) / "events.log")
        self.addCleanup(reset_call_context, token)

        @asynccontextmanager
        async def slot(*args, **kwargs):
            self.active += 1
            self.acquired += 1
            try:
                yield SimpleNamespace(key="test:llm")
            finally:
                self.active -= 1
                self.released += 1

        for change in (patch("endpoints.runtime.model_calls.model_semaphore", slot),
                       patch("endpoints.runtime.model_calls._retry_delay", return_value=0)):
            change.start()
            self.addCleanup(change.stop)

    async def call(self, handler, *, mode=True, timeout=2, retries=0, **kwargs):
        def wrapped(request):
            self.assertEqual(self.active, 1, "HTTP must hold the shared slot")
            self.requests.append(json.loads(request.content))
            return handler(request)

        async with AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped))) as client:
            with patch.object(llm_client, "LLM_STREAM", mode), logical_call("test.llm", "test") as logical:
                self.logical = logical
                async def send(remaining):
                    return await llm_client.request_llm(client.chat.completions,
                        model="test", messages=[{"role": "user", "content": "query"}], timeout=remaining, **kwargs)
                return await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                    budget=ModelCallBudget(timeout_seconds=timeout, max_retries=retries),
                    serialize=lambda value: value.model_dump())

    async def test_both_modes_return_same_complete_result_and_usage(self):
        for mode in (False, True):
            with self.subTest(stream=mode):
                response = await self.call(success, mode=mode, stream=not mode,
                    extra_body={"stream": not mode, "enable_thinking": False})
                self.assertEqual(response.choices[0].message.content, "answer")
                self.assertEqual(response.usage.total_tokens, 12)
                record = self.logical.payload["attempts"][0]
                self.assertEqual((record["transport"], record["stream"]), ("unified_llm", mode))
                self.assertEqual(record["stream_complete"], True if mode else None)
                self.assertEqual(record["first_chunk_seconds"] is not None, mode)
                self.assertGreater(record["response_seconds"], 0)
                self.assertEqual(self.requests[-1]["stream"], mode)
                self.assertEqual("stream_options" in self.requests[-1], mode)
        self.assertEqual((self.acquired, self.released, self.active), (2, 2, 0))

    async def test_parallel_tool_calls_and_reasoning_are_assembled_before_return(self):
        parts = [chunk({"role": "assistant", "reasoning_content": "think"}),
                 chunk({"tool_calls": [{"index": 1, "id": "b", "type": "function",
                                        "function": {"name": "second", "arguments": '{"q":'}}]}),
                 chunk({"tool_calls": [{"index": 0, "id": "a", "type": "function",
                                        "function": {"name": "first", "arguments": '{"q":'}}]}),
                 chunk({"reasoning_content": "ing", "tool_calls": [
                     {"index": 0, "function": {"arguments": '"one"}'}},
                     {"index": 1, "function": {"arguments": '"two"}'}}]}),
                 chunk(finish="tool_calls"), "[DONE]"]
        response = await self.call(lambda _: streamed(parts)[0])
        message = response.choices[0].message
        self.assertEqual(message.reasoning_content, "thinking")
        self.assertEqual([tool.id for tool in message.tool_calls], ["a", "b"])
        self.assertEqual([json.loads(tool.function.arguments) for tool in message.tool_calls],
                         [{"q": "one"}, {"q": "two"}])
        self.assertIsNone(response.usage)
        self.assertIsNone(self.logical.payload["input_tokens"])

    async def test_empty_and_unfinished_streams_retry_without_returning_partial_text(self):
        for parts in ([], [chunk({"content": "discard this"})]):
            before = self.acquired
            def handler(request, *, before=before, parts=parts):
                if self.acquired == before + 1:
                    response, body = streamed(parts)
                    self.bodies.append(body)
                    return response
                return success(request)
            result = await self.call(handler, retries=1)
            self.assertEqual(result.choices[0].message.content, "answer")
            first, second = self.logical.payload["attempts"]
            self.assertEqual(first["error"]["type"], "IncompleteLLMStream")
            self.assertEqual((first["stream_complete"], second["stream_complete"]), (False, True))
            self.assertEqual(self.active, 0)
        self.assertTrue(all(body.closed for body in self.bodies))

    async def test_timeout_covers_all_chunks_and_each_retry_has_a_new_timer(self):
        for mode in (False, True):
            before = self.acquired
            def handler(_, *, mode=mode):
                if mode:
                    response, body = streamed([chunk({"content": "x"})] * 50, delay=0.02)
                else:
                    body = ResponseBody([b" "] * 50, delay=0.02)
                    response = httpx.Response(200, stream=body)
                self.bodies.append(body)
                return response
            with self.assertRaises(ModelCallTimeout):
                await self.call(handler, mode=mode, timeout=0.12, retries=1)
            self.assertEqual(self.acquired - before, 2)
            self.assertEqual(self.active, 0)
            attempts = self.logical.payload["attempts"]
            self.assertTrue(all(attempt["remaining_seconds"] > 0.10 for attempt in attempts))
            self.assertTrue(all(attempt["timing"]["request_seconds"] >= 0.10 for attempt in attempts))
            self.assertTrue(all(attempt["input_tokens"] is None for attempt in attempts))
        self.assertTrue(all(body.closed for body in self.bodies))

    async def test_disconnects_in_both_modes_close_connections_release_and_retry(self):
        for mode in (False, True):
            before = self.acquired
            def handler(request, *, before=before, mode=mode):
                if self.acquired == before + 1:
                    parts = [event(chunk({"content": "discard"}))] if mode else [b'{"choices":']
                    body = ResponseBody(parts, error=httpx.RemoteProtocolError("connection lost"))
                    self.bodies.append(body)
                    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
                return success(request)
            response = await self.call(handler, mode=mode, retries=1)
            self.assertEqual(response.choices[0].message.content, "answer")
            self.assertEqual(self.active, 0)
            self.assertEqual(self.acquired - before, 2)
        self.assertTrue(all(body.closed for body in self.bodies))

    async def test_cancellation_closes_the_stream_and_does_not_retry(self):
        def handler(_):
            response, body = streamed([chunk({"content": "x"})] * 50, delay=0.02)
            self.bodies.append(body)
            return response
        operation = asyncio.create_task(self.call(handler, retries=5))
        while not self.bodies:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.04)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual((self.acquired, self.released, self.active), (1, 1, 0))
        self.assertTrue(self.bodies[0].closed)

    async def test_transport_rejects_calls_outside_the_shared_controller(self):
        with self.assertRaisesRegex(RuntimeError, "execute_model"):
            await llm_client.request_llm(None)
        self.assertEqual(self.acquired, 0)

    async def test_all_agent_and_retrieval_adapters_use_both_modes(self):
        for mode in (False, True):
            def factory(**kwargs):
                return AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(success)))
            with patch.object(llm_client, "LLM_STREAM", mode), \
                 patch("agents.base_agent.AsyncOpenAI", side_effect=factory), \
                 patch("tools.docxindex.llm.client.AsyncOpenAI", side_effect=factory), \
                 patch("tools.retrieval.llamaindex.limited_llm.AsyncOpenAI", side_effect=factory):
                agent = Agent(settings=Settings(api_key="fake", model="test"))
                try:
                    self.assertEqual(await agent.chat("query"), "answer")
                finally:
                    await agent.client.close()
                self.assertEqual(LLMClient(LLMSettings(model="test")).complete("query"), "answer")
                adapter = UnifiedOpenAILike(model="test", api_key="fake", is_chat_model=True)
                self.assertEqual((await adapter.achat([ChatMessage(role="user", content="query")])).message.content,
                                 "answer")
        self.assertEqual((self.acquired, self.released, self.active), (6, 6, 0))

    async def test_legacy_completion_stream_retains_text(self):
        parts = [{"id": "test", "created": 1, "model": "test", "object": "text_completion",
                  "choices": [{"index": 0, "text": text, "finish_reason": finish, "logprobs": None}]}
                 for text, finish in (("ans", None), ("wer", "stop"))]
        async with AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: streamed(parts)[0]))) as client:
            with patch.object(llm_client, "LLM_STREAM", True):
                async def send(remaining):
                    return await llm_client.request_llm(client.completions, chat=False,
                        model="test", prompt="query", timeout=remaining)
                result = await execute_model(model_type="llm", model="test", caller="test", request={}, send=send)
        self.assertEqual(result.choices[0].text, "answer")


class LLMHTTPFixture:
    """A real HTTP server with normal, stalled, and disconnected responses."""
    def __init__(self):
        self.active = self.peak = self.count = 0
        self.modes = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                behavior = request.get("messages", [{}])[0].get("content", "good")
                mode = request["stream"]
                with fixture.lock:
                    fixture.active += 1
                    fixture.count += 1
                    fixture.peak = max(fixture.peak, fixture.active)
                    fixture.modes.append(mode)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream" if mode else "application/json")
                    if behavior == "disconnect" and not mode:
                        self.send_header("Content-Length", "100000")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if behavior in {"timeout", "disconnect"}:
                        for _ in range(100 if behavior == "timeout" else 1):
                            self.wfile.write(event(chunk({"content": "partial"})) if mode else b'{"partial":')
                            self.wfile.flush()
                            time.sleep(0.02)
                        self.connection.shutdown(socket.SHUT_RDWR)
                    else:
                        time.sleep(0.08)
                        parts = [event(chunk({"content": "answer"})), event(chunk(finish="stop")),
                                 event(chunk(usage=completion()["usage"])), event("[DONE]")] if mode else [json.dumps(completion()).encode()]
                        for part in parts:
                            self.wfile.write(part)
                            self.wfile.flush()
                except OSError:
                    pass
                finally:
                    self.close_connection = True
                    with fixture.lock:
                        fixture.active -= 1

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def llm_worker(redis_url, key, url, mode, results, barrier):
    async def run():
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(redis_url, key, 5, 30, 0.005))
        try:
            with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), \
                 patch.object(llm_client, "LLM_STREAM", mode):
                async with AsyncOpenAI(api_key="fake", base_url=url, max_retries=0,
                                       http_client=httpx.AsyncClient(trust_env=False)) as client:
                    async def one():
                        async def send(remaining):
                            return await llm_client.request_llm(client.chat.completions, model="test",
                                messages=[{"role": "user", "content": "good"}], timeout=remaining)
                        with logical_call("worker.llm", "test") as logical:
                            result = await execute_model(model_type="llm", model="test", caller="worker", request={}, send=send,
                                budget=ModelCallBudget(timeout_seconds=3, wait_timeout_seconds=10, max_retries=0))
                            return {"text": result.choices[0].message.content, "attempt": logical.payload["attempts"][0]}
                    barrier.wait(timeout=20)
                    return await asyncio.gather(*(one() for _ in range(5)))
        finally:
            await llm_semaphore.close_model_clients()
    try:
        results.put(asyncio.run(run()))
    except BaseException as exc:
        results.put(repr(exc))


@unittest.skipUnless(os.getenv("TEST_MODEL_REDIS_URL"), "Set TEST_MODEL_REDIS_URL to an isolated Redis")
class LLMTransportRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis_url = os.environ["TEST_MODEL_REDIS_URL"]
        self.redis = redis.Redis.from_url(self.redis_url)
        self.redis.ping()
        self.key = "contract-stream-test:" + uuid.uuid4().hex
        self.semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, self.key, 5, 30, 0.005))
        self.fixture = LLMHTTPFixture()

    async def asyncTearDown(self):
        await llm_semaphore.close_model_clients()
        await asyncio.to_thread(self.fixture.close)
        keys = list(self.redis.scan_iter(self.key + "*"))
        if keys:
            self.redis.delete(*keys)
        self.redis.close()

    def assert_empty(self):
        for suffix in ("", ":waiting", ":deadlines"):
            self.assertEqual(self.redis.zcard(self.key + suffix), 0)

    async def test_real_redis_releases_slots_after_timeout_and_stream_disconnect(self):
        for mode, behavior, error in ((False, "timeout", ModelCallTimeout),
                                      (True, "timeout", ModelCallTimeout),
                                      (False, "disconnect", APIConnectionError),
                                      (True, "disconnect", llm_client.IncompleteLLMStream)):
            with self.subTest(stream=mode, behavior=behavior), \
                 patch.dict(llm_semaphore._SEMAPHORES, {"llm": self.semaphore}), \
                 patch.object(llm_client, "LLM_STREAM", mode), \
                 patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
                async with AsyncOpenAI(api_key="fake", base_url=self.fixture.url, max_retries=0,
                                       http_client=httpx.AsyncClient(trust_env=False)) as client:
                    async def send(remaining, *, behavior=behavior):
                        return await llm_client.request_llm(client.chat.completions, model="test",
                            messages=[{"role": "user", "content": behavior}], timeout=remaining)
                    with logical_call("test.llm", "test") as logical:
                        with self.assertRaises(error):
                            await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                                budget=ModelCallBudget(timeout_seconds=0.18, max_retries=1))
                        self.assertEqual(logical.sent_count, 2)
                self.assert_empty()

    async def test_three_processes_share_five_slots_in_both_modes(self):
        context = multiprocessing.get_context("spawn")
        for mode in (False, True):
            results = context.Queue()
            barrier = context.Barrier(3)
            before = self.fixture.count
            processes = [context.Process(target=llm_worker, args=(self.redis_url, self.key, self.fixture.url,
                                                                  mode, results, barrier)) for _ in range(3)]
            for process in processes:
                process.start()
            try:
                for process in processes:
                    await asyncio.to_thread(process.join, 35)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
                outputs = [results.get(timeout=2) for _ in processes]
                self.assertTrue(all(isinstance(output, list) for output in outputs), outputs)
                calls = [item for output in outputs for item in output]
                self.assertEqual(len(calls), 15)
                self.assertTrue(all(item["text"] == "answer" for item in calls))
                self.assertTrue(all(item["attempt"]["stream"] == mode and
                                    item["attempt"]["transport"] == "unified_llm" for item in calls))
                self.assertEqual(self.fixture.count - before, 15)
                self.assertLessEqual(self.fixture.peak, 5)
                self.assertGreater(self.fixture.peak, 1)
                self.assert_empty()
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        await asyncio.to_thread(process.join, 5)
                results.close()
