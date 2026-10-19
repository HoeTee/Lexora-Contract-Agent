"""Receive every LLM response through one configurable, controlled transport."""
from __future__ import annotations

import time
import json
from typing import Any

import httpx
from openai.types import Completion
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from config import LLM_STREAM
from endpoints.runtime.model_calls import active_model_call


class IncompleteLLMStream(httpx.RemoteProtocolError):
    """The server closed its stream before every choice finished."""


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


def _completion(chunks: dict[int, dict], metadata: dict, *, chat: bool):
    if not chunks or any(choice.get("finish_reason") is None for choice in chunks.values()):
        raise IncompleteLLMStream("LLM stream ended before all choices returned a finish_reason.")
    choices = []
    for _, choice in sorted(chunks.items()):
        if chat:
            message = choice["message"]
            message.setdefault("role", "assistant")
            message.setdefault("content", None)
            if message.get("tool_calls"):
                message["tool_calls"].sort(key=lambda tool: tool["index"])
                for tool in message["tool_calls"]:
                    tool.pop("index")
        choices.append(choice)
    result = {**metadata, "choices": choices}
    if chat:
        result["object"] = "chat.completion"
        return ChatCompletion.model_validate(result)
    result["object"] = "text_completion"
    return Completion.model_validate(result)


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
    try:
        raw = await resource.with_raw_response.create(**payload)
        status = getattr(raw, "status_code", None)
        record.payload["http_status"] = status
        if not LLM_STREAM:
            return raw.parse(), status
        stream = raw.parse()
        choices: dict[int, dict] = {}
        metadata: dict[str, Any] = {}
        async with stream:
            async for chunk in stream:
                if record.payload["first_chunk_seconds"] is None:
                    record.payload["first_chunk_seconds"] = round(time.monotonic() - started, 6)
                data = chunk.model_dump(exclude_none=True)
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
                    # Usage follows all finished choices. Do not keep an
                    # otherwise complete response open waiting for a missing
                    # [DONE] event from a compatible gateway.
                    if (len(choices) >= (payload.get("n") or 1)
                            and all(choice["finish_reason"] is not None for choice in choices.values())):
                        break
        response = _completion(choices, metadata, chat=chat)
        record.payload["stream_complete"] = True
        return response, status
    except (json.JSONDecodeError, ValidationError) as exc:
        if not LLM_STREAM:
            raise
        raise IncompleteLLMStream(f"LLM stream contained an invalid response: {exc}") from exc
    finally:
        record.payload["response_seconds"] = round(time.monotonic() - started, 6)
        if LLM_STREAM:
            record.payload["max_content_gap_seconds"] = max(
                record.payload["max_content_gap_seconds"], round(time.monotonic() - last_content, 6))
