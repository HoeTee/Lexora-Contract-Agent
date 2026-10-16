"""Parent-process failure handling survives a killed task execution process."""
from __future__ import annotations

import logging
import os
import signal
from contextlib import suppress
from pathlib import Path
from threading import Thread

from billiard.exceptions import TimeLimitExceeded
from celery.worker.request import Request

from endpoints.review.task_store import fail_active_task, write_task_log_event

logger = logging.getLogger(__name__)


def terminate_descendants(pid: int | None) -> None:
    if pid is None or os.name != "posix" or not Path("/proc").exists():
        return
    children = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    def terminate(parent: int) -> None:
        for child in children.get(parent, []):
            terminate(child)
            with suppress(ProcessLookupError, PermissionError):
                os.kill(child, signal.SIGKILL)
    terminate(pid)


class ReviewRequest(Request):
    def _fail_business_task(self, code: str, detail: str) -> None:
        if len(self.args) != 2:
            return
        client_dir, task_id = self.args
        try:
            updated = fail_active_task(client_dir, task_id, code=code, detail=detail, queue_id=self.id)
            if updated:
                write_task_log_event(client_dir, task_id, "review_worker_failed", code=code, detail=detail)
        except Exception:
            logger.exception("Could not persist worker failure for %s", self.id)

    def _schedule_business_failure(self, code: str, detail: str) -> None:
        if getattr(self, "_business_failure_scheduled", False):
            return
        self._business_failure_scheduled = True
        # Billiard invokes callbacks before killing the child. Never wait here for
        # task.json: the child may hold its lock until Billiard terminates it.
        try:
            Thread(target=self._fail_business_task, args=(code, detail), daemon=True,
                   name=f"review-failure-{self.id[:8]}").start()
        except RuntimeError:
            logger.exception("Could not schedule worker failure persistence for %s", self.id)

    def on_timeout(self, soft, timeout):
        if not soft:
            # This callback belongs to the pool's current timed-out job, immediately
            # before Billiard kills its child. Generic/delayed failure callbacks must
            # never kill by old worker_pid, which may already be executing another job.
            terminate_descendants(self.worker_pid)
            self._schedule_business_failure("TASK_TIME_LIMIT_EXCEEDED", f"Contract execution exceeded {timeout} seconds.")
        super().on_timeout(soft, timeout)

    def on_failure(self, exc_info, send_failed_event=True, return_ok=False):
        exc = exc_info.exception
        underlying = getattr(exc, "exc", exc)
        code = "TASK_TIME_LIMIT_EXCEEDED" if isinstance(underlying, TimeLimitExceeded) else "WORKER_INTERRUPTED"
        self._schedule_business_failure(code, str(underlying))
        super().on_failure(exc_info, send_failed_event=send_failed_event, return_ok=return_ok)
