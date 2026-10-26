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
from loggers.api_event_logger import append_api_event
from loggers.task_log_io import allocate_logical_no


_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("model_call_context", default=None)
_LOGICAL: ContextVar[LogicalCall | None] = ContextVar("logical_model_call", default=None)


def set_call_context(events_path: str | Path | None, *, operation_id: str | None = None,
                     criterion_id: str | None = None, criterion_attempt: int | None = None,
                     stage: str = "indexing"):
    path = Path(events_path).parent / "calls" if events_path and LOGGING_ENABLED else None
    return _CONTEXT.set({"directory": path, "operation_id": operation_id,
                         "events_path": str(events_path) if events_path else None,
                         "criterion_id": criterion_id, "criterion_attempt": criterion_attempt,
                         "stage": stage,
                         "phase": "indexing" if stage == "indexing" else "retrieval"})


def reset_call_context(token) -> None:
    _CONTEXT.reset(token)


def current_call_context() -> dict[str, Any]:
    """Return task correlation without exposing the mutable context dictionary."""
    return dict(_CONTEXT.get() or {})


@contextmanager
def criterion_call_context(criterion_id: str, criterion_attempt: int, *,
                           events_path: str | Path | None = None):
    """Add criterion recovery metadata while retaining the task's log paths."""
    context = {**(_CONTEXT.get() or {}), "criterion_id": criterion_id,
               "criterion_attempt": criterion_attempt}
    if events_path is not None:
        context.update(events_path=str(events_path),
                       directory=Path(events_path).parent / "calls" if LOGGING_ENABLED else None)
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
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
             stage: str | None = None, source: str | None = None,
             criterion_attempt: int | None = None) -> "CallRecord":
    context = _CONTEXT.get() or {}
    return CallRecord(
        directory=(directory if directory is not None else context.get("directory")) if LOGGING_ENABLED else None,
        component=component, model=model, request=request,
        criterion_id=criterion_id if criterion_id is not None else context.get("criterion_id"),
        stage=stage if stage is not None else context.get("stage"),
        operation_id=context.get("operation_id"),
        source=source,
        logical=_LOGICAL.get(),
        criterion_attempt=(criterion_attempt if criterion_attempt is not None
                           else context.get("criterion_attempt")),
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
                 logical: LogicalCall | None = None,
                 criterion_attempt: int | None = None) -> None:
        self.logical = logical
        if logical and "attempt" in request:
            request = {**request, "attempt": logical.sent_count + 1}
        self.call_id = uuid.uuid4().hex
        instant = datetime.now(timezone.utc)
        call_source = logical.payload["source"] if logical else source or criterion_id or "Task"
        stage = logical.payload["stage"] if logical else stage
        if logical:
            criterion_id = logical.payload["criterion_id"] or criterion_id
            criterion_attempt = (logical.payload["criterion_attempt"] if logical.payload["criterion_attempt"] is not None
                                 else criterion_attempt)
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
            "criterion_id": criterion_id, "criterion_attempt": criterion_attempt,
            "source": call_source,
            "component": component, "stage": stage,
            "reason": logical.payload["reason"] if logical else None,
            "model": model, "status": "pending",
            "started_at": instant.astimezone().isoformat(timespec="milliseconds"),
            "request": deepcopy(request), "response": None, "error": None,
            "logical_call_id": logical.logical_call_id if logical else None,
            "logical_no": logical.logical_no if logical else None,
            "category": logical.payload["category"] if logical else None,
            "attempt": logical.sent_count + 1 if logical else 1,
            "is_retry": bool(logical and logical.sent_count),
            **token_fields(None),
            "local_input_tokens": None, "received_output_tokens": None,
            "service_usage": None, "input_token_source": None, "output_token_source": None,
            "output_completeness": "unknown", "sent": False,
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

    def event(self, event: str, summary: str, **fields: Any) -> None:
        """Write a summary using this attempt's stored task and logical identity."""
        if self.logical is not None:
            self.logical.event(event, summary, **fields)
        elif self.path is not None and LOGGING_ENABLED:
            append_api_event(self.path.parent.parent / "events.log", event, summary=summary,
                             **{**fields, "logical_no": self.payload["logical_no"],
                                "category": self.payload["category"], "criterion_id": self.payload["criterion_id"],
                                "source": self.payload["source"], "stage": self.payload["stage"]})

    def sent(self) -> None:
        """Count only requests that actually reach the model client."""
        self.payload["sent"] = True
        self.payload["sent_at"] = _now()
        if self.logical:
            self.logical.sent_count += 1
            self.logical.payload["send_count"] = self.logical.sent_count
            self.logical.payload["retry_count"] = max(0, self.logical.sent_count - 1)
            self.logical.last_record = self
            self.logical._write()

    def record_local_input(self, count: int, metadata: dict[str, Any] | None = None) -> None:
        """Save locally counted input even when admission or HTTP later fails."""
        self.payload["local_input_tokens"] = count
        if metadata:
            self.payload["tokenizer"] = deepcopy(metadata)
        self._refresh_tokens()
        self._write()

    def record_received_output(self, count: int, complete: bool = False) -> None:
        """Count cumulative received output; callers must not sum tokenized deltas."""
        self.payload.update(received_output_tokens=count,
                            output_completeness="complete" if complete else "partial")
        self._refresh_tokens()

    def record_service_usage(self, usage: Any, *, embedding: bool = False) -> None:
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if isinstance(usage, dict):
            self.payload["service_usage"] = deepcopy(usage)
        self._refresh_tokens(embedding=embedding)

    def _refresh_tokens(self, *, embedding: bool = False) -> None:
        embedding = embedding or self.payload.get("model_type") == "embedding" or self.payload["component"].endswith("embedding")
        reranker = self.payload.get("model_type") == "reranker" or self.payload["component"].endswith("reranker")
        usage = token_fields(self.payload.get("service_usage"), embedding=embedding)
        local_input = self.payload.get("local_input_tokens")
        local_output = self.payload.get("received_output_tokens")
        received_input, received_output = usage["input_tokens"], usage["output_tokens"]
        input_count = received_input if received_input is not None else local_input
        output_count = received_output if received_output is not None else local_output
        self.payload.update(
            input_tokens=input_count, output_tokens=None if embedding or reranker else output_count,
            input_token_source="service" if received_input is not None else "local" if local_input is not None else None,
            output_token_source="not_applicable" if embedding or reranker else "service" if received_output is not None else "local" if local_output is not None else None,
            input_output_tokens=input_count if embedding or reranker else
            input_count + output_count if input_count is not None and output_count is not None else None,
        )

    def succeeded(self, response: Any, *, http_status: int | None = None) -> None:
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        self.record_service_usage(usage)
        self.payload.update(status="succeeded", finished_at=_now(), response=response,
                            http_status=http_status, output_completeness="complete")
        if self.logical:
            self.logical.tokens = {key: self.payload[key] for key in token_fields(None)}
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
        )
        if timing is not None:
            self.payload["timing"] = timing
        if self.logical:
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


