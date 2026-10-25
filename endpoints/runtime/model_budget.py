"""Share retry and slot-wait limits while timing each HTTP request separately."""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock, Thread
from typing import Any, Coroutine

from config import MODEL_CALL_MAX_RETRIES, MODEL_CALL_TIMEOUT_SECONDS, MODEL_WAIT_TIMEOUT_SECONDS


class ModelCallTimeout(TimeoutError):
    """A request exceeded its total time or its streaming content-idle time."""


class ModelSemaphoreTimeout(ModelCallTimeout):
    """The logical operation's cumulative slot-wait budget has expired."""


class TaskDeadlineExceeded(ModelCallTimeout):
    """The enclosing contract task has reached its execution deadline."""

    failure_stage = "task_deadline"


class ModelRetryLimitError(RuntimeError):
    """A schema repair or transport retry exhausted the shared allowance."""


_task_deadline: ContextVar[float | None] = ContextVar("contract_task_deadline", default=None)
_runner_lock = Lock()
_runner_loop: asyncio.AbstractEventLoop | None = None
_runner_pid: int | None = None


def task_deadline() -> float | None:
    value = _task_deadline.get()
    if value is not None:
        return value
    raw = os.getenv("CONTRACT_TASK_DEADLINE")
    return float(raw) if raw else None


@contextmanager
def task_execution(deadline: float | None, task_id: str | None = None):
    token = _task_deadline.set(deadline)
    previous = os.environ.get("CONTRACT_TASK_DEADLINE")
    previous_id = os.environ.get("CONTRACT_TASK_ID")
    if task_id is not None:
        os.environ["CONTRACT_TASK_ID"] = task_id
    if deadline is None:
        os.environ.pop("CONTRACT_TASK_DEADLINE", None)
    else:
        os.environ["CONTRACT_TASK_DEADLINE"] = str(deadline)
    try:
        yield
    finally:
        _task_deadline.reset(token)
        if previous_id is None:
            os.environ.pop("CONTRACT_TASK_ID", None)
        else:
            os.environ["CONTRACT_TASK_ID"] = previous_id
        if previous is None:
            os.environ.pop("CONTRACT_TASK_DEADLINE", None)
        else:
            os.environ["CONTRACT_TASK_DEADLINE"] = previous


