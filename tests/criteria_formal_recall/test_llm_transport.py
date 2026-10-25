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
from endpoints.runtime.llm_semaphore import ModelLeaseLost, ModelSemaphoreConfig, RedisModelSemaphore
from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout, TaskDeadlineExceeded, task_execution
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.model_tokens import count_model_input
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
    def __init__(self, parts, *, delay=0, error=None, tail_delay=0):
        self.parts, self.delay, self.error = parts, delay, error
        self.tail_delay = tail_delay
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield part
        if self.error:
            raise self.error
        if self.tail_delay:
            await asyncio.sleep(self.tail_delay)

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
            shutil.copytree(root / "resources" / "tokenizers", folder / "resources" / "tokenizers")
            (folder / ".env").write_text("LLM_API_KEY=fake\nEMBED_API_KEY=fake\nSESSION_SECRET_KEY=fake\n")
            for value in (False, True, "absent", None, "invalid"):
                with self.subTest(value=value):
                    if value == "absent":
                        config["llm"].pop("stream", None)
                    else:
                        config["llm"]["stream"] = value
                    (folder / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
                    result = subprocess.run([sys.executable, "-c", "import config; print('STREAM='+str(config.LLM_STREAM))"],
                                            cwd=folder, capture_output=True, text=True, timeout=15,
                                            env={**os.environ, "PYTHONPATH": str(root)})
                    if value is None or value == "invalid":
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("llm.stream must be true or false", result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("STREAM=" + str(value is True), result.stdout)


class LLMTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Tight deadline tests exercise network progress, not first tokenizer load.
        count_model_input("llm", "test", {})
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

    async def asyncSetUp(self):
        # SDK request-schema/platform initialization is unrelated to the subsecond
        # stream/task deadlines below. Warm it against an offline transport first.
        async with AsyncOpenAI(api_key="fake", base_url="https://warm.test/v1", max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(success))) as client:
            client._platform = "Windows"
            await client.chat.completions.create(model="test", messages=[{"role": "user", "content": "warm"}], stream=False)

    async def call(self, handler, *, mode=True, timeout=2, retries=0, **kwargs):
        def wrapped(request):
            self.assertEqual(self.active, 1, "HTTP must hold the shared slot")
            self.requests.append(json.loads(request.content))
            return handler(request)

        async with AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped))) as client:
            # Platform discovery may query Windows system information for >1s;
            # these subsecond tests intentionally isolate HTTP/SSE timing.
            client._platform = "Windows"
            resource = client.chat.completions
            with patch.object(llm_client, "LLM_STREAM", mode), logical_call("test.llm", "test") as logical:
                self.logical = logical
                async def send(remaining):
                    return await llm_client.request_llm(resource,
                        model="test", messages=[{"role": "user", "content": "query"}], timeout=remaining, **kwargs)
                return await execute_model(model_type="llm", model="test", caller="test",
                    request={"model": "test", "messages": [{"role": "user", "content": "query"}], **kwargs}, send=send,
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
                self.assertGreaterEqual(record["response_seconds"], 0)
                self.assertEqual(self.requests[-1]["stream"], mode)
                self.assertEqual("stream_options" in self.requests[-1], mode)
        self.assertEqual((self.acquired, self.released, self.active), (2, 2, 0))

    async def test_unlimited_request_timeout_is_sent_to_sdk_as_explicit_none(self):
        for mode in (False, True):
            observed = []
            def handler(request):
                observed.append(request.extensions["timeout"])
                return success(request)
            await self.call(handler, mode=mode, timeout=None)
            self.assertTrue(observed)
            self.assertTrue(all(value is None for value in observed[0].values()))

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
        self.assertGreater(self.logical.payload["input_tokens"], 0)
        self.assertFalse(self.logical.payload["input_usage_complete"])

    async def test_done_can_terminate_valid_content_without_inventing_a_finish_reason(self):
        response = await self.call(lambda _: streamed([chunk({"content": '{"answer": true}'}), "[DONE]"])[0])
        self.assertIsNone(response.choices[0].finish_reason)
        self.assertEqual(response.end_source, "done")
        self.assertEqual(response.choices[0].message.content, '{"answer": true}')
        attempt = self.logical.payload["attempts"][0]
        self.assertTrue(attempt["done_received"])
        self.assertEqual(attempt["end_source"], "done")

    async def test_finished_choices_do_not_require_done(self):
        response = await self.call(lambda _: streamed([chunk({"content": "answer"}), chunk(finish="stop")])[0])
        self.assertEqual(response.end_source, "finish_reason")

    async def test_done_alone_and_valid_json_without_terminal_signal_fail(self):
        for parts in (["[DONE]"], [chunk({"role": "assistant"}), "[DONE]"],
                      [chunk({"content": '{"answer": true}'})]):
            with self.subTest(parts=parts), self.assertRaises(llm_client.IncompleteLLMStream):
                await self.call(lambda _, parts=parts: streamed(parts)[0])
            self.assertEqual(self.active, 0)

    async def test_done_does_not_hide_missing_choices_or_invalid_tool_structure(self):
        incomplete = [chunk({"content": "first"}), "[DONE]"]
        with self.assertRaises(llm_client.IncompleteLLMStream):
            await self.call(lambda _: streamed(incomplete)[0], n=2)
        invalid_tool = [chunk({"tool_calls": [{"index": 0, "id": "tool", "type": "function",
                                              "function": {"arguments": "{}"}}]}), "[DONE]"]
        with self.assertRaises(llm_client.IncompleteLLMStream):
            await self.call(lambda _: streamed(invalid_tool)[0])

    async def test_error_json_is_recorded_instead_of_being_reported_as_empty_stream(self):
        with self.assertRaisesRegex(llm_client.IncompleteLLMStream, "context exceeds limit"):
            await self.call(lambda _: httpx.Response(200, json={"error": {"message": "context exceeds limit"}}))
        self.assertIn("context exceeds limit", self.logical.payload["attempts"][0]["error_response"])
        self.assertEqual(self.logical.payload["attempts"][0]["http_status"], 200)

    async def test_usage_and_partial_output_survive_unfinished_stream(self):
        parts = [chunk({"content": "partial text"}), chunk(usage=completion()["usage"])]
        with self.assertRaises(llm_client.IncompleteLLMStream):
            await self.call(lambda _: streamed(parts)[0])
        attempt = self.logical.payload["attempts"][0]
        self.assertEqual(attempt["service_usage"]["prompt_tokens"], 8)
        self.assertEqual(attempt["input_tokens"], 8)
        self.assertEqual(attempt["output_tokens"], 4)
        self.assertGreater(attempt["received_output_tokens"], 0)
        self.assertEqual(attempt["output_completeness"], "partial")
        self.assertEqual(self.logical.payload["input_tokens"], 8)

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

    async def test_total_or_content_idle_timeout_retries_with_a_new_timer(self):
        for mode in (False, True):
            before = self.acquired
            def handler(_, *, mode=mode):
                if mode:
                    response, body = streamed([chunk({"role": "assistant", "content": " "})] * 50, delay=0.02)
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
            self.assertTrue(all(attempt["local_input_tokens"] is not None for attempt in attempts))
            self.assertTrue(all(attempt["input_token_source"] == "local" for attempt in attempts))
        self.assertTrue(all(body.closed for body in self.bodies))

    async def test_stream_content_can_outlive_the_nonstreaming_total_deadline(self):
        parts = [chunk({"content": "x"})] * 8 + [chunk(finish="stop"), chunk(usage=completion()["usage"]), "[DONE]"]
        response, body = streamed(parts, delay=0.05)
        result = await self.call(lambda _: response, timeout=0.18)
        self.assertEqual(result.choices[0].message.content, "xxxxxxxx")
        attempt = self.logical.payload["attempts"][0]
        self.assertGreater(attempt["response_seconds"], 0.4)
        self.assertLess(attempt["max_content_gap_seconds"], 0.18)
        self.assertEqual(attempt["content_chars"], 8)
        self.assertEqual(attempt["timeout_kind"], "content_idle")
        self.assertEqual((self.acquired, self.released, self.active), (1, 1, 0))
        self.assertTrue(body.closed)
        body = ResponseBody([b" "] * 50, delay=0.02)
        with self.assertRaises(ModelCallTimeout):
            await self.call(lambda _: httpx.Response(200, stream=body), mode=False, timeout=0.18)
        self.assertEqual(self.logical.payload["attempts"][0]["timeout_kind"], "request_total")
        self.assertTrue(body.closed)

    async def test_reasoning_and_tool_fragments_reset_idle_timer(self):
        for delta in ({"reasoning_content": "thinking"}, {"reasoning": "thinking"},
                      {"tool_calls": [{"index": 0, "function": {"arguments": "x"}}]}):
            with self.subTest(delta=delta):
                prefix = [chunk({"tool_calls": [{"index": 0, "id": "tool", "type": "function",
                                                "function": {"name": "search"}}]})] if "tool_calls" in delta else []
                parts = prefix + [chunk(delta)] * 6 + [chunk({"content": "answer"}), chunk(finish="stop"), "[DONE]"]
                result = await self.call(lambda _, parts=parts: streamed(parts, delay=0.04)[0], timeout=0.16)
                self.assertEqual(result.choices[0].finish_reason, "stop")
                attempt = self.logical.payload["attempts"][0]
                self.assertGreater(attempt["response_seconds"], 0.3)
                self.assertGreater(attempt["reasoning_chars"] + attempt["tool_chars"], 0)

    async def test_role_metadata_heartbeats_and_empty_chunks_do_not_reset_idle_timer(self):
        for parts in ([event(chunk({"role": "assistant"}))] * 50,
                      [event(chunk({"content": ""}))] * 50,
                      [b": heartbeat\n\n"] * 50):
            with self.subTest(first=parts[0]):
                body = ResponseBody(parts, delay=0.02)
                with self.assertRaises(ModelCallTimeout):
                    await self.call(lambda _, body=body: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body), timeout=0.16)
                attempt = self.logical.payload["attempts"][0]
                self.assertIsNone(attempt["first_content_seconds"])
                self.assertEqual(attempt["content_chars"], 0)
                self.assertLess(attempt["response_seconds"], 0.5)
                self.assertTrue(body.closed)
                self.assertEqual(self.active, 0)

    async def test_content_stall_closes_discards_partial_and_retries(self):
        body = ResponseBody([event(chunk({"content": "discard me"}))], tail_delay=2)
        def handler(request):
            if self.acquired == 1:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
            return success(request)
        result = await self.call(handler, timeout=0.16, retries=1)
        self.assertEqual(result.choices[0].message.content, "answer")
        first, second = self.logical.payload["attempts"]
        self.assertEqual(first["error"]["type"], "ModelCallTimeout")
        self.assertEqual(first["content_chars"], len("discard me"))
        self.assertGreaterEqual(first["max_content_gap_seconds"], 0.15)
        self.assertGreater(second["remaining_seconds"], 0.14)
        self.assertTrue(body.closed)
        self.assertEqual((self.acquired, self.released, self.active), (2, 2, 0))

    async def test_task_deadline_still_stops_a_continuously_progressing_stream(self):
        response, body = streamed([chunk({"content": "x"})] * 100, delay=0.03)
        with task_execution(time.time() + 1.0):
            with self.assertRaises(TaskDeadlineExceeded):
                await self.call(lambda _: response, timeout=0.5, retries=5)
        self.assertTrue(body.closed)
        self.assertEqual((self.acquired, self.released, self.active), (1, 1, 0))

    async def test_finished_choices_and_usage_do_not_wait_for_missing_done(self):
        response, body = streamed([chunk({"content": "answer"}), chunk(finish="stop"),
                                   chunk(usage=completion()["usage"])], tail_delay=2)
        result = await self.call(lambda _: response, timeout=0.16)
        self.assertEqual(result.choices[0].message.content, "answer")
        self.assertEqual(result.usage.total_tokens, 12)
        self.assertTrue(body.closed)

    async def test_finished_choices_do_not_wait_for_a_silent_open_connection(self):
        response, body = streamed([chunk({"content": "answer"}), chunk(finish="stop")], tail_delay=2)
        result = await self.call(lambda _: response, timeout=0.16)
        self.assertEqual(result.choices[0].message.content, "answer")
        self.assertEqual(result.end_source, "finish_reason")
        self.assertIsNone(result.usage)
        self.assertLess(self.logical.budget.request_elapsed_seconds, 0.16)
        self.assertLess(self.logical.payload["attempts"][0]["response_seconds"], 0.6)
        self.assertTrue(body.closed)

    async def test_optional_usage_trailer_does_not_extend_contract_deadline(self):
        response, body = streamed([chunk({"content": "answer"}), chunk(finish="stop")], tail_delay=2)
        async def handler(_):
            # The task expires after the final choice, during optional drainage.
            return response
        with patch.object(llm_client, "_TRAILER_WAIT_SECONDS", 0.5):
            with task_execution(time.time() + 0.25):
                with self.assertRaises(TaskDeadlineExceeded):
                    await self.call(handler, timeout=1)
        self.assertTrue(body.closed)

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
                 patch("endpoints.runtime.llm_client.AsyncOpenAI", side_effect=factory):
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
                            self.wfile.write(event(chunk({"role": "assistant"})) if mode else b'{"partial":')
                            self.wfile.flush()
                            time.sleep(0.02)
                        self.connection.shutdown(socket.SHUT_RDWR)
                    else:
                        time.sleep(0.08)
                        if behavior == "long-progress" and mode:
                            for _ in range(16):
                                self.wfile.write(event(chunk({"content": "x"})))
                                self.wfile.flush()
                                time.sleep(0.1)
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


