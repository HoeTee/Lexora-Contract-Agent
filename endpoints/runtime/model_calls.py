"""Shared network deadline, slot acquisition, retries and attempt accounting."""
from __future__ import annotations

import asyncio
import math
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx
from openai import APIConnectionError

from endpoints.runtime.llm_semaphore import model_semaphore
from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout, ModelRetryLimitError
from loggers.call_log import current_logical_call, new_call


def _retryable(exc: Exception) -> bool:
    status = (getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
              or getattr(getattr(exc, "response", None), "status_code", None))
    return (isinstance(exc, (APIConnectionError, httpx.TransportError))
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
    return min(0.5 * 2 ** max(0, attempt - 1), 8)


async def execute_model(*, model_type, model, caller, request, send, validate=None,
                        serialize=None, budget=None, record_options=None):
    """send(remaining) returns (parsed response, HTTP status); validate may repair its prompt.

    Validation failures and HTTP failures consume the same sends on the logical
    call. Slot waiting and retry backoff cannot reset the operation deadline.
    """
    logical = current_logical_call()
    budget = budget or (logical.budget if logical is not None else ModelCallBudget())
    while True:
        budget.remaining()
        if budget.sends >= budget.max_retries + 1:
            exc = ModelRetryLimitError("Model operation exhausted its shared send/retry allowance.")
            if logical and logical.last_record:
                exc.call_id = logical.last_record.reference_id
                exc.call_path = str(logical.last_record.reference_path) if logical.last_record.reference_path else None
            raise exc
        record = new_call(component=caller, model=model, request=request, **(record_options or {}))
        queue_started = time.monotonic()
        request_started = None
        request_finished = None
        backoff_seconds = 0.0
        stage = "semaphore_wait"
        try:
            async with asyncio.timeout(budget.remaining()):
                async with model_semaphore(model_type, caller, model, budget=budget) as lease:
                    budget.reserve_send()
                    stage = "request"
                    request_started = time.monotonic()
                    record.payload.update(model_type=model_type, semaphore_key=lease.key,
                                          budget_seconds=budget.timeout_seconds,
                                          remaining_seconds=budget.remaining(), send_number=budget.sends)
                    record.sent()
                    try:
                        response, status = await send(budget.remaining())
                    finally:
                        request_finished = time.monotonic()
                    # Preserve the received status/usage even if releasing the
                    # Redis slot fails or the deadline expires during cleanup.
                    payload = serialize(response) if serialize else response
                    record.succeeded(payload, http_status=status)
                    stage = "semaphore_release"
                stage = "output_validation"
                result = validate(response) if validate else response
                budget.remaining()
                return result
        except asyncio.CancelledError as exc:
            record.failed(exc, stage=stage)
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                expired = ModelCallTimeout(f"{model_type} operation exceeded its shared deadline during {stage}.")
                expired.failure_stage = stage
                exc = expired
            record.failed(exc, stage=stage)
            exc.call_id = record.reference_id
            exc.call_path = str(record.reference_path) if record.reference_path else None
            exc.failure_stage = stage
            if not budget.can_retry or not (stage == "output_validation" and isinstance(exc, ValueError) or stage == "request" and _retryable(exc)):
                raise exc
            delay = 0 if stage == "output_validation" else _retry_delay(exc, budget.sends)
            record.payload["retry_reason"] = type(exc).__name__
            record.payload["retry_delay_seconds"] = min(delay, budget.remaining())
            backoff_started = time.monotonic()
            try:
                async with asyncio.timeout(budget.remaining()):
                    await asyncio.sleep(delay)
            except TimeoutError as timeout:
                error = ModelCallTimeout("Model operation expired during retry backoff.")
                error.failure_stage = "retry_backoff"
                error.call_id = record.reference_id
                error.call_path = str(record.reference_path) if record.reference_path else None
                record.failed(error, stage="retry_backoff")
                raise error from timeout
            finally:
                backoff_seconds = time.monotonic() - backoff_started
        finally:
            now = time.monotonic()
            record.payload["timing"] = {
                "queue_wait_seconds": round((request_started or now) - queue_started, 6),
                "request_seconds": round((request_finished or now) - request_started, 6) if request_started else 0,
                "backoff_seconds": round(backoff_seconds, 6),
                "total_seconds": round(now - budget.started, 6),
                "remaining_seconds": round(max(0, budget.deadline - now), 6),
            }
            record._write()
