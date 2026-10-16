"""One deadline and send allowance per logical model operation."""

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

from config import MODEL_CALL_MAX_RETRIES, MODEL_CALL_TIMEOUT_SECONDS


class ModelCallTimeout(TimeoutError):
    """The shared operation deadline has expired."""


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
def task_execution(deadline: float, task_id: str | None = None):
    token = _task_deadline.set(deadline)
    previous = os.environ.get("CONTRACT_TASK_DEADLINE")
    previous_id = os.environ.get("CONTRACT_TASK_ID")
    if task_id is not None:
        os.environ["CONTRACT_TASK_ID"] = task_id
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
    timeout_seconds: float = MODEL_CALL_TIMEOUT_SECONDS
    max_retries: int = MODEL_CALL_MAX_RETRIES
    started: float = field(default_factory=time.monotonic)
    sends: int = 0
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        self.deadline = self.started + self.timeout_seconds
        outer = task_deadline()
        if outer is not None:
            self.deadline = min(self.deadline, self.started + max(0, outer - time.time()))

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ModelCallTimeout("Model operation exhausted its shared time budget.")
        return remaining

    def reserve_send(self) -> None:
        self.remaining()
        if self.sends >= self.max_retries + 1:
            raise ModelRetryLimitError("Model operation exhausted its shared send/retry allowance.")
        self.sends += 1

    @property
    def can_retry(self) -> bool:
        return self.sends < self.max_retries + 1 and self.deadline > time.monotonic()


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
