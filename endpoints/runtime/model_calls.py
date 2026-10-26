"""Time each HTTP send independently while sharing retry and acquisition limits."""
from __future__ import annotations

import asyncio
import math
import time
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx
from openai import APIConnectionError

from config import MODEL_INPUT_LIMITS
from endpoints.runtime.errors import ModelInputTooLong
from endpoints.runtime.model_limits import model_semaphore, semaphore_metadata
from endpoints.runtime.model_tokens import count_model_input
from endpoints.runtime.model_budget import (
    ModelCallBudget,
    ModelCallTimeout,
    ModelRetryLimitError,
    ModelSemaphoreTimeout,
    TaskDeadlineExceeded,
)
from loggers.call_log import CallRecord, current_logical_call, logical_call, new_call


_ACTIVE_CALL = ContextVar("active_model_call", default=None)
DEFAULT_INPUT_LIMIT = object()


@dataclass
class ModelRequest:
    record: CallRecord
    budget: ModelCallBudget
    timer: asyncio.Timeout

    def received_content(self) -> None:
        self.budget.received_content()
        remaining = self.budget.remaining()
        self.timer.reschedule(asyncio.get_running_loop().time() + remaining if math.isfinite(remaining) else None)


def active_model_call():
    """Return the record, budget and timer of the attempt holding a model slot."""
    return _ACTIVE_CALL.get()


def record_input_rejection(*, model_type, model, caller, request, limit,
                           error: ModelInputTooLong, preparation=None) -> None:
    """Record an unsplittable local input without admission, HTTP or retries.

    Preparation can fail before execute_model is reached. Give that rejection
    the same compact-log and tokenizer provenance as the final request guard.
    """
    with logical_call(caller, model, stage="input-preparation") as logical:
        record = new_call(component=caller, model=model, request=request)
        counted = count_model_input(model_type, model, request)
        largest = max(counted.input_token_counts, default=0)
        record.payload.update(model_type=model_type, sent=False,
                              input_token_counts=counted.input_token_counts,
                              max_input_tokens=limit,
                              input_preparation=preparation or {},
                              input_headroom_tokens=limit - largest if limit is not None else None,
                              input_limit_utilization=largest / limit if limit else None)
        record.record_local_input(counted.input_tokens, counted.metadata)
        error.model_type = model_type
        error.model = model
        error.input_tokens = largest
        error.limit = limit
        error.caller = caller
        error.args = (f"{error} ({model_type}; model={model}; input_tokens={largest}; "
                      f"max_input_tokens={limit}; sent=false)",)
        error.call_id = record.reference_id
        error.call_path = str(record.reference_path) if record.reference_path else None
        record.failed(error, stage="input_validation")
        record.event("model_input_rejected", f"Request not sent. Input: {counted.input_tokens} tokens. Input validation failed.")
        logical.failed(error)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (TaskDeadlineExceeded, ModelSemaphoreTimeout)):
        return False
    status = (getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
              or getattr(getattr(exc, "response", None), "status_code", None))
    return (isinstance(exc, (ModelCallTimeout, APIConnectionError, httpx.TransportError))
            or status in (408, 409, 429) or isinstance(status, int) and status >= 500)


def _retry_delay(exc: Exception, attempt: int) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    milliseconds = headers.get("retry-after-ms")
    raw = headers.get("retry-after")
    if milliseconds:
        try:
            seconds = float(milliseconds) / 1000
            if math.isfinite(seconds):
                return max(0, seconds)
        except ValueError:
            pass
    if raw:
        try:
            seconds = float(raw)
            if math.isfinite(seconds):
                return max(0, seconds)
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    return min(0.5 * 2 ** min(5, max(0, attempt - 1)), 8)


def _output_description(record: CallRecord) -> str:
    if record.payload.get("output_token_source") == "not_applicable":
        return "not applicable"
    value = record.payload.get("output_tokens")
    if value is None:
        return "unknown"
    qualifier = " (partial)" if record.payload.get("output_completeness") != "complete" else ""
    return f"{value} tokens{qualifier}"


