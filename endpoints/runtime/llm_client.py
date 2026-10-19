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


async def request_llm(resource, *, chat: bool = True, **kwargs: Any):
    """Return (complete SDK response, HTTP status) inside execute_model's deadline.

    llm.stream controls the wire protocol. Callers always receive a complete
    response. An interrupted attempt is discarded before the controller retries.
    """
    record = active_model_call()
    if record is None or record.payload.get("model_type") != "llm":
        raise RuntimeError("LLM requests must run inside execute_model(model_type='llm').")
    payload = {**kwargs, "stream": LLM_STREAM}
    # Transport overrides must not bypass the deployment-wide mode.
    if isinstance(payload.get("extra_body"), dict):
        payload["extra_body"] = {k: v for k, v in payload["extra_body"].items()
                                 if k not in {"stream", "stream_options"}}
    payload.pop("stream_options", None)
    if LLM_STREAM:
        payload["stream_options"] = {"include_usage": True}
    record.payload.update(transport="unified_llm", stream=LLM_STREAM,
                          first_chunk_seconds=None, response_seconds=None,
                          stream_complete=False if LLM_STREAM else None)
    record.payload["request"]["stream"] = LLM_STREAM
    started = time.monotonic()
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
                for key, value in data.items():
                    if key != "choices":
                        metadata[key] = value
                for part in data.get("choices", []):
                    index = part["index"]
                    choice = choices.setdefault(index, {"index": index, "finish_reason": None,
                                                        **({"message": {}} if chat else {"text": ""})})
                    if chat:
                        _merge_delta(choice["message"], part.get("delta", {}))
                    else:
                        choice["text"] += part.get("text", "")
                    if part.get("finish_reason") is not None:
                        choice["finish_reason"] = part["finish_reason"]
                    if part.get("logprobs") is not None:
                        _merge_delta(choice.setdefault("logprobs", {}), part["logprobs"])
        response = _completion(choices, metadata, chat=chat)
        record.payload["stream_complete"] = True
        return response, status
    except (json.JSONDecodeError, ValidationError) as exc:
        if not LLM_STREAM:
            raise
        raise IncompleteLLMStream(f"LLM stream contained an invalid response: {exc}") from exc
    finally:
        record.payload["response_seconds"] = round(time.monotonic() - started, 6)
