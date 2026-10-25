"""Receive every LLM response through one configurable, controlled transport."""
from __future__ import annotations

import asyncio
import time
import json
import math
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI
from openai.types import Completion
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion import Choice
from openai.types.completion_choice import CompletionChoice
from openai._streaming import SSEDecoder
from pydantic import ValidationError

from config import LLM_STREAM
from endpoints.runtime.model_calls import active_model_call
from endpoints.runtime.model_tokens import count_model_output


class IncompleteLLMStream(httpx.RemoteProtocolError):
    """The server closed its stream before every choice finished."""


# Usage is optional trailing metadata, not another completion requirement.
_TRAILER_WAIT_SECONDS = 0.1


class StreamChoice(Choice):
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "function_call"] | None = None


class StreamChatCompletion(ChatCompletion):
    choices: list[StreamChoice]
    end_source: Literal["finish_reason", "done"]


class StreamCompletionChoice(CompletionChoice):
    finish_reason: Literal["stop", "length", "content_filter"] | None = None


class StreamCompletion(Completion):
    choices: list[StreamCompletionChoice]
    end_source: Literal["finish_reason", "done"]


def _merge_delta(target: dict, delta: dict) -> None:
    """Accumulate text and indexed tool calls without parsing partial arguments."""
    for key, value in delta.items():
        if value is None:
            continue
        if key not in target or target[key] is None:
            target[key] = value
        elif isinstance(value, dict):
            _merge_delta(target[key], value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "index" in item:
                    previous = next((part for part in target[key] if part.get("index") == item["index"]), None)
                    if previous is None:
                        target[key].append(item)
                    else:
                        _merge_delta(previous, item)
                else:
                    target[key].append(item)
        elif isinstance(value, str) and key not in {"role", "type"}:
            target[key] += value
        else:
            target[key] = value


def _completion(chunks: dict[int, dict], metadata: dict, *, chat: bool,
                done: bool = False, expected_choices: int = 1):
    finished = bool(chunks) and all(choice.get("finish_reason") is not None for choice in chunks.values())
    if set(chunks) != set(range(expected_choices)) or not (finished or done):
        raise IncompleteLLMStream("LLM stream ended without complete choices and an explicit finish_reason or [DONE].")
    choices = []
    for _, choice in sorted(chunks.items()):
        if chat:
            message = choice["message"]
            if not (str(message.get("content") or "").strip() or message.get("tool_calls") or message.get("function_call")
                    or str(message.get("refusal") or "").strip()):
                raise IncompleteLLMStream("LLM stream ended without usable response content or a tool call.")
            message.setdefault("role", "assistant")
            message.setdefault("content", None)
            if message.get("tool_calls"):
                message["tool_calls"].sort(key=lambda tool: tool["index"])
                for tool in message["tool_calls"]:
                    tool.pop("index")
        elif not choice.get("text"):
            raise IncompleteLLMStream("LLM stream ended without usable response text.")
        choices.append(choice)
    result = {**metadata, "choices": choices, "end_source": "finish_reason" if finished else "done"}
    if chat:
        result["object"] = "chat.completion"
        return StreamChatCompletion.model_validate(result)
    result["object"] = "text_completion"
    return StreamCompletion.model_validate(result)


def _content_lengths(part: dict, *, chat: bool) -> tuple[int, int, int]:
    """Count new nonblank text, reasoning, and tool fragments, not metadata."""
    delta = part.get("delta", {}) if chat else {"content": part.get("text")}
    def length(value):
        return len(value) if isinstance(value, str) and value.strip() else 0
    tools = delta.get("tool_calls") or []
    legacy = delta.get("function_call") or {}
    tool_chars = sum(length((tool.get("function") or {}).get(field))
                     for tool in tools for field in ("name", "arguments"))
    tool_chars += sum(length(legacy.get(field)) for field in ("name", "arguments"))
    return (length(delta.get("content")),
            length(delta.get("reasoning_content")) + length(delta.get("reasoning")), tool_chars)


async def send_llm_request(*, api_key: str, base_url: str, request: dict[str, Any],
                           remaining: float | None, endpoint: str = "chat",
                           default_headers: dict[str, str] | None = None):
    """Send one controlled SDK request and close its client on every exit path."""
    if endpoint not in {"chat", "completion"}:
        raise ValueError(f"Unsupported LLM endpoint: {endpoint}")
    timeout = None if remaining is None or math.isinf(remaining) else remaining
    async with AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                           timeout=timeout, default_headers=default_headers) as client:
        resource = client.chat.completions if endpoint == "chat" else client.completions
        return await request_llm(resource, chat=endpoint == "chat", **{**request, "timeout": timeout})


