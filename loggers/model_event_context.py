from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from config import LOGGING_DETAILED

from loggers.api_event_logger import append_api_event


_model_event_path: ContextVar[Path | None] = ContextVar(
    "model_event_path",
    default=None,
)


def set_model_event_path(path: str | Path | None):
    resolved_path = Path(path) if path else None
    return _model_event_path.set(resolved_path)


def reset_model_event_path(token) -> None:
    _model_event_path.reset(token)


def append_model_event(event: str, **fields: Any) -> None:
    path = _model_event_path.get()
    if path is None or (not LOGGING_DETAILED and event != "docxindex_ablation"):
        return
    append_api_event(path, event, **fields)


def safe_endpoint(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return parsed.path
    return f"{parsed.netloc}{parsed.path}"
