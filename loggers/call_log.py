"""Durable, task-scoped records of outbound model requests."""

from __future__ import annotations

import json
import re
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
             stage: str | None = None, source: str | None = None) -> "CallRecord":
    context = _CONTEXT.get() or {}
    return CallRecord(
        directory=(directory if directory is not None else context.get("directory")) if LOGGING_ENABLED else None,
        component=component, model=model, request=request,
        criterion_id=criterion_id if criterion_id is not None else context.get("criterion_id"),
        stage=stage if stage is not None else context.get("stage"),
        operation_id=context.get("operation_id"),
        source=source,
    )


def append_route_event(event: str, payload: dict[str, Any]) -> None:
    context = _CONTEXT.get() or {}
    directory = context.get("directory")
    if directory is None:
        return
    instant = datetime.now(timezone.utc)
    name = "__".join((
        _filename_part(context.get("criterion_id") or "Task"),
        _filename_part(event),
        _file_timestamp(instant),
        uuid.uuid4().hex,
    ))
    path = directory.parent / "route_events" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"operation_id": context.get("operation_id"),
                                "criterion_id": context.get("criterion_id"),
                                "stage": context.get("stage"), "event": event,
                                "at": instant.astimezone().isoformat(timespec="milliseconds"),
                                "payload": payload},
                               ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class CallRecord:
    def __init__(self, *, directory: Path | None, component: str, model: str,
                 request: dict[str, Any], criterion_id: str | None, stage: str | None,
                 operation_id: str | None, source: str | None) -> None:
        self.call_id = uuid.uuid4().hex
        instant = datetime.now(timezone.utc)
        call_source = criterion_id or source or ("Index" if stage == "indexing" else "Task")
        filename = "__".join((
            _filename_part(call_source),
            _filename_part(_call_stage_label(component, stage, criterion_id)),
            _file_timestamp(instant),
            self.call_id,
        ))
        self.path = directory / f"{filename}.json" if directory else None
        self.payload: dict[str, Any] = {
            "call_id": self.call_id, "operation_id": operation_id,
            "criterion_id": criterion_id, "source": call_source,
            "component": component, "stage": stage,
            "model": model, "status": "pending",
            "started_at": instant.astimezone().isoformat(timespec="milliseconds"),
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


def _file_timestamp(instant: datetime) -> str:
    return instant.strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def _filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", str(value)).strip("-")[:64] or "Task"


def _call_stage_label(component: str, stage: str | None, criterion_id: str | None) -> str:
    if component == "agent":
        return "agent-model" if criterion_id else "model"
    if component.startswith("docxindex."):
        if stage and stage.startswith("route."):
            return f"docxindex-{stage.rsplit('.', 1)[-1]}"
        if stage == "indexing":
            return component.rsplit(".", 1)[-1]
        return f"docxindex-{stage or component.rsplit('.', 1)[-1]}"
    return stage or component
