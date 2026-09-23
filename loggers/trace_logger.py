from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from config import LOGGING_ENABLED
from loggers.trace_helpers import error_info


_CURRENT_TRACE: ContextVar["TraceLogger | None"] = ContextVar("current_trace", default=None)
_CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("current_run_id", default=None)


def set_current_trace(trace: "TraceLogger | None"):
    return _CURRENT_TRACE.set(trace)


def reset_current_trace(token) -> None:
    _CURRENT_TRACE.reset(token)


def get_current_trace() -> "TraceLogger | NoopTraceLogger":
    return _CURRENT_TRACE.get() or NOOP_TRACE


class TraceLogger:
    def __init__(
        self,
        path: str | Path | None,
        *,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.path = Path(path) if path and LOGGING_ENABLED else None
        self.trace_id = trace_id or uuid.uuid4().hex
        self.metadata = metadata or {}
        self.runs: list[dict[str, Any]] = []
        self._counter = 0
        self._write_lock = threading.Lock()
        self.last_write_error: str | None = None

    def span(
        self,
        name: str,
        *,
        run_type: str,
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "TraceSpan":
        return TraceSpan(
            trace=self,
            name=name,
            run_type=run_type,
            inputs=inputs,
            metadata=metadata,
        )

    def _next_run_id(self) -> str:
        self._counter += 1
        return f"run-{self._counter:05d}"

    def _append_run(self, run: dict[str, Any]) -> None:
        self.runs.append(run)
        self._write()

    def add_external_call(self, call: dict[str, Any], *, parent_id: str) -> None:
        """Import one MCP-process call without sharing the trace writer."""
        started = call.get("started_at") or _now_iso()
        finished = call.get("finished_at")
        self._append_run({
            "id": self._next_run_id(), "parent_id": parent_id,
            "name": f"llm.{call.get('component')}", "type": "llm",
            "status": call.get("status"), "start_time": started,
            "end_time": finished, "duration_seconds": None,
            "inputs": {"model": call.get("model"), "criterion_id": call.get("criterion_id"),
                       "stage": call.get("stage")},
            "outputs": {"call_id": call.get("call_id"),
                        "call_path": call.get("call_path")},
            "metadata": {"operation_id": call.get("operation_id")},
            "error": call.get("error"),
        })

    def add_external_route(self, event: dict[str, Any], *, parent_id: str, path: Path) -> None:
        self._append_run({
            "id": self._next_run_id(), "parent_id": parent_id,
            "name": f"docxindex.{event.get('event')}", "type": "retrieval",
            "status": "succeeded", "start_time": event.get("at"),
            "end_time": event.get("at"), "duration_seconds": 0,
            "inputs": {"criterion_id": event.get("criterion_id"), "stage": event.get("stage")},
            "outputs": event.get("payload") or {},
            "metadata": {"event_path": str(path)}, "error": None,
        })

    def _write(self) -> None:
        if self.path is None:
            return
        try:
            with self._write_lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                metadata = dict(self.metadata)
                if self.last_write_error:
                    metadata["trace_write_warning"] = self.last_write_error
                payload = {
                    "trace_id": self.trace_id,
                    "created_at": self.runs[0]["start_time"] if self.runs else _now_iso(),
                    "metadata": metadata,
                    "runs": self.runs,
                }
                temp_path = self.path.with_name(f"{self.path.stem}.{uuid.uuid4().hex}.tmp")
                temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temp_path.replace(self.path)
        except OSError as exc:
            self.last_write_error = f"{exc.__class__.__name__}: {exc}"
            print(f"[TraceLogger] warning: failed to write trace file: {self.last_write_error}", file=sys.stderr)


class TraceSpan:
    def __init__(
        self,
        *,
        trace: TraceLogger,
        name: str,
        run_type: str,
        inputs: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        self.trace = trace
        self.run = {
            "id": trace._next_run_id(),
            "parent_id": _CURRENT_RUN_ID.get(),
            "name": name,
            "type": run_type,
            "status": "running",
            "start_time": _now_iso(),
            "end_time": None,
            "duration_seconds": None,
            "inputs": inputs or {},
            "outputs": {},
            "metadata": metadata or {},
            "error": None,
        }
        self._start_monotonic = 0.0
        self._token = None

    async def __aenter__(self) -> "TraceSpan":
        self._start_monotonic = time.monotonic()
        self.trace._append_run(self.run)
        self._token = _CURRENT_RUN_ID.set(self.run["id"])
        return self

    async def __aexit__(self, exc_type, exc, tb) -> Literal[False]:
        if exc is not None and self.run["error"] is None:
            self.set_error(error_info(exc))
        self.run["status"] = "failed" if self.run["error"] else "succeeded"
        self.run["end_time"] = _now_iso()
        self.run["duration_seconds"] = round(time.monotonic() - self._start_monotonic, 3)
        if self._token is not None:
            _CURRENT_RUN_ID.reset(self._token)
        self.trace._write()
        return False

    def set_outputs(self, outputs: dict[str, Any] | None) -> None:
        if outputs:
            self.run["outputs"].update(outputs)

    def set_metadata(self, metadata: dict[str, Any] | None) -> None:
        if metadata:
            self.run["metadata"].update(metadata)

    def set_error(self, error: dict[str, Any] | None) -> None:
        if error:
            self.run["error"] = error


class NoopTraceLogger:
    def add_external_call(self, call: dict[str, Any], *, parent_id: str) -> None:
        return None

    def add_external_route(self, event: dict[str, Any], *, parent_id: str, path: Path) -> None:
        return None

    def span(self, *args, **kwargs) -> "NoopTraceSpan":
        return NoopTraceSpan()


class NoopTraceSpan:
    async def __aenter__(self) -> "NoopTraceSpan":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> Literal[False]:
        return False

    def set_outputs(self, outputs: dict[str, Any] | None) -> None:
        return None

    def set_metadata(self, metadata: dict[str, Any] | None) -> None:
        return None

    def set_error(self, error: dict[str, Any] | None) -> None:
        return None


NOOP_TRACE = NoopTraceLogger()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