async def request_llm(resource, *, chat: bool = True, **kwargs: Any):
    """Return (complete SDK response, HTTP status) inside execute_model's deadline.

    llm.stream controls the wire protocol. Callers always receive a complete
    response. An interrupted attempt is discarded before the controller retries.
    """
    active = active_model_call()
    if active is None or active.record.payload.get("model_type") != "llm":
        raise RuntimeError("LLM requests must run inside execute_model(model_type='llm').")
    record = active.record
    payload = {**kwargs, "stream": LLM_STREAM}
    if isinstance(payload.get("timeout"), (int, float)) and not math.isfinite(payload["timeout"]):
        payload["timeout"] = None
    # Transport overrides must not bypass the deployment-wide mode.
    if isinstance(payload.get("extra_body"), dict):
        payload["extra_body"] = {k: v for k, v in payload["extra_body"].items()
                                 if k not in {"stream", "stream_options"}}
    payload.pop("stream_options", None)
    if LLM_STREAM:
        payload["stream_options"] = {"include_usage": True}
        # Socket reads must not inherit the initial total/task remaining time.
        # The controller's rescheduled timer enforces content progress and the
        # enclosing contract deadline, including heartbeats and empty chunks.
        payload["timeout"] = httpx.Timeout(active.budget.timeout_seconds)
        active.budget.streaming = True
    record.payload.update(transport="unified_llm", stream=LLM_STREAM,
                          first_chunk_seconds=None, response_seconds=None,
                          stream_complete=False if LLM_STREAM else None,
                          timeout_kind="content_idle" if LLM_STREAM else "request_total")
    record.payload["request"]["stream"] = LLM_STREAM
    started = time.monotonic()
    last_content = started
    if LLM_STREAM:
        record.payload.update(stream_chunk_count=0, content_chars=0, reasoning_chars=0, tool_chars=0,
                              first_content_seconds=None, last_content_seconds=None,
                              max_content_gap_seconds=0.0, finish_reasons={}, usage_received=False)
    raw = None
    received_response = None
    choices: dict[int, dict] = {}
    metadata: dict[str, Any] = {}
    done = False
    try:
        raw = await resource.with_raw_response.create(**payload)
        status = getattr(raw, "status_code", None)
        record.payload["http_status"] = status
        if not LLM_STREAM:
            received_response = raw.parse()
            record.record_service_usage(getattr(received_response, "usage", None))
            return received_response, status
        response_http = raw.http_response
        content_type = response_http.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "text/event-stream":
            body = await response_http.aread()
            record.payload["error_response"] = body.decode("utf-8", errors="replace")
            raise IncompleteLLMStream(
                f"Expected text/event-stream, received {content_type or 'missing Content-Type'}: "
                f"{record.payload['error_response'][:1000]}")
        # Consume the SDK's SSE decoder directly: AsyncStream consumes [DONE]
        # internally and cannot distinguish an explicit terminal event from EOF.
        decoder = SSEDecoder()
        events = decoder.aiter_bytes(response_http.aiter_bytes())
        trailer_deadline = None
        while True:
            try:
                if trailer_deadline is None:
                    event = await anext(events)
                else:
                    trailer_remaining = max(0, trailer_deadline - time.monotonic())
                    async with asyncio.timeout(min(trailer_remaining, active.budget.task_remaining())):
                        event = await anext(events)
            except StopAsyncIteration:
                break
            except TimeoutError:
                if trailer_deadline is None:
                    raise
                active.budget.task_remaining()
                break
            if event.data.strip() == "[DONE]":
                done = True
                record.payload["done_received"] = True
                break
            if not event.data:
                continue
            if record.payload["first_chunk_seconds"] is None:
                record.payload["first_chunk_seconds"] = round(time.monotonic() - started, 6)
            data = json.loads(event.data)
            if not isinstance(data, dict):
                raise IncompleteLLMStream("LLM stream event must contain a JSON object.")
            if event.event == "error" or data.get("error"):
                record.payload["error_response"] = data
                raise IncompleteLLMStream(f"LLM stream reported an error: {data}")
            if chat:
                data = ChatCompletionChunk.model_validate(data).model_dump(exclude_none=True)
            record.payload["stream_chunk_count"] += 1
            progress = False
            for key, value in data.items():
                if key != "choices":
                    metadata[key] = value
            for part in data.get("choices", []):
                lengths = _content_lengths(part, chat=chat)
                for field, count in zip(("content_chars", "reasoning_chars", "tool_chars"), lengths, strict=True):
                    record.payload[field] += count
                progress = progress or any(lengths)
                index = part["index"]
                choice = choices.setdefault(index, {"index": index, "finish_reason": None,
                                                    **({"message": {}} if chat else {"text": ""})})
                if chat:
                    _merge_delta(choice["message"], part.get("delta", {}))
                else:
                    choice["text"] += part.get("text", "")
                if part.get("finish_reason") is not None:
                    choice["finish_reason"] = part["finish_reason"]
                    record.payload["finish_reasons"][str(index)] = part["finish_reason"]
                if part.get("logprobs") is not None:
                    _merge_delta(choice.setdefault("logprobs", {}), part["logprobs"])
            if progress:
                active.received_content()
                now = time.monotonic()
                record.payload["max_content_gap_seconds"] = max(
                    record.payload["max_content_gap_seconds"], round(now - last_content, 6))
                last_content = now
                elapsed = round(now - started, 6)
                if record.payload["first_content_seconds"] is None:
                    record.payload["first_content_seconds"] = elapsed
                record.payload["last_content_seconds"] = elapsed
            if data.get("usage") is not None:
                record.payload["usage_received"] = True
                record.record_service_usage(data["usage"])
                # Usage follows all finished choices. Do not keep an
                # otherwise complete response open waiting for a missing
                # [DONE] event from a compatible gateway.
                if (len(choices) >= (payload.get("n") or 1)
                        and all(choice["finish_reason"] is not None for choice in choices.values())):
                    break
            if (trailer_deadline is None and len(choices) >= (payload.get("n") or 1)
                    and all(choice["finish_reason"] is not None for choice in choices.values())):
                # The model has declared completion. Freeze its idle deadline
                # now; optional usage drainage remains bounded by the contract.
                active.budget.remaining()
                active.budget.finish_request()
                record.payload["stream_finished_seconds"] = round(time.monotonic() - started, 6)
                task_remaining = active.budget.task_remaining()
                active.timer.reschedule(asyncio.get_running_loop().time() + task_remaining
                                        if math.isfinite(task_remaining) else None)
                trailer_deadline = time.monotonic() + _TRAILER_WAIT_SECONDS
        response = _completion(choices, metadata, chat=chat, done=done,
                               expected_choices=payload.get("n") or 1)
        record.payload["end_source"] = response.end_source
        record.payload["stream_complete"] = True
        return response, status
    except BaseException as exc:
        error_response = getattr(exc, "response", None)
        if error_response is not None:
            record.payload["http_status"] = getattr(error_response, "status_code", None)
        error_body = getattr(exc, "body", None)
        if error_body is not None:
            record.payload["error_response"] = error_body
        if isinstance(exc, (json.JSONDecodeError, ValidationError)) and LLM_STREAM:
            raise IncompleteLLMStream(f"LLM stream contained an invalid response: {exc}") from exc
        raise
    finally:
        if raw is not None and LLM_STREAM:
            await raw.http_response.aclose()
        accumulated = choices if LLM_STREAM else received_response
        if accumulated:
            record.record_received_output(
                count_model_output(payload.get("model", record.payload["model"]), accumulated),
                complete=bool(record.payload.get("stream_complete")) if LLM_STREAM else True,
            )
        if metadata.get("usage") is not None:
            record.record_service_usage(metadata["usage"])
        record.payload["response_seconds"] = round(time.monotonic() - started, 6)
        if LLM_STREAM:
            record.payload["max_content_gap_seconds"] = max(
                record.payload["max_content_gap_seconds"], round(time.monotonic() - last_content, 6))
