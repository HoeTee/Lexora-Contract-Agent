from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from config import LOGGING_ENABLED, LOGGING_DETAILED


_LOGGERS: dict[str, logging.Logger] = {}


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
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


def create_api_event_logger(path: Path) -> logging.Logger:
    resolved = str(path.resolve())
    logger = _LOGGERS.get(resolved)
    if logger is not None:
        return logger

    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"APIEvents.{resolved}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    _LOGGERS[resolved] = logger
    return logger


def append_api_event(path: Path, event: str, **fields: Any) -> None:
    if not LOGGING_ENABLED:
        return
    if not LOGGING_DETAILED:
        fields = {key: value for key, value in fields.items()
                  if key not in {"traceback", "messages", "prompt", "response", "tokens", "token_usage"}}
        for key in ("error", "detail", "message"):
            if key in fields:
                fields[key] = str(fields[key])[:200].replace("\n", " ").replace("\r", " ")
    logger = create_api_event_logger(path)
    parts = [event]
    parts.extend(f"{key}={_format_value(value)}" for key, value in fields.items())
    logger.log(_event_level(event), " ".join(parts))
