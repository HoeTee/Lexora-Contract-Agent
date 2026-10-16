from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from threading import Event, Thread

from endpoints.review import job_worker, task_store
from task_queue.celery_app import celery_app
from task_queue.review_queue import enqueue_review_job
from endpoints.runtime.model_budget import task_execution
from task_queue.review_request import ReviewRequest


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
        self.assertEqual(celery_app.conf.worker_pool, "prefork")
        self.assertIsNone(celery_app.conf.task_soft_time_limit)
        self.assertFalse(celery_app.conf.task_acks_late)

    def test_worker_claim_is_exclusive_and_persists_execution_deadline(self) -> None:
        with TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)):
            task_id = "20260929-120000-abcd"
            task_store.write_task(_task(task_id, status="queued", created_at=task_store.now_iso()))
            with task_execution(1900000000):
                first = task_store.mark_worker_started("test-client", task_id, "worker:1")
                second = task_store.mark_worker_started("test-client", task_id, "worker:2")
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(first["worker"]["execution_deadline"], 1900000000)

    def test_terminal_state_survives_late_heartbeat_and_worker_callbacks(self) -> None:
        with TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)):
            task_id = "20260929-120000-abcd"
            task_store.write_task(_task(task_id, status="queued", created_at=task_store.now_iso()))
            task_store.mark_worker_started("test-client", task_id, "worker:1")
            task_store.mark_running("test-client", task_id)
            task_store.fail_active_task("test-client", task_id, code="TASK_TIME_LIMIT_EXCEEDED", detail="expired")
            self.assertIsNone(task_store.mark_worker_heartbeat("test-client", task_id, "worker:1"))
            self.assertIsNone(task_store.mark_running("test-client", task_id))
            task_store.mark_succeeded("test-client", task_id)
            task_store.mark_failed("test-client", task_id, code="REVIEW_FAILED", message="late exception")
            current = task_store.read_task("test-client", task_id)
            self.assertEqual(current["error"]["code"], "TASK_TIME_LIMIT_EXCEEDED")
            self.assertEqual(current["status"], "failed")

    def test_publish_identity_is_saved_before_fast_worker_accepts(self) -> None:
        with (TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)),
              patch.object(task_store, "LOGGING_ENABLED", False)):
            task_id = "20260929-120000-abcd"
            task_store.write_task(_task(task_id, status="pending", created_at=task_store.now_iso()))

            def publish(*, args, task_id):
                stored = task_store.read_task(*args)
                self.assertEqual(stored["queue"]["task_id"], task_id)
                task_store.mark_worker_started(*args, "worker:1")
                task_store.mark_running(*args)

            with patch("task_queue.review_queue.run_review_job_task.apply_async", side_effect=publish):
                queue_id = enqueue_review_job("test-client", task_id)
            stored = task_store.read_task("test-client", task_id)
            self.assertEqual(stored["status"], "running")
            self.assertEqual(stored["queue"]["task_id"], queue_id)

    def test_failed_publish_is_terminal_and_cannot_be_claimed(self) -> None:
        with (TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)),
              patch.object(task_store, "LOGGING_ENABLED", False)):
            task_id = "20260929-120000-abcd"
            task_store.write_task(_task(task_id, status="pending", created_at=task_store.now_iso()))
            with patch("task_queue.review_queue.run_review_job_task.apply_async", side_effect=ConnectionError("offline")):
                with self.assertRaises(ConnectionError):
                    enqueue_review_job("test-client", task_id)
            self.assertIsNone(task_store.mark_worker_started("test-client", task_id, "worker:1"))
            self.assertEqual(task_store.read_task("test-client", task_id)["error"]["code"], "QUEUE_SUBMIT_FAILED")

    def test_old_request_failure_does_not_fail_different_queue_identity(self) -> None:
        with TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)):
            task_id = "20260929-120000-abcd"
            task = _task(task_id, status="queued", created_at=task_store.now_iso())
            task["queue"]["task_id"] = "new-request"
            task_store.write_task(task)
            self.assertIsNone(task_store.fail_active_task("test-client", task_id, code="WORKER_INTERRUPTED",
                                                         detail="old", queue_id="old-request"))

    def test_parallel_criterion_updates_preserve_all_completed_items(self) -> None:
        with TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)):
            task_id = "20260929-120000-abcd"
            task_store.write_task(_task(task_id, status="running", created_at=task_store.now_iso()))
            with ThreadPoolExecutor(max_workers=8) as pool:
                updates = [pool.submit(task_store.update_task_criterion, "test-client", task_id,
                                       f"H{index}", execution_status="completed") for index in range(16)]
                for update in updates:
                    update.result()
            task = task_store.read_task("test-client", task_id)
            self.assertEqual(task["quality"]["completed_count"], 16)

    def test_deadline_reconciliation_does_not_fail_live_execution(self) -> None:
        with TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)):
            task_id = "20260929-120000-abcd"
            task = _task(task_id, status="running", created_at=task_store.now_iso())
            task["worker"].update(worker_id="worker:1", execution_deadline=1900000000)
            task_store.write_task(task)
            with patch.object(task_store.time, "time", return_value=1899999999):
                self.assertEqual(task_store.expire_executing_tasks(), [])
            with patch.object(task_store.time, "time", return_value=1900000006):
                expired = task_store.expire_executing_tasks()
            self.assertEqual([item["task_id"] for item in expired], [task_id])

    def test_late_failure_callback_does_not_kill_reused_worker_tools(self) -> None:
        with (TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)),
              patch.object(task_store, "LOGGING_ENABLED", False)):
            task_id = "20260929-120000-abcd"
            task = _task(task_id, status="succeeded", created_at=task_store.now_iso())
            task["queue"]["task_id"] = "old-request"
            task_store.write_task(task)
            request = object.__new__(ReviewRequest)
            request._args = ("test-client", task_id)
            request.id = "old-request"
            request.worker_pid = 12345
            with patch("task_queue.review_request.terminate_descendants") as terminate:
                request._fail_business_task("WORKER_INTERRUPTED", "late callback")
            terminate.assert_not_called()

    def test_failure_persistence_does_not_block_the_pool_callback(self) -> None:
        request = object.__new__(ReviewRequest)
        request._args = ("test-client", "20260929-120000-abcd")
        request.id = "blocked-request"
        entered, release, returned = Event(), Event(), Event()

        def persist(*args):
            entered.set()
            release.wait(timeout=5)

        def callback():
            request._schedule_business_failure("TASK_TIME_LIMIT_EXCEEDED", "expired")
            returned.set()

        with patch.object(request, "_fail_business_task", side_effect=persist):
            caller = Thread(target=callback, daemon=True)
            caller.start()
            try:
                self.assertTrue(entered.wait(timeout=1))
                self.assertTrue(returned.wait(timeout=1), "Pool callback waited for task state persistence")
            finally:
                release.set()
                caller.join(timeout=1)

    def test_web_history_is_completed_before_runtime_input_is_removed(self) -> None:
        with (TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", Path(directory)),
              patch.object(task_store, "LOGGING_ENABLED", False), patch.object(job_worker, "LOGGING_ENABLED", False)):
            client_dir, task_id = "web:tenant-a", "20260929-120000-abcd"
            contract = task_store.runtime_input_dir(client_dir, task_id) / "contract.docx"
            contract.parent.mkdir(parents=True)
            contract.write_bytes(b"original contract")
            report = task_store.output_dir(client_dir, task_id) / "contract_reviewed.docx"
            task_store.create_task(client_id="alice", client_dir=client_dir, task_id=task_id,
                                   contract_filename=contract.name, contract_path=contract,
                                   criteria_source="mandatory", criteria_filename=None, criteria_path=None,
                                   result_filename=report.name, result_path=report)
            task_store.update_task(client_dir, task_id, tenant_id="tenant-a", username="alice", display_name="Alice")
            workflow = SimpleNamespace(run=AsyncMock(return_value={"report_docx": str(report), "criteria": []}))
            with patch.object(job_worker, "ContractReviewWorkflow", return_value=workflow):
                asyncio.run(job_worker.run_async_review_job(client_dir, task_id))
            task = task_store.read_task(client_dir, task_id)
            self.assertEqual(task["status"], "succeeded")
            self.assertEqual(task["contract_size_bytes"], len(b"original contract"))
            self.assertEqual(task["report_stored_name"], report.name)
            self.assertEqual(task["username"], "alice")
            self.assertIsNone(task["criteria_original_name"])
            self.assertFalse(contract.exists())
            self.assertTrue(report.exists())

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