def _failure_event(record: CallRecord, exc: BaseException, stage: str,
                   request_started: float | None, request_finished: float | None) -> None:
    sent = record.payload.get("sent")
    elapsed = (request_finished or time.monotonic()) - request_started if request_started is not None else 0
    number = record.payload.get("send_number", record.payload.get("attempt", 1))
    if not sent:
        summary = f"Request not sent. Failed during {stage}."
    elif isinstance(exc, ModelCallTimeout):
        summary = f"Request {number} timed out after {elapsed:.3f}s."
    elif stage == "output_validation":
        summary = f"Response {number} failed output validation after {elapsed:.3f}s."
    elif isinstance(exc, asyncio.CancelledError):
        summary = f"Request {number} cancelled after {elapsed:.3f}s."
    else:
        status = (record.payload.get("error") or {}).get("http_status")
        reason = f"HTTP {status}" if status is not None else type(exc).__name__
        summary = f"Request {number} failed during {stage} after {elapsed:.3f}s ({reason})."
    input_tokens = record.payload.get("input_tokens")
    input_text = f"{input_tokens} tokens" if input_tokens is not None else "unknown"
    record.event("model_request_failed", summary +
                 f" Input: {input_text}. Output: {_output_description(record)}.")


async def execute_model(*, model_type, model, caller, request, send, validate=None,
                        serialize=None, budget=None, record_options=None, input_guard=None,
                        input_limit=DEFAULT_INPUT_LIMIT):
    """send(remaining) returns (parsed response, HTTP status); validate may repair its prompt.

    Validation failures and HTTP failures consume the same send allowance and
    cumulative slot-wait budget. Each acquired slot starts a new request timeout.
    """
    logical = current_logical_call()
    budget = budget or (logical.budget if logical is not None else ModelCallBudget())
    limit = MODEL_INPUT_LIMITS[model_type] if input_limit is DEFAULT_INPUT_LIMIT else input_limit
    while True:
        budget.task_remaining()
        if budget.max_retries is not None and budget.sends >= budget.max_retries + 1:
            exc = ModelRetryLimitError("Model operation exhausted its shared send/retry allowance.")
            if logical and logical.last_record:
                exc.call_id = logical.last_record.reference_id
                exc.call_path = str(logical.last_record.reference_path) if logical.last_record.reference_path else None
            raise exc
        record = new_call(component=caller, model=model, request=request, **(record_options or {}))
        wait_before = budget.waiting_total_seconds
        request_started = None
        request_finished = None
        backoff_seconds = 0.0
        stage = "input_validation"
        try:
            counted = count_model_input(model_type, model, request)
            largest_input = max(counted.input_token_counts, default=0)
            record.payload.update(model_type=model_type,
                                  input_token_counts=counted.input_token_counts,
                                  max_input_tokens=limit,
                                  input_headroom_tokens=limit - largest_input if limit is not None else None,
                                  input_limit_utilization=largest_input / limit if limit else None)
            if request.get("_input_preparation"):
                record.payload["input_preparation"] = request["_input_preparation"]
            record.record_local_input(counted.input_tokens, counted.metadata)
            if limit is not None and any(value > limit for value in counted.input_token_counts):
                raise ModelInputTooLong(
                    f"{model_type} input exceeds max_input_tokens "
                    f"({max(counted.input_token_counts)} tokens > {limit}); request was not sent",
                    model_type=model_type, input_tokens=max(counted.input_token_counts),
                    limit=limit, caller=caller,
                )
            if input_guard is not None:
                input_guard(record)
            stage = "semaphore_wait"
            record.event("model_admission_wait", "Waiting for model admission.")
            async with model_semaphore(model_type, caller, model, budget=budget) as lease:
                lease.release_event = record.event
                budget.reserve_send()
                stage = "request"
                request_started = time.monotonic()
                timer = None
                try:
                    async with asyncio.timeout(budget.remaining()) as timer:
                        record.payload.update(model_type=model_type, semaphore_key=lease.key,
                                              budget_seconds=budget.timeout_seconds,
                                              remaining_seconds=(budget.remaining()
                                                  if math.isfinite(budget.remaining()) else None),
                                              send_number=budget.sends)
                        if hasattr(lease, "model_type"):
                            record.payload.update(semaphore_metadata(lease))
                        record.sent()
                        record.event("model_request_sent",
                                     f"Request {budget.sends} sent. Input: {record.payload['input_tokens']} tokens. "
                                     f"Wait: {budget.waiting_total_seconds - wait_before:.3f}s.")
                        token = _ACTIVE_CALL.set(ModelRequest(record, budget, timer))
                        try:
                            # Admission reserves RPM atomically with capacity.
                            # Only unused admission is refundable on cancellation.
                            lease.sent = True
                            response, status = await send(budget.remaining())
                        finally:
                            _ACTIVE_CALL.reset(token)
                except TimeoutError as exc:
                    # Inspect the actual asyncio timer before freezing a failed
                    # request. Its source remains authoritative if the loop
                    # wakes one clock tick before the numeric deadline.
                    if timer is not None and timer.expired():
                        raise budget.timeout_error(stage, request_timer_expired=True) from exc
                    raise
                finally:
                    request_finished = time.monotonic()
                    budget.finish_request()
                # Preserve the received status/usage even if releasing the
                # Redis slot fails or the deadline expires during cleanup.
                payload = serialize(response) if serialize else response
                record.succeeded(payload, http_status=status)
                record.event("model_response_received",
                             f"Response {budget.sends} received in {request_finished - request_started:.3f}s. "
                             f"Input: {record.payload['input_tokens']} tokens. Output: {_output_description(record)}.")
                # Synchronous SDK parsing can overrun without yielding to the
                # asyncio timer. Check the frozen request duration as well.
                budget.remaining()
                stage = "semaphore_release"
            budget.task_remaining()
            stage = "output_validation"
            result = validate(response) if validate else response
            budget.task_remaining()
            return result
        except asyncio.CancelledError as exc:
            record.failed(exc, stage=stage)
            _failure_event(record, exc, stage, request_started, request_finished)
            raise
        except Exception as exc:
            if stage == "request" and isinstance(exc, TimeoutError) and not isinstance(exc, ModelCallTimeout):
                exc = budget.timeout_error(stage)
            elif stage == "semaphore_wait" and isinstance(exc, TimeoutError) and not isinstance(exc, ModelCallTimeout):
                exc = ModelSemaphoreTimeout(str(exc))
            # Local validation and retry delays are outside request timers, but
            # they must never hide expiration of the enclosing contract.
            if not isinstance(exc, TaskDeadlineExceeded):
                try:
                    budget.task_remaining()
                except TaskDeadlineExceeded as deadline:
                    exc = deadline
            stage = getattr(exc, "failure_stage", stage)
            record.failed(exc, stage=stage)
            _failure_event(record, exc, stage, request_started, request_finished)
            exc.call_id = record.reference_id
            exc.call_path = str(record.reference_path) if record.reference_path else None
            exc.failure_stage = stage
            if not budget.can_retry or not (stage == "output_validation" and isinstance(exc, ValueError) or stage == "request" and _retryable(exc)):
                raise exc
            delay = 0 if stage == "output_validation" else _retry_delay(exc, budget.sends)
            record.payload["retry_reason"] = type(exc).__name__
            record.payload["retry_delay_seconds"] = min(delay, budget.task_remaining())
            record.event("model_retry_scheduled", f"Retry scheduled in {record.payload['retry_delay_seconds']:.3f}s.")
            backoff_started = time.monotonic()
            try:
                async with asyncio.timeout(budget.task_remaining()):
                    await asyncio.sleep(delay)
            except TimeoutError as timeout:
                error = TaskDeadlineExceeded("Contract execution deadline reached during retry backoff.")
                error.call_id = record.reference_id
                error.call_path = str(record.reference_path) if record.reference_path else None
                record.failed(error, stage=error.failure_stage)
                record.event("model_backoff_failed", "Contract execution deadline reached during retry backoff.")
                raise error from timeout
            except asyncio.CancelledError:
                record.event("model_retry_cancelled", "Retry cancelled during backoff.")
                raise
            finally:
                backoff_seconds = time.monotonic() - backoff_started
        finally:
            now = time.monotonic()
            record.payload["timing"] = {
                "queue_wait_seconds": round(budget.waiting_total_seconds - wait_before, 6),
                "request_seconds": round((request_finished or now) - request_started, 6) if request_started else 0,
                "backoff_seconds": round(backoff_seconds, 6),
                "total_seconds": round(now - budget.started, 6),
                "model_elapsed_seconds": round(budget.model_elapsed_seconds, 6),
                "waiting_total_seconds": round(budget.waiting_total_seconds, 6),
                "wait_remaining_seconds": (round(budget.wait_remaining_seconds, 6)
                                            if math.isfinite(budget.wait_remaining_seconds) else None),
                "remaining_seconds": (round(budget.remaining_seconds, 6)
                                       if math.isfinite(budget.remaining_seconds) else None),
            }
            record.payload["lease_renewals"] = getattr(lease, "renewals", 0) if request_started else 0
            record._write()
