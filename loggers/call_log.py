"""Durable, task-scoped records of outbound model requests."""

from __future__ import annotations

import json
import re
import uuid
from contextvars import ContextVar, copy_context
from copy import deepcopy
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import LOGGING_ENABLED, LOGGING_DETAILED
from endpoints.runtime.model_budget import ModelCallBudget


_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("model_call_context", default=None)
_LOGICAL: ContextVar[LogicalCall | None] = ContextVar("logical_model_call", default=None)


def set_call_context(events_path: str | Path | None, *, operation_id: str | None = None,
                     criterion_id: str | None = None, stage: str = "indexing"):
    path = Path(events_path).parent / "calls" if events_path and LOGGING_ENABLED else None
    return _CONTEXT.set({"directory": path, "operation_id": operation_id,
                         "events_path": str(events_path) if events_path else None,
                         "criterion_id": criterion_id, "stage": stage,
                         "phase": "indexing" if stage == "indexing" else "retrieval"})


def reset_call_context(token) -> None:
    _CONTEXT.reset(token)


def set_call_stage(stage: str):
    context = _CONTEXT.get()
    return _CONTEXT.set({**context, "stage": stage} if context else None)


def reset_call_stage(token) -> None:
    _CONTEXT.reset(token)


@contextmanager
def call_stage(stage: str, reason: str | None = None):
    context = _CONTEXT.get() or {}
    token = _CONTEXT.set({**context, "stage": stage, "reason": reason})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_call_stage() -> str | None:
    return (_CONTEXT.get() or {}).get("stage")


def current_events_path() -> str | None:
    return (_CONTEXT.get() or {}).get("events_path")


def retrieval_call_stage(action: str) -> str:
    parts = (current_call_stage() or "").split(".")
    if len(parts) >= 2 and parts[0] == "route" and parts[1].isdigit():
        return f"{action}-step{parts[1]}"
    return action


def map_with_context(executor, function, items):
    """Carry immutable task correlation into each executor job."""
    iterator = iter(items)
    pending = []
    sentinel = object()
    limit = max(1, executor._max_workers)
    for _ in range(limit):
        item = next(iterator, sentinel)
        if item is not sentinel:
            pending.append(executor.submit(copy_context().run, function, item))
    results = []
    while pending:
        future = pending.pop(0)
        results.append(future.result())
        item = next(iterator, sentinel)
        if item is not sentinel:
            pending.append(executor.submit(copy_context().run, function, item))
    return results


def current_logical_call():
    return _LOGICAL.get()


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
        logical=_LOGICAL.get(),
    )


