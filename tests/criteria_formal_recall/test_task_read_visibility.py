from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints.api import review_jobs
from endpoints.api.client_mapping import ApiClient
from endpoints.review import task_store


class TaskReadVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_patch = patch.object(task_store, "DATA_DIR", Path(self.directory.name))
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.client_dir = "test-client"
        self.task_id = "20261026-004057-86cd"
        self.path = task_store.task_json_path(self.client_dir, self.task_id)
        self.task = {
            "task_id": self.task_id,
            "client_dir": self.client_dir,
            "status": "queued",
            "created_at": task_store.now_iso(),
            "started_at": None,
            "finished_at": None,
            "message": "Queued.",
            "error": None,
            "criteria": [],
            "input": {
                "contract_path": str(self.path.parent / "input" / "contract.docx"),
                "contract_filename": "contract.docx",
                "criteria_source": "default",
                "criteria_filename": None,
            },
            "output": {
                "result_path": str(self.path.parent / "output" / "result.docx"),
                "result_filename": "result.docx",
            },
        }
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.task), encoding="utf-8")

    def test_missing_once_then_visible_returns_task(self) -> None:
        with patch.object(Path, "read_text", side_effect=[FileNotFoundError(), json.dumps(self.task)]) as read, \
                patch.object(task_store.time, "sleep") as sleep:
            self.assertEqual(task_store.read_task(self.client_dir, self.task_id), self.task)
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_continuously_missing_returns_none_after_three_reads(self) -> None:
        with patch.object(Path, "read_text", side_effect=FileNotFoundError()) as read, \
                patch.object(task_store.time, "sleep") as sleep:
            self.assertIsNone(task_store.read_task(self.client_dir, self.task_id))
        self.assertEqual(read.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.args == (0.01,) for call in sleep.call_args_list))

    def test_valid_file_reads_directly_without_exists_or_sleep(self) -> None:
        with patch.object(Path, "exists", side_effect=AssertionError("read_task must read directly")), \
                patch.object(task_store.time, "sleep") as sleep:
            self.assertEqual(task_store.read_task(self.client_dir, self.task_id), self.task)
        sleep.assert_not_called()

    def test_invalid_json_propagates_without_retry(self) -> None:
        with patch.object(Path, "read_text", return_value="{") as read, \
                patch.object(task_store.time, "sleep") as sleep:
            with self.assertRaises(json.JSONDecodeError):
                task_store.read_task(self.client_dir, self.task_id)
        read.assert_called_once_with(encoding="utf-8")
        sleep.assert_not_called()

    def test_other_io_errors_propagate_without_retry(self) -> None:
        for error in (PermissionError("denied"), OSError("damaged filesystem")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(Path, "read_text", side_effect=error) as read, \
                    patch.object(task_store.time, "sleep") as sleep:
                with self.assertRaises(type(error)):
                    task_store.read_task(self.client_dir, self.task_id)
                read.assert_called_once_with(encoding="utf-8")
                sleep.assert_not_called()

    def test_different_api_client_cannot_read_shared_task_path(self) -> None:
        self.assertIsNone(task_store.read_task("other-client", self.task_id))
        self.assertEqual(task_store.read_task(self.client_dir, self.task_id), self.task)

    def test_invalid_client_dir_is_rejected_before_reading(self) -> None:
        with patch.object(Path, "read_text") as read:
            with self.assertRaises(ValueError):
                task_store.read_task("../other-client", self.task_id)
        read.assert_not_called()

    def test_status_api_recovers_from_transient_missing_task_file(self) -> None:
        app = FastAPI()
        app.include_router(review_jobs.api_jobs_router)
        api_client = ApiClient("test-client", self.client_dir, "127.0.0.1")
        with TestClient(app, raise_server_exceptions=False) as client, \
                patch.object(review_jobs, "resolve_api_client", AsyncMock(return_value=api_client)), \
                patch.object(Path, "read_text", side_effect=[FileNotFoundError(), json.dumps(self.task)]), \
                patch.object(task_store.time, "sleep"):
            response = client.post("/api/review/jobs/status", json={"task_id": self.task_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_id"], self.task_id)
        self.assertEqual(response.json()["status"], "queued")


if __name__ == "__main__":
    unittest.main()
