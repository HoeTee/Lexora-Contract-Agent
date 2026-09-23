"""Durable, task-scoped records of outbound model requests."""

from __future__ import annotations

import json
import uuid
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import LOGGING_ENABLED


_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("model_call_context", default=None)


def set_call_context(events_path: str | Path | None, *, operation_id: str | None = None,
                     criterion_id: str | None = None, stage: str = "indexing"):
    path = Path(events_path).parent / "calls" if events_path and LOGGING_ENABLED else None
    return _CONTEXT.set({"directory": path, "operation_id": operation_id,
                         "criterion_id": criterion_id, "stage": stage})


def reset_call_context(token) -> None:
    _CONTEXT.reset(token)


def set_call_stage(stage: str):
    context = _CONTEXT.get()
    return _CONTEXT.set({**context, "stage": stage} if context else None)


def reset_call_stage(token) -> None:
    _CONTEXT.reset(token)


def map_with_context(executor, function, items):
    """Carry immutable task correlation into each executor job."""
    futures = [executor.submit(copy_context().run, function, item) for item in items]
    return [future.result() for future in futures]


def new_call(*, component: str, model: str, request: dict[str, Any],
             directory: Path | None = None, criterion_id: str | None = None,
             stage: str | None = None) -> "CallRecord":
    context = _CONTEXT.get() or {}
    return CallRecord(
        directory=(directory if directory is not None else context.get("directory")) if LOGGING_ENABLED else None,
        component=component, model=model, request=request,
        criterion_id=criterion_id if criterion_id is not None else context.get("criterion_id"),
        stage=stage if stage is not None else context.get("stage"),
        operation_id=context.get("operation_id"),
    )


def append_route_event(event: str, payload: dict[str, Any]) -> None:
    context = _CONTEXT.get() or {}
    directory = context.get("directory")
    if directory is None:
        return
    path = directory.parent / "route_events" / f"{uuid.uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"operation_id": context.get("operation_id"),
                                "criterion_id": context.get("criterion_id"),
                                "stage": context.get("stage"), "event": event,
                                "at": _now(), "payload": payload},
                               ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class CallRecord:
    def __init__(self, *, directory: Path | None, component: str, model: str,
                 request: dict[str, Any], criterion_id: str | None, stage: str | None,
                 operation_id: str | None) -> None:
        self.call_id = uuid.uuid4().hex
        self.path = directory / f"{self.call_id}.json" if directory else None
        self.payload: dict[str, Any] = {
            "call_id": self.call_id, "operation_id": operation_id,
            "criterion_id": criterion_id, "component": component, "stage": stage,
            "model": model, "status": "pending", "started_at": _now(),
            "request": request, "response": None, "error": None,
        }
        self._write()  # A failed write must prevent the outbound request.

    def succeeded(self, response: Any, *, http_status: int | None = 200) -> None:
        self.payload.update(status="succeeded", finished_at=_now(), response=response,
                            http_status=http_status)
        self._write()

    def failed(self, exc: BaseException, *, stage: str = "request") -> None:
        self.payload.update(
            status="failed", finished_at=_now(), failure_stage=stage,
            error={"type": type(exc).__name__, "message": str(exc),
                   "http_status": getattr(exc, "http_status", getattr(exc, "status_code", None))},
        )
        self._write()

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.call_id}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(self.payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(self.path)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