def summarize_model_attempts(logical_dir: str | Path) -> dict[str, Any]:
    """Sum unique sent attempts, including failed/partial ones, from compact logs.

    Input/output totals are known received or locally counted amounts. Missing
    output remains explicitly unknown rather than being claimed as zero usage.
    """
    summary = {key: 0 for key in ("planner", "retrieval", "execute", "summarizer", "total",
               "input", "output", "unknown_output_attempts", "local_fallback_attempts",
               "sent_attempts", "input_rejections", "partial_output_attempts", "unknown_input_attempts")}
    attempts = {}
    unreadable = []
    for path in Path(logical_dir).glob("*.json"):
        try:
            logical = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            unreadable.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        source = str(logical.get("source") or "").lower()
        category = str(logical.get("category") or "").lower()
        group = ("planner" if source.startswith("planner") else "summarizer" if source.startswith("summarizer")
                 else "retrieval" if category.startswith(("docxindex", "llamaindex"))
                 or source.startswith(("docxindex", "llamaindex")) else "execute")
        for attempt in logical.get("attempts") or []:
            call_id = attempt.get("call_id")
            if call_id:
                attempts[call_id] = (group, attempt)
    for group, attempt in attempts.values():
        if not attempt.get("sent"):
            summary["input_rejections"] += int(attempt.get("failure_stage") == "input_validation")
            continue
        summary["sent_attempts"] += 1
        input_count = attempt.get("input_tokens")
        output_count = attempt.get("output_tokens")
        known_input = input_count if isinstance(input_count, int) else 0
        known_output = output_count if isinstance(output_count, int) else 0
        summary["input"] += known_input
        summary["unknown_input_attempts"] += int(input_count is None)
        summary["output"] += known_output
        summary[group] += known_input + known_output
        summary["unknown_output_attempts"] += int(
            output_count is None and attempt.get("output_token_source") != "not_applicable"
            and attempt.get("model_type") not in {"embedding", "reranker"})
        summary["local_fallback_attempts"] += int(
            attempt.get("input_token_source") == "local" or attempt.get("output_token_source") == "local")
        summary["partial_output_attempts"] += int(attempt.get("output_completeness") == "partial")
    summary["total"] = summary["input"] + summary["output"]
    missing_logs = not Path(logical_dir).is_dir() or bool(unreadable)
    summary["output_complete"] = not (missing_logs or summary["unknown_output_attempts"] or summary["partial_output_attempts"])
    summary["stats_complete"] = summary["output_complete"] and not summary["unknown_input_attempts"]
    summary["total_is_lower_bound"] = not summary["stats_complete"]
    if unreadable:
        summary["unreadable_records"] = unreadable
    return summary


