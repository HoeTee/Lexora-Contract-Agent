from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from endpoints.review import task_store
from task_queue.celery_app import celery_app


def _task(task_id: str, *, status: str, created_at: str, queued_at: str | None = None) -> dict:
    return {
        "client_id": "test-client",
        "client_dir": "test-client",
        "task_id": task_id,
        "status": status,
        "created_at": created_at,
        "queued_at": queued_at,
        "started_at": None,
        "finished_at": None,
        "message": "test",
        "error": None,
        "criteria": [],
        "quality": task_store.summarize_criteria([]),
        "worker": {
            "worker_id": None,
            "worker_started_at": None,
            "heartbeat_at": None,
            "interrupted_at": None,
        },
        "queue": {"task_id": None},
    }


class QueueControlTests(unittest.TestCase):
    def test_celery_disables_redis_prefetch(self) -> None:
        self.assertTrue(celery_app.conf.worker_disable_prefetch)
        self.assertEqual(celery_app.conf.worker_prefetch_multiplier, 1)

    def test_mark_queued_does_not_overwrite_running_task(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory,
            patch.object(task_store, "DATA_DIR", Path(temporary_directory)),
            patch.object(task_store, "LOGGING_ENABLED", False),
        ):
            created_at = datetime.now(task_store.BEIJING_TZ).isoformat(timespec="seconds")
            task_store.write_task(_task("20260929-120000-abcd", status="pending", created_at=created_at))

            queued = task_store.mark_queued("test-client", "20260929-120000-abcd", "celery-id")
            self.assertIsNotNone(queued)
            assert queued is not None
            self.assertEqual(queued["status"], "queued")
            self.assertIsNotNone(queued["queued_at"])
            self.assertEqual(queued["queue"]["task_id"], "celery-id")

            task_store.mark_running("test-client", "20260929-120000-abcd")
            self.assertIsNone(task_store.mark_queued("test-client", "20260929-120000-abcd", "late-id"))
            current = task_store.read_task("test-client", "20260929-120000-abcd")
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["queue"]["task_id"], "celery-id")

    def test_waiting_timeout_skips_task_claimed_by_worker(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory,
            patch.object(task_store, "DATA_DIR", Path(temporary_directory)),
            patch.object(task_store, "LOGGING_ENABLED", False),
        ):
            now = datetime.now(task_store.BEIJING_TZ)
            old = (now - timedelta(minutes=5)).isoformat(timespec="seconds")

            stale = _task("20260929-120001-abcd", status="queued", created_at=old, queued_at=old)
            claimed = _task("20260929-120002-abcd", status="queued", created_at=old, queued_at=old)
            claimed["worker"]["worker_id"] = "worker:1"
            task_store.write_task(stale)
            task_store.write_task(claimed)

            expired = task_store.expire_stale_waiting_tasks(
                pending_timeout_seconds=60,
                queued_timeout_seconds=60,
                current_time=now,
            )

            self.assertEqual([item["task_id"] for item in expired], ["20260929-120001-abcd"])
            failed = task_store.read_task("test-client", "20260929-120001-abcd")
            still_queued = task_store.read_task("test-client", "20260929-120002-abcd")
            self.assertIsNotNone(failed)
            self.assertIsNotNone(still_queued)
            assert failed is not None and still_queued is not None
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"]["code"], "QUEUE_WAIT_TIMEOUT")
            self.assertEqual(still_queued["status"], "queued")


if __name__ == "__main__":
    unittest.main()