def append_route_event(event: str, payload: dict[str, Any]) -> None:
    context = _CONTEXT.get() or {}
    directory = context.get("directory")
    if directory is None or not LOGGING_ENABLED or not LOGGING_DETAILED:
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
                 operation_id: str | None, source: str | None,
                 logical: LogicalCall | None = None) -> None:
        self.logical = logical
        if logical and "attempt" in request:
            request = {**request, "attempt": logical.sent_count + 1}
        self.call_id = uuid.uuid4().hex
        instant = datetime.now(timezone.utc)
        call_source = logical.payload["source"] if logical else source or criterion_id or "Task"
        stage = logical.payload["stage"] if logical else stage
        filename = "__".join((
            _filename_part(call_source),
            _filename_part(stage or _call_stage_label(component, stage, criterion_id)),
            _file_timestamp(instant),
            self.call_id,
        ))
        self.path = directory / f"{filename}.json" if directory and LOGGING_DETAILED else None
        self.payload: dict[str, Any] = {
            "call_id": self.call_id, "operation_id": operation_id,
            "task_id": os.getenv("CONTRACT_TASK_ID"), "worker_pid": os.getpid(),
            "criterion_id": criterion_id, "source": call_source,
            "component": component, "stage": stage,
            "reason": logical.payload["reason"] if logical else None,
            "model": model, "status": "pending",
            "started_at": instant.astimezone().isoformat(timespec="milliseconds"),
            "request": deepcopy(request), "response": None, "error": None,
            "logical_call_id": logical.logical_call_id if logical else None,
            "category": logical.payload["category"] if logical else None,
            "attempt": logical.sent_count + 1 if logical else 1,
            "is_retry": bool(logical and logical.sent_count),
            **token_fields(None),
        }
        if logical is not None:
            logical.attempt_records.append(self)
        self._write()  # A failed write must prevent the outbound request.

    @property
    def reference_id(self) -> str:
        return self.call_id if self.path or not self.logical else self.logical.logical_call_id

    @property
    def reference_path(self) -> Path | None:
        return self.path or (self.logical.path if self.logical else None)

    def sent(self) -> None:
        """Count only requests that actually reach the model client."""
        self.payload["sent"] = True
        if self.logical:
            self.logical.sent_count += 1
            self.logical.payload["send_count"] = self.logical.sent_count
            self.logical.payload["retry_count"] = max(0, self.logical.sent_count - 1)
            self.logical.last_record = self
            self.logical._write()

    def succeeded(self, response: Any, *, http_status: int | None = None) -> None:
        tokens = token_fields(response.get("usage") if isinstance(response, dict) else None,
                              embedding=self.payload["component"].endswith("embedding"))
        self.payload.update(status="succeeded", finished_at=_now(), response=response,
                            http_status=http_status, **tokens)
        if self.logical:
            self.logical.tokens = tokens
            self.logical.last_record = self
        self._write()

    def failed(
        self,
        exc: BaseException,
        *,
        stage: str = "request",
        timing: dict[str, float] | None = None,
    ) -> None:
        self.payload.update(
            status="failed", finished_at=_now(), failure_stage=stage,
            error={"type": type(exc).__name__, "message": str(exc),
                   "http_status": self.payload.get("http_status") if stage == "output_validation" else _error_http_status(exc)},
            **(token_fields(None) if self.payload.get("response") is None else {}),
        )
        if timing is not None:
            self.payload["timing"] = timing
        if self.logical:
            self.logical.tokens = token_fields(None)
            self.logical.last_record = self
        self._write()

    def _write(self) -> None:
        _atomic_write(self.path, self.payload)
        if self.logical:
            self.logical._write()