def request_model(resource, **kwargs):
    """Return parsed SDK output and the HTTP status of the received response."""
    raw = resource.with_raw_response.create(**kwargs)
    return raw.parse(), getattr(raw, "status_code", None)


async def request_model_async(resource, **kwargs):
    raw = await resource.with_raw_response.create(**kwargs)
    return raw.parse(), getattr(raw, "status_code", None)


class LogicalCall:
    def __init__(self, component: str, model: str, directory: Path | None, source: str | None,
                 stage: str | None = None, reason: str | None = None,
                 criterion_id: str | None = None, criterion_attempt: int | None = None):
        context = _CONTEXT.get() or {}
        directory = directory or context.get("directory")
        directory = Path(directory) if directory is not None and LOGGING_ENABLED else None
        self.logical_call_id = uuid.uuid4().hex
        self.logical_no = allocate_logical_no(directory.parent) if directory is not None else None
        self.events_path = directory.parent / "events.log" if directory is not None else None
        self.sent_count = 0
        self.budget = ModelCallBudget()
        self.attempt_records = []
        self.last_record: CallRecord | None = None
        self.tokens = token_fields(None)
        self._finished_event_signature: tuple[str, int, int] | None = None
        phase = context.get("phase", "indexing")
        if component.endswith("reranker"):
            phase = "retrieval"
        backend = "llamaindex" if component.startswith("llamaindex.") else "docxindex"
        category = "agent" if component == "agent" else f"{backend}.{phase}.{component.rsplit('.', 1)[-1]}"
        criterion_id = criterion_id if criterion_id is not None else context.get("criterion_id")
        criterion_attempt = (criterion_attempt if criterion_attempt is not None
                             else context.get("criterion_attempt"))
        call_source = source or (f"{backend}_{criterion_id}" if criterion_id else f"{backend}_indexing_{component.rsplit('.', 1)[-1]}" if phase == "indexing" else backend)
        stage = stage or context.get("stage") or "model"
        reason = reason if reason is not None else context.get("reason")
        filename = "__".join((str(self.logical_no), _filename_part(category, allow_dots=True),
                              _filename_part(call_source), _filename_part(stage)))
        self.path = directory.parent / "logical_calls" / f"{filename}.json" if directory and LOGGING_ENABLED else None
        self.payload = {
            "logical_call_id": self.logical_call_id, "logical_no": self.logical_no,
            "category": category,
            "operation_id": context.get("operation_id"), "criterion_id": criterion_id,
            "criterion_attempt": criterion_attempt,
            "task_id": os.getenv("CONTRACT_TASK_ID"), "worker_pid": os.getpid(),
            "source": call_source, "stage": stage, "reason": str(reason)[:200] if reason else None,
            "model": model, "started_at": _now(),
            "finished_at": None, "status": "pending", "retry_count": 0,
            **self.tokens, "error": None,
        }
        self._write()
        self.event("logical_call_started", "Operation started.")

    def event(self, event: str, summary: str, **fields: Any) -> None:
        """Use one identity for the time line and the detailed logical record."""
        if self.events_path is not None and LOGGING_ENABLED:
            append_api_event(self.events_path, event, summary=summary,
                             **{**fields, "logical_no": self.logical_no, "category": self.payload["category"],
                                "criterion_id": self.payload["criterion_id"], "source": self.payload["source"],
                                "stage": self.payload["stage"]})

    def _write(self) -> None:
        self.payload["attempts"] = [
            {key: record.payload.get(key) for key in
             ("call_id", "logical_call_id", "logical_no", "category", "criterion_id", "criterion_attempt", "source", "stage",
              "status", "sent", "sent_at", "started_at", "finished_at", "send_number", "http_status", "model_type", "semaphore_key", "budget_seconds",
              "remaining_seconds", "input_tokens", "output_tokens", "input_output_tokens",
              "failure_stage", "error", "timing", "retry_reason", "retry_delay_seconds",
              "transport", "stream", "first_chunk_seconds", "response_seconds", "stream_complete",
              "timeout_kind", "stream_chunk_count", "content_chars", "reasoning_chars", "tool_chars",
              "first_content_seconds", "last_content_seconds", "max_content_gap_seconds",
              "finish_reasons", "usage_received", "lease_renewals", "done_received", "end_source", "error_response",
              "local_input_tokens", "received_output_tokens", "service_usage", "tokenizer",
              "input_token_source", "output_token_source", "output_completeness",
              "input_token_counts", "max_input_tokens", "input_preparation",
              "rpm_key", "rpm_count", "max_requests_per_minute", "concurrent_requests", "wait_reason",
              "rpm_window_count", "concurrency_wait_seconds", "rpm_wait_seconds", "admitted_at",
              "admitted_at_ms", "acquired_concurrency", "stream_finished_seconds",
              "input_headroom_tokens", "input_limit_utilization")}
            for record in self.attempt_records
        ]
        sent = [record.payload for record in self.attempt_records if record.payload.get("sent")]
        self.payload["input_usage_complete"] = all(
            token_fields(item.get("service_usage"))["input_tokens"] is not None for item in sent)
        self.payload["output_usage_complete"] = all(
            token_fields(item.get("service_usage"))["output_tokens"] is not None
            or item.get("model_type") in {"embedding", "reranker"}
            or item["component"].endswith(("embedding", "reranker")) for item in sent
        )
        self.payload["usage_complete"] = self.payload["input_usage_complete"] and self.payload["output_usage_complete"]
        self.payload["known_input_tokens"] = sum(item["input_tokens"] for item in sent if item.get("input_tokens") is not None)
        self.payload["known_output_tokens"] = sum(item["output_tokens"] for item in sent if item.get("output_tokens") is not None)
        self.payload["input_tokens"] = self.payload["known_input_tokens"] if any(item.get("input_tokens") is not None for item in sent) else None
        self.payload["output_tokens"] = self.payload["known_output_tokens"] if any(item.get("output_tokens") is not None for item in sent) else None
        non_text_output = bool(sent) and all(item.get("output_token_source") == "not_applicable" for item in sent)
        self.payload["input_output_tokens"] = self.payload["input_tokens"] if non_text_output else (
            self.payload["input_tokens"] + self.payload["output_tokens"]
            if self.payload["input_tokens"] is not None and self.payload["output_tokens"] is not None else None)
        self.payload["input_count_complete"] = all(item.get("input_tokens") is not None for item in sent)
        self.payload["output_count_complete"] = all(
            item.get("output_tokens") is not None and item.get("output_completeness") == "complete"
            or item.get("output_token_source") == "not_applicable" for item in sent)
        _atomic_write(self.path, self.payload)

    def reject_response(self, exc: BaseException, *, reason: str = "JSON/schema 校验未通过") -> None:
        if self.last_record:
            self.last_record.failed(exc, stage="output_validation")
        original = self.payload["reason"]
        reason = f"{reason}：{exc}"
        self.payload["reason"] = f"{original}; {reason}"[:200] if original else reason[:200]
        self._write()
        number = self.last_record.payload.get("send_number", self.sent_count) if self.last_record else self.sent_count
        self.event("model_output_validation_failed",
                   f"Response {number} failed JSON/schema validation. {type(exc).__name__}.")

    def finish(self, exc: BaseException | None = None) -> None:
        if exc is not None:
            if self.last_record and self.last_record.payload["status"] == "succeeded":
                self.last_record.failed(exc, stage="output_validation")
            self.failed(exc)
        elif self.payload["status"] != "failed":
            self.payload.update(status="succeeded", **self.tokens)
        signature = (self.payload["status"], self.sent_count, len(self.attempt_records))
        if self._finished_event_signature == signature:
            # An outer validator can replace the final error without another
            # request. Persist that detail while retaining one finish event.
            self._write()
            return
        self.payload["finished_at"] = _now()
        self._write()
        self._finished_event_signature = signature
        self.event("logical_call_failed" if self.payload["status"] == "failed" else "logical_call_finished",
                   self._finish_summary())

    def _finish_summary(self) -> str:
        validation = "Operation failed" if self.payload["status"] == "failed" else "Validation passed"
        result = (f"{validation}. Requests: {self.sent_count}. "
                  f"Total input: {self.payload['known_input_tokens']} tokens")
        if not self.payload["input_count_complete"]:
            result += " (incomplete)"
        result += ". "
        sent = [record.payload for record in self.attempt_records if record.payload.get("sent")]
        if sent and all(item.get("output_token_source") == "not_applicable" for item in sent):
            return result + "Output: not applicable."
        result += f"Known output: {self.payload['known_output_tokens']} tokens"
        if not self.payload["output_count_complete"]:
            result += " (incomplete)"
        return result + "."

    def failed(self, exc: BaseException) -> None:
        http_status = _error_http_status(exc)
        if http_status is None and self.last_record and self.last_record.payload.get("failure_stage") == "output_validation":
            http_status = (self.last_record.payload.get("error") or {}).get("http_status")
        self.payload.update(status="failed", error={
            "type": getattr(exc, "error_type", type(exc).__name__),
            "http_status": http_status,
            "message": str(exc)[:200],
        })


@contextmanager
def logical_call(component: str, model: str, *, directory: Path | None = None,
                 source: str | None = None, stage: str | None = None,
                 reason: str | None = None, record: LogicalCall | None = None,
                 finalize: bool = True, criterion_id: str | None = None,
                 criterion_attempt: int | None = None):
    """Nested transport/schema retries belong to the same logical request."""
    current = _LOGICAL.get()
    if current is not None:
        yield current
        return
    record = record or LogicalCall(component, model, directory, source, stage, reason,
                                   criterion_id, criterion_attempt)
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


def _filename_part(value: str, *, allow_dots: bool = False) -> str:
    pattern = r"[^A-Za-z0-9_.-]+" if allow_dots else r"[^A-Za-z0-9_-]+"
    return re.sub(pattern, "-", str(value)).strip("-.")[:64] or "Task"


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