def llm_worker(redis_url, key, url, mode, results, barrier, lease_seconds=30, behavior="good"):
    async def run():
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(redis_url, key, 5, lease_seconds, 0.005))
        try:
            with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), \
                 patch.object(llm_client, "LLM_STREAM", mode):
                async with AsyncOpenAI(api_key="fake", base_url=url, max_retries=0,
                                       http_client=httpx.AsyncClient(trust_env=False)) as client:
                    client._platform = "Windows"
                    resource = client.chat.completions
                    request = {"model": "test", "messages": [{"role": "user", "content": behavior}]}
                    count_model_input("llm", "test", request)
                    async def one():
                        async def send(remaining):
                            return await llm_client.request_llm(resource, **request, timeout=remaining)
                        with logical_call("worker.llm", "test") as logical:
                            result = await execute_model(model_type="llm", model="test", caller="worker", request=request, send=send,
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

    async def test_real_http_content_progress_outlives_initial_timeout_and_renews(self):
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, self.key, 5, 0.9, 0.005))
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), patch.object(llm_client, "LLM_STREAM", True):
            async with AsyncOpenAI(api_key="fake", base_url=self.fixture.url, max_retries=0,
                    http_client=httpx.AsyncClient(trust_env=False)) as client:
                async def send(remaining):
                    return await llm_client.request_llm(client.chat.completions, model="test",
                        messages=[{"role": "user", "content": "long-progress"}], timeout=remaining)
                with logical_call("test.llm", "test") as logical:
                    result = await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                        budget=ModelCallBudget(timeout_seconds=0.5, max_retries=0))
                    attempt = logical.payload["attempts"][0]
        self.assertEqual(result.choices[0].message.content, "x" * 16 + "answer")
        self.assertGreater(attempt["response_seconds"], 1.5)
        self.assertLess(attempt["max_content_gap_seconds"], 0.5)
        self.assertGreater(attempt["lease_renewals"], 0)
        await semaphore._async_client().aclose()
        self.assert_empty()

    async def test_long_request_renews_and_waiter_cannot_take_live_owner(self):
        # Leave Redis enough scheduling time while the Docker build is active;
        # the owner still spans three whole leases and must renew repeatedly.
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, self.key, 1, 0.9, 0.005))
        entered = asyncio.Event()
        async def hold():
            async with llm_semaphore.model_semaphore("llm", "owner", "test") as lease:
                entered.set()
                await asyncio.sleep(2.7)
                return lease.renewals
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}):
            operation = asyncio.create_task(hold())
            await entered.wait()
            await asyncio.sleep(1.35)
            self.assertEqual(await asyncio.to_thread(self.redis.zcard, self.key), 1)
            with self.assertRaises(llm_semaphore.ModelSemaphoreTimeout):
                await semaphore.acquire_async(caller_name="waiter", model="test",
                    budget=ModelCallBudget(wait_timeout_seconds=0.15))
            self.assertGreaterEqual(await operation, 2)
        await semaphore._async_client().aclose()
        self.assert_empty()

    async def test_missing_owner_is_not_recreated_and_stops_the_request(self):
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, self.key, 1, 0.3, 0.005))
        response, body = streamed([chunk({"content": "x"})] * 100, delay=0.02)
        def handler(_):
            self.redis.delete(self.key)
            return response
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), patch.object(llm_client, "LLM_STREAM", True):
            async with AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
                async def send(remaining):
                    return await llm_client.request_llm(client.chat.completions, model="test",
                        messages=[{"role": "user", "content": "test"}], timeout=remaining)
                with self.assertRaises(ModelLeaseLost):
                    await execute_model(model_type="llm", model="test", caller="test", request={}, send=send,
                        budget=ModelCallBudget(timeout_seconds=1, max_retries=5))
        self.assertTrue(body.closed)
        await semaphore._async_client().aclose()
        self.assert_empty()

    async def test_redis_renewal_error_cancels_request_and_releases_owner(self):
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, self.key, 1, 0.3, 0.005))
        client = semaphore._async_client()
        original_eval = client.eval
        async def fail_renew(script, *args):
            if script == semaphore._RENEW_SCRIPT:
                raise redis.RedisError("renewal unavailable")
            return await original_eval(script, *args)
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), patch.object(client, "eval", fail_renew):
            with self.assertRaises(ModelLeaseLost):
                async with llm_semaphore.model_semaphore("llm", "owner", "test"):
                    await asyncio.Event().wait()
        await client.aclose()
        self.assert_empty()

    async def test_renewal_error_survives_release_error_and_slot_expires(self):
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, self.key, 1, 0.3, 0.005))
        client = semaphore._async_client()
        original_eval = client.eval
        async def fail_renew(script, *args):
            if script == semaphore._RENEW_SCRIPT:
                raise redis.RedisError("renewal unavailable")
            return await original_eval(script, *args)
        async def fail_remove(*args, **kwargs):
            raise redis.RedisError("release unavailable")
        with patch.dict(llm_semaphore._SEMAPHORES, {"llm": semaphore}), \
                patch.object(client, "eval", fail_renew), patch.object(semaphore, "_remove", fail_remove), \
                self.assertLogs("endpoints.runtime.model_limits", level="ERROR"):
            with self.assertRaises(ModelLeaseLost):
                async with llm_semaphore.model_semaphore("llm", "owner", "test"):
                    await asyncio.Event().wait()
        self.assertEqual(self.redis.zcard(self.key), 1)
        await asyncio.sleep(0.3)
        next_lease = await semaphore.acquire_async(caller_name="replacement", model="test", budget=ModelCallBudget())
        self.assertEqual(self.redis.zcard(self.key), 1)
        await semaphore.release_async(next_lease)
        await client.aclose()
        self.assert_empty()

    async def test_expired_owner_cannot_be_renewed_or_recreated(self):
        semaphore = RedisModelSemaphore("llm", ModelSemaphoreConfig(self.redis_url, self.key, 1, 0.15, 0.005))
        lease = await semaphore.acquire_async(caller_name="expired", model="test", budget=ModelCallBudget())
        score = self.redis.zscore(self.key, lease.member)
        await asyncio.sleep(0.2)
        with self.assertRaises(ModelLeaseLost):
            await semaphore.keep_alive(lease)
        self.assertEqual(self.redis.zscore(self.key, lease.member), score)
        await semaphore.release_async(lease)
        await semaphore._async_client().aclose()
        self.assert_empty()

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
        for mode, lease_seconds, behavior in ((False, 30, "good"), (True, 30, "good"),
                                              (True, 3, "long-progress")):
            results = context.Queue()
            barrier = context.Barrier(3)
            before = self.fixture.count
            processes = [context.Process(target=llm_worker, args=(self.redis_url, self.key, self.fixture.url,
                                                                  mode, results, barrier, lease_seconds, behavior)) for _ in range(3)]
            for process in processes:
                process.start()
            try:
                # Drain Queue pipes before joining: compact attempts can exceed
                # pipe capacity and multiprocessing's feeder waits for a reader.
                outputs = [await asyncio.to_thread(results.get, True, 60) for _ in processes]
                for process in processes:
                    await asyncio.to_thread(process.join, 35)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
                self.assertTrue(all(isinstance(output, list) for output in outputs), outputs)
                calls = [item for output in outputs for item in output]
                self.assertEqual(len(calls), 15)
                expected = "x" * 16 + "answer" if behavior == "long-progress" else "answer"
                self.assertTrue(all(item["text"] == expected for item in calls))
                self.assertTrue(all(item["attempt"]["stream"] == mode and
                                    item["attempt"]["transport"] == "unified_llm" for item in calls))
                self.assertEqual(self.fixture.count - before, 15)
                self.assertLessEqual(self.fixture.peak, 5)
                self.assertGreater(self.fixture.peak, 1)
                if behavior == "long-progress":
                    self.assertTrue(all(item["attempt"]["lease_renewals"] > 0 for item in calls))
                self.assert_empty()
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        await asyncio.to_thread(process.join, 5)
                results.close()
                # Profiles change the lease policy. Their sent RPM events must
                # not carry that old policy into the next isolated test profile.
                keys = list(self.redis.scan_iter(self.key + "*"))
                if keys:
                    self.redis.delete(*keys)
