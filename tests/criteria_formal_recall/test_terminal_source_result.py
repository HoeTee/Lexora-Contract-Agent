from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import FileResponse

from endpoints.api.review_jobs import _export_review_job_result
from endpoints.review import task_store
from endpoints.review.response import present_review_task


class TerminalSourceResultTests(unittest.IsolatedAsyncioTestCase):
    def _task(self, status: str, result_path: Path) -> dict:
        return {
            "task_id": "20260920-093706-ed31",
            "status": status,
            "created_at": "2026-09-20T09:37:06+08:00",
            "started_at": None,
            "finished_at": None,
            "message": "test",
            "error": None,
            "criteria": [
                {
                    "id": "H01",
                    "criterion": "测试标准",
                    "execution_status": "completed",
                    "review_result": "compliant",
                    "error": None,
                },
                {
                    "id": "H12",
                    "criterion": "是否有错别字。",
                    "execution_status": "error",
                    "review_result": None,
                    "error": {"type": "ModelCallError", "message": "Request timed out."},
                },
            ],
            "input": {
                "contract_filename": "source.docx",
                "criteria_source": "mandatory",
                "criteria_filename": None,
                "contract_path": str(result_path.parent / "source.docx"),
            },
            "output": {
                "result_filename": result_path.name,
                "result_path": str(result_path),
            },
            "logs": {},
            "criteria_manifest": {"count": 2},
        }

    def test_create_task_stages_source_at_existing_result_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = root / "output" / "reviewed.docx"
            source = result.parent / "source.docx"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"original contract")

            with patch.object(task_store, "DATA_DIR", str(root / "data")):
                task_store.create_task(
                    client_id="client-a",
                    client_dir="client-a",
                    task_id="20260920-093706-ed31",
                    contract_filename=source.name,
                    contract_path=source,
                    criteria_source="mandatory",
                    criteria_filename=None,
                    criteria_path=None,
                    result_filename=result.name,
                    result_path=result,
                )

            self.assertEqual(result.read_bytes(), source.read_bytes())

    def test_failed_and_cancelled_tasks_report_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = Path(temp_dir) / "reviewed.docx"
            result.write_bytes(b"original contract")

            for status in ("failed", "cancelled"):
                with self.subTest(status=status):
                    response = present_review_task(self._task(status, result))
                    self.assertTrue(response["output"]["ready"])

    def test_status_response_keeps_quality_summary_and_all_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = Path(temp_dir) / "reviewed.docx"
            result.write_bytes(b"reviewed")
            response = present_review_task(self._task("succeeded", result))

        self.assertEqual(response["quality"]["total_count"], 2)
        self.assertEqual(response["quality"]["error_criteria"], ["H12"])
        self.assertEqual([item["id"] for item in response["criteria"]], ["H01", "H12"])
        self.assertNotIn("logs", response)
        self.assertNotIn("criteria_manifest", response)

    async def test_result_endpoint_serves_all_terminal_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = Path(temp_dir) / "reviewed.docx"
            result.write_bytes(b"original contract")

            for status in ("succeeded", "failed", "cancelled"):
                with self.subTest(status=status):
                    response = await _export_review_job_result(
                        client_dir="client-a",
                        task=self._task(status, result),
                        output_type="file",
                    )
                    self.assertIsInstance(response, FileResponse)
                    self.assertEqual(Path(response.path), result)

    async def test_result_endpoint_materializes_source_for_legacy_failed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = root / "output" / "reviewed.docx"
            source = result.parent / "source.docx"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"original contract")
            task = self._task("failed", result)

            response = await _export_review_job_result(
                client_dir="client-a",
                task=task,
                output_type="file",
            )

            self.assertIsInstance(response, FileResponse)
            self.assertEqual(result.read_bytes(), source.read_bytes())

    async def test_result_endpoint_still_rejects_active_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = Path(temp_dir) / "reviewed.docx"
            result.write_bytes(b"original contract")

            for status in ("pending", "queued", "running"):
                with self.subTest(status=status):
                    with self.assertRaises(HTTPException) as raised:
                        await _export_review_job_result(
                            client_dir="client-a",
                            task=self._task(status, result),
                            output_type="file",
                        )
                    self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
