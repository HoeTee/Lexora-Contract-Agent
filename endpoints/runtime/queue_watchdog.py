from __future__ import annotations

import asyncio
import logging

from config import (
    QUEUE_PENDING_TIMEOUT_SECONDS,
    QUEUE_QUEUED_TIMEOUT_SECONDS,
    QUEUE_WATCHDOG_INTERVAL_SECONDS,
)
from endpoints.review.task_store import expire_stale_waiting_tasks


logger = logging.getLogger(__name__)


async def run_queue_watchdog() -> None:
    interval = max(1.0, QUEUE_WATCHDOG_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(interval)
        try:
            expired = await asyncio.to_thread(
                expire_stale_waiting_tasks,
                pending_timeout_seconds=QUEUE_PENDING_TIMEOUT_SECONDS,
                queued_timeout_seconds=QUEUE_QUEUED_TIMEOUT_SECONDS,
            )
            if expired:
                logger.warning("Marked %s stale waiting review task(s) as failed", len(expired))
        except Exception:
            logger.exception("Queue timeout watchdog failed")
