from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from config import LOGGING_ENABLED, LOGGING_DETAILED
from loggers.task_log_io import append_task_log_line


_LOGGERS: dict[str, logging.Logger] = {}
_LOGGERS_LOCK = threading.Lock()
_LOCATION_FIELDS = ("criterion_id", "source", "stage")
_PRIVATE_FIELDS = {"traceback", "messages", "prompt", "response", "request", "content", "tokens", "token_usage"}
_STRUCTURED_FIELDS = {"summary", "annotation_stats", "token_stats"}
_SECRET = re.compile(
    r"(?i)(bearer\s+)[^\s,;\"']+|"
    r"([\"']?(?:api[_-]?key|authorization|password|secret|(?:access|refresh)[_-]?token)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)|"
    r"\bsk-[A-Za-z0-9_-]{8,}"
)
_STRUCTURED_ERROR = re.compile(r"\{\s*[\"']|\[\s*(?:\{|[\"'])")


def _credential_field(key: str) -> bool:
    return key.lower().replace("-", "_") in {"access_token", "refresh_token", "accesstoken", "refreshtoken"}


def _one_line(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    text = _SECRET.sub(lambda match: (match.group(1) or match.group(2) or "") + "[redacted]", text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = _one_line(value)
    if any(ch.isspace() for ch in text) or "=" in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _event_level(event: str) -> int:
    normalized = event.lower()
    if any(marker in normalized for marker in ("failed", "error", "interrupted")):
        return logging.ERROR
    if any(marker in normalized for marker in ("retry", "cancelled", "queued")):
        return logging.WARNING
    return logging.INFO


def _safe_error_text(value: str) -> str:
    payload = _STRUCTURED_ERROR.search(value)
    if payload is None:
        return _one_line(value)
    prefix = value[:payload.start()]
    error_type = re.search(r"\b[A-Za-z_]\w*(?:Error|Exception)\b", prefix)
    status = re.search(r"\b[45][0-9]{2}\b", prefix)
    details = [error_type.group()] if error_type else []
    if status:
        details.append("HTTP " + status.group())
    details.append("Response body omitted.")
    return " ".join(details)


def _safe_structure(value: Any, *, depth: int = 0) -> Any:
    """Keep small status/count metadata, never request or credential payloads."""
    if depth >= 3:
        return None
    if isinstance(value, dict):
        return {
            key: _safe_error_text(item) if key in {"error", "detail", "error_message"} and isinstance(item, str)
            else _safe_structure(item, depth=depth + 1)
            for key, item in value.items()
            if not _credential_field(str(key))
            if not any(part in str(key).lower() for part in ("key", "secret", "password", "authorization", "prompt", "message", "traceback", "request", "response", "content"))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_structure(item, depth=depth + 1) for item in value[:8]]
    return _one_line(value, limit=120) if isinstance(value, str) else value


def _compact_metadata(key: str, value: Any) -> Any:
    if not isinstance(value, dict):
        return _safe_structure(value)
    if key == "token_stats":
        compact = {name: value[name] for name in ("input", "output", "total", "sent_attempts") if name in value}
        compact.update({name: item for name, item in value.items()
                        if name.endswith("_complete") and item is False})
        if value.get("total_is_lower_bound") is True:
            compact["total_is_lower_bound"] = True
        return compact
    if key == "summary":
        error = value.get("error")
        error_type = error.get("type", "Error") if isinstance(error, dict) else type(error).__name__
        return {"status": _one_line(value.get("status", "unknown"), limit=40),
                "error": {"type": _one_line(error_type, limit=80)} if error is not None else None}
    if key == "annotation_stats":
        counts = value.get("annotation_stats", value.get("stats", value))
        compact = {name: item for name, item in counts.items()
                   if isinstance(item, int) and not isinstance(item, bool)} if isinstance(counts, dict) else {}
        skipped = value.get("annotation_skipped_count")
        if isinstance(skipped, int) and not isinstance(skipped, bool):
            compact["annotation_skipped_count"] = skipped
        return compact
    return _safe_structure(value)


def _generic_summary(event: str, fields: dict[str, Any]) -> str:
    parts = [_one_line(event, limit=100)]
    for key, value in fields.items():
        if key in _PRIVATE_FIELDS or key in {"logical_no", "category", *_LOCATION_FIELDS}:
            continue
        if _credential_field(key):
            continue
        if any(marker in key.lower() for marker in ("key", "secret", "password", "authorization", "tokenizer")):
            continue
        if key in {"file_path", "result_file", "source_url", "url", "llm", "embedding", "reranker"}:
            continue
        if isinstance(value, (dict, list, tuple)):
            if key not in _STRUCTURED_FIELDS:
                continue
            value = _compact_metadata(key, value)
        elif key in {"error", "detail", "error_message", "message"} and isinstance(value, str):
            value = _safe_error_text(value)
        parts.append(f"{key}={_format_value(value)}")
    return " ".join(parts)


class _TaskEventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "event_fields", {})
        logical_no = fields.get("logical_no") or "-"
        category = _one_line(fields.get("category") or "-", limit=100)
        location = "/".join(_one_line(fields.get(key) or "-", limit=100).replace(" ", "_")
                            for key in _LOCATION_FIELDS)
        timestamp = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")
        return f"{logical_no} {timestamp} {record.levelname:<7} {category} {location} | {record.getMessage()}"


class _TaskEventHandler(logging.Handler):
    def __init__(self, path: Path):
        super().__init__(logging.DEBUG)
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            append_task_log_line(self.path, self.format(record))
        except Exception:
            self.handleError(record)


def create_api_event_logger(path: Path) -> logging.Logger:
    path = Path(path)
    resolved = str(path.resolve())
    with _LOGGERS_LOCK:
        logger = _LOGGERS.get(resolved)
        if logger is not None:
            return logger
        path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"APIEvents.{resolved}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers.clear()
        handler = _TaskEventHandler(path)
        handler.setFormatter(_TaskEventFormatter())
        logger.addHandler(handler)
        _LOGGERS[resolved] = logger
        return logger


def append_api_event(path: Path, event: str, *, summary: str | dict | None = None,
                     level: int | str | None = None, **fields: Any) -> None:
    if not LOGGING_ENABLED:
        return
    # Existing workflow callers use summary for structured final-task metadata.
    if summary is not None and not isinstance(summary, str):
        fields["summary"] = summary
        summary = None
    body = _one_line(summary, limit=600) if summary is not None else _generic_summary(event, fields)
    event_level = _event_level(event) if level is None else level
    if isinstance(event_level, str):
        event_level = logging.getLevelNamesMapping()[event_level.upper()]
    logger = create_api_event_logger(path)
    logger.log(event_level, body, extra={"event_type": event, "event_fields": fields})
