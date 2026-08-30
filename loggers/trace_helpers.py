from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _safe_text(value: Any, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated {len(text) - limit} chars]"


def _messages_size(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(message.get("content", ""))) for message in messages)


def error_info(exc: BaseException) -> dict[str, Any]:
    text = str(exc)
    info: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": text,
    }
    http_status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    if http_status is not None:
        info["http_status"] = http_status
    component = getattr(exc, "component", None)
    if component:
        info["component"] = component
    code = getattr(exc, "event_type", None) or _extract_error_code(text)
    if code:
        info["code"] = code
    return info


def _extract_error_code(text: str) -> str | None:
    match = re.search(r"['\"]code['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
    if match:
        return match.group(1)
    return None


def llm_inputs(
    *,
    agent_name: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    step: int,
) -> dict[str, Any]:
    return {
        "agent": agent_name,
        "model": model,
        "step": step,
        "message_count": len(messages),
        "input_chars": _messages_size(messages),
        "tool_count": len(tools or []),
        "tool_names": [
            tool.get("function", {}).get("name")
            for tool in tools or []
            if tool.get("function", {}).get("name")
        ],
    }


def llm_outputs(completion: Any) -> dict[str, Any]:
    choice = completion.choices[0] if getattr(completion, "choices", None) else None
    message = choice.message if choice else None
    usage = getattr(completion, "usage", None)
    return {
        "finish_reason": getattr(choice, "finish_reason", None),
        "output_chars": len(str(getattr(message, "content", "") or "")),
        "tool_call_count": len(getattr(message, "tool_calls", None) or []),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def mcp_inputs(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "tool": tool_name,
        "arg_keys": sorted(args.keys()),
    }
    for key in (
        "file_path",
        "docx_path",
        "contract_path",
        "output_path",
        "output_dir",
        "annotation_events_path",
    ):
        if key in args and args[key]:
            summary[key] = str(args[key])
            if key.endswith("path") or key.endswith("dir"):
                summary[f"{key}_name"] = Path(str(args[key])).name
    if "query" in args:
        summary["query_chars"] = len(str(args["query"]))
        summary["query_preview"] = _safe_text(args["query"], 200)
    if "results_json" in args:
        summary["results_json_chars"] = len(str(args["results_json"]))
    if "summary_sections_json" in args:
        summary["summary_sections_json_chars"] = len(str(args["summary_sections_json"]))
    return summary


def mcp_outputs(result: Any) -> dict[str, Any]:
    text = str(result)
    return {
        "output_chars": len(text),
        "output_preview": _safe_text(text, 500),
    }


def criterion_inputs(criterion: dict[str, Any]) -> dict[str, Any]:
    check_points = criterion.get("check_points") or []
    return {
        "criterion_id": criterion.get("id"),
        "section": criterion.get("section"),
        "criterion_chars": len(str(criterion.get("criterion", ""))),
        "criterion_preview": _safe_text(criterion.get("criterion", ""), 300),
        "check_point_count": len(check_points),
    }


def criterion_outputs(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "criterion_id": result.get("criterion_id"),
        "status": result.get("status"),
        "issue_count": len(result.get("issues") or []),
        "tokens": result.get("tokens", 0),
        "error_message": result.get("error_message"),
        "anchor_validation": result.get("anchor_validation", {}),
    }