@dataclass
class ModelCallBudget:
    timeout_seconds: float | None = MODEL_CALL_TIMEOUT_SECONDS
    max_retries: int | None = MODEL_CALL_MAX_RETRIES
    started: float = field(default_factory=time.monotonic)
    sends: int = 0
    deadline: float | None = field(default=None, init=False)
    wait_timeout_seconds: float | None = MODEL_WAIT_TIMEOUT_SECONDS
    wait_seconds: float = field(default=0.0, init=False)
    _task_deadline: float | None = field(default=None, init=False, repr=False)
    _waiting_since: float | None = field(default=None, init=False, repr=False)
    _request_started: float | None = field(default=None, init=False, repr=False)
    _request_finished: float | None = field(default=None, init=False, repr=False)
    _request_seconds: float = field(default=0.0, init=False, repr=False)
    streaming: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        outer = task_deadline()
        if outer is not None:
            self._task_deadline = self.started + max(0, outer - time.time())

    def _task_remaining(self, now: float) -> float:
        remaining = self._task_deadline - now if self._task_deadline is not None else float("inf")
        if remaining <= 0:
            raise TaskDeadlineExceeded("Contract task reached its execution deadline.")
        return remaining

    def task_remaining(self) -> float:
        """Check the contract deadline without consulting a previous request."""
        return self._task_remaining(time.monotonic())

    @property
    def model_remaining_seconds(self) -> float:
        if self.deadline is None:
            return self.timeout_seconds if self.timeout_seconds is not None else float("inf")
        now = self._request_finished if self._request_finished is not None else time.monotonic()
        return max(0.0, self.deadline - now)

    @property
    def model_elapsed_seconds(self) -> float:
        active = self.request_elapsed_seconds if self._request_finished is None else 0.0
        return self._request_seconds + active

    @property
    def request_elapsed_seconds(self) -> float:
        if self._request_started is None:
            return 0.0
        now = self._request_finished if self._request_finished is not None else time.monotonic()
        return max(0.0, now - self._request_started)

    @property
    def waiting_total_seconds(self) -> float:
        pending = time.monotonic() - self._waiting_since if self._waiting_since is not None else 0.0
        return self.wait_seconds + pending

    @property
    def wait_remaining_seconds(self) -> float:
        if self.wait_timeout_seconds is None:
            return float("inf")
        return max(0.0, self.wait_timeout_seconds - self.waiting_total_seconds)

    @property
    def remaining_seconds(self) -> float:
        task_remaining = (max(0.0, self._task_deadline - time.monotonic())
                          if self._task_deadline is not None else float("inf"))
        return min(self.model_remaining_seconds, task_remaining)

    @contextmanager
    def waiting(self):
        """Accumulate slot waiting without starting or extending a request timer."""
        self.task_remaining()
        if self._waiting_since is not None:
            raise RuntimeError("A logical model operation cannot acquire multiple slots at once.")
        self._waiting_since = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - self._waiting_since
            self.wait_seconds += elapsed
            self._waiting_since = None

    def waiting_remaining(self) -> float:
        task_remaining = self._task_remaining(time.monotonic())
        remaining = self.wait_remaining_seconds
        if remaining <= 0:
            raise ModelSemaphoreTimeout("Model operation exhausted its cumulative slot-wait budget.")
        return min(remaining, task_remaining)

    def remaining(self) -> float:
        task_remaining = self._task_remaining(time.monotonic())
        remaining = self.model_remaining_seconds
        if remaining <= 0:
            raise ModelCallTimeout("Model HTTP request reached its execution deadline.")
        return min(remaining, task_remaining)

    def reserve_send(self) -> None:
        self.task_remaining()
        if self.max_retries is not None and self.sends >= self.max_retries + 1:
            raise ModelRetryLimitError("Model operation exhausted its shared send/retry allowance.")
        if self._request_started is not None and self._request_finished is None:
            raise RuntimeError("A model operation cannot send overlapping HTTP requests.")
        self.sends += 1
        self._request_started = time.monotonic()
        self._request_finished = None
        self.streaming = False
        self.deadline = (self._request_started + self.timeout_seconds
                         if self.timeout_seconds is not None else None)

    def received_content(self) -> None:
        """Extend a streaming request only while its current deadline is valid."""
        self.remaining()
        self.deadline = (time.monotonic() + self.timeout_seconds
                         if self.timeout_seconds is not None else None)

    def finish_request(self) -> None:
        """Freeze this request's duration before releasing its slot or validating."""
        if self._request_started is not None and self._request_finished is None:
            self._request_finished = time.monotonic()
            self._request_seconds += self.request_elapsed_seconds

    @property
    def can_retry(self) -> bool:
        task_remaining = (self._task_deadline - time.monotonic()
                          if self._task_deadline is not None else float("inf"))
        return (self.max_retries is None or self.sends < self.max_retries + 1) and task_remaining > 0

    def timeout_error(self, stage: str, *, request_timer_expired: bool = False) -> ModelCallTimeout:
        """Classify the expired timer by its limiting deadline, not clock rounding.

        asyncio may wake one clock tick before the arithmetic deadline. Once its
        timer has expired, the earlier limit identifies the source. A completed
        request has frozen its model timer; optional trailer I/O is task-bound.
        Call this before finish_request() freezes a failed HTTP attempt.
        """
        task_limited = (self._task_deadline is not None and
                        (self._request_finished is not None or self.deadline is None
                         or self._task_deadline <= self.deadline))
        if self._task_deadline is not None and (
                time.monotonic() >= self._task_deadline or request_timer_expired and task_limited):
            return TaskDeadlineExceeded("Contract task reached its execution deadline.")
        if self.streaming and self.timeout_seconds is not None:
            return ModelCallTimeout(f"LLM stream received no new content for {self.timeout_seconds:g} seconds during {stage}.")
        return ModelCallTimeout(f"Model HTTP request reached its execution deadline during {stage}.")


def run_sync(coroutine: Coroutine[Any, Any, Any]) -> Any:
    """Run cancellable async I/O for synchronous callers, including callers in a loop.

    The operation enforces its own deadline inside this loop. Returning from a
    future timeout without cancelling the network request is deliberately avoided.
    """
    global _runner_loop, _runner_pid
    with _runner_lock:
        if _runner_pid != os.getpid() or _runner_loop is None or not _runner_loop.is_running():
            ready: Future[asyncio.AbstractEventLoop] = Future()

            def serve() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set_result, loop)
                loop.run_forever()

            Thread(target=serve, name="model-io", daemon=True).start()
            _runner_loop = ready.result()
            _runner_pid = os.getpid()
        loop = _runner_loop
    try:
        caller_loop = asyncio.get_running_loop()
    except RuntimeError:
        caller_loop = None
    if caller_loop is loop:
        coroutine.close()
        raise RuntimeError("Synchronous model adapter cannot block its own network loop.")
    return asyncio.run_coroutine_threadsafe(coroutine, loop).result()


def _reset_runner_after_fork():
    global _runner_lock, _runner_loop, _runner_pid
    _runner_lock = Lock()
    _runner_loop = None
    _runner_pid = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_runner_after_fork)