def token_fields(usage: Any, *, embedding: bool = False) -> dict[str, int | None]:
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = None if embedding else usage.get("completion_tokens", usage.get("output_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_output_tokens": input_tokens if embedding else (
            input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None
        ),
    }


def request_model(resource, **kwargs):
    """Return parsed SDK output and the HTTP status of the received response."""
    raw = resource.with_raw_response.create(**kwargs)
    return raw.parse(), getattr(raw, "status_code", None)


async def request_model_async(resource, **kwargs):
    raw = await resource.with_raw_response.create(**kwargs)
    return raw.parse(), getattr(raw, "status_code", None)


class LogicalCall:
    def __init__(self, component: str, model: str, directory: Path | None, source: str | None,
                 stage: str | None = None, reason: str | None = None):
        context = _CONTEXT.get() or {}
        directory = directory or context.get("directory")
        self.logical_call_id = uuid.uuid4().hex
        self.sent_count = 0
        self.budget = ModelCallBudget()
        self.attempt_records = []
        self.last_record: CallRecord | None = None
        self.tokens = token_fields(None)
        phase = context.get("phase", "indexing")
        if component.endswith("reranker"):
            phase = "retrieval"
        category = "agent" if component == "agent" else f"docxindex.{phase}.{component.rsplit('.', 1)[-1]}"
        criterion_id = context.get("criterion_id")
        call_source = source or (f"docxindex_{criterion_id}" if criterion_id else f"docxindex_indexing_{component.rsplit('.', 1)[-1]}" if phase == "indexing" else "docxindex")
        stage = stage or context.get("stage") or "model"
        reason = reason if reason is not None else context.get("reason")
        filename = "__".join((_filename_part(call_source), _filename_part(stage), self.logical_call_id))
        self.path = directory.parent / "logical_calls" / f"{filename}.json" if directory and LOGGING_ENABLED else None
        self.payload = {
            "logical_call_id": self.logical_call_id, "category": category,
            "source": call_source, "stage": stage, "reason": str(reason)[:200] if reason else None,
            "model": model, "started_at": _now(),
            "finished_at": None, "status": "pending", "retry_count": 0,
            **self.tokens, "error": None,
        }
        self._write()

    def _write(self) -> None:
        self.payload["attempts"] = [
            {key: record.payload.get(key) for key in
             ("call_id", "status", "sent", "send_number", "http_status", "model_type", "semaphore_key", "budget_seconds",
              "remaining_seconds", "input_tokens", "output_tokens", "input_output_tokens",
              "failure_stage", "error", "timing", "retry_reason", "retry_delay_seconds")}
            for record in self.attempt_records
        ]
        sent = [record.payload for record in self.attempt_records if record.payload.get("sent")]
        self.payload["input_usage_complete"] = all(item.get("input_tokens") is not None for item in sent)
        self.payload["output_usage_complete"] = all(
            item.get("output_tokens") is not None or item["component"].endswith("embedding") for item in sent
        )
        self.payload["usage_complete"] = self.payload["input_usage_complete"] and self.payload["output_usage_complete"]
        self.payload["known_input_tokens"] = sum(item["input_tokens"] for item in sent if item.get("input_tokens") is not None)
        self.payload["known_output_tokens"] = sum(item["output_tokens"] for item in sent if item.get("output_tokens") is not None)
        _atomic_write(self.path, self.payload)

    def reject_response(self, exc: BaseException, *, reason: str = "JSON/schema 校验未通过") -> None:
        if self.last_record:
            self.last_record.failed(exc, stage="output_validation")
        original = self.payload["reason"]
        reason = f"{reason}：{exc}"
        self.payload["reason"] = f"{original}; {reason}"[:200] if original else reason[:200]
        self._write()

    def finish(self, exc: BaseException | None = None) -> None:
        if exc is not None:
            if self.last_record and self.last_record.payload["status"] == "succeeded":
                self.last_record.failed(exc, stage="output_validation")
            self.failed(exc)
        elif self.payload["status"] != "failed":
            self.payload.update(status="succeeded", **self.tokens)
        self.payload["finished_at"] = _now()
        self._write()

    def failed(self, exc: BaseException) -> None:
        http_status = _error_http_status(exc)
        if http_status is None and self.last_record and self.last_record.payload.get("failure_stage") == "output_validation":
            http_status = (self.last_record.payload.get("error") or {}).get("http_status")
        self.payload.update(status="failed", **token_fields(None), error={
            "type": getattr(exc, "error_type", type(exc).__name__),
            "http_status": http_status,
            "message": str(exc)[:200],
        })


@contextmanager
def logical_call(component: str, model: str, *, directory: Path | None = None,
                 source: str | None = None, stage: str | None = None,
                 reason: str | None = None, record: LogicalCall | None = None,
                 finalize: bool = True):
    """Nested transport/schema retries belong to the same logical request."""
    current = _LOGICAL.get()
    if current is not None:
        yield current
        return
    record = record or LogicalCall(component, model, directory, source, stage, reason)
    token = _LOGICAL.set(record)
    try:
        yield record
    except BaseException as exc:
        if record.last_record and record.last_record.payload["status"] == "succeeded":
            record.last_record.failed(exc, stage="output_validation")
        record.failed(exc)
        raise
    finally:
        _LOGICAL.reset(token)
        if finalize or record.payload["status"] == "failed":
            record.finish()


def _atomic_write(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _error_http_status(exc: BaseException) -> int | None:
    return (getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
            or getattr(getattr(exc, "response", None), "status_code", None))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _file_timestamp(instant: datetime) -> str:
    return instant.strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def _filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")[:64] or "Task"


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
