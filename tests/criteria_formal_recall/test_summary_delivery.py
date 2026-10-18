"""Known summary failures preserve reviewed content without hiding review failures."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree
from zipfile import ZipFile

from docx import Document

from agents.json_utils import AgentOutputValidationError
from endpoints.api.review_jobs import _export_review_job_result
from endpoints.review import job_worker, task_store
from endpoints.review.response import present_review_task
from endpoints.runtime.errors import ModelCallError
from loggers import api_event_logger
from tools.document.reporting.docx_report import DocxReportGenerator
from workflow.workflow import ContractReviewWorkflow


def reviewed_results():
    return [{
        "criterion_id": "U01", "criterion": "检查日期", "section": "签署",
        "status": "ISSUES_FOUND", "quality_status": "passed",
        "issues": [{
            "issue_id": "U01.1", "issue_comment": "请补充签署日期。", "risk_level": "low",
            "check_point": "日期应完整", "anchors": [{
                "role": "edit", "xml_anchor_type": "paragraph", "xml_anchor_id": "path:body/p1",
                "quoted_text": "签署日期：", "comment_text": "请补充签署日期。",
            }],
        }],
    }, {
        "criterion_id": "H04", "criterion": "检查标的物", "section": "合同框架",
        "status": "ERROR", "quality_status": "error", "issues": [],
        "error_type": "ModelCallError", "error_message": "Request timed out.",
    }]


class SummaryDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.docx"
        document = Document()
        document.add_paragraph("签署日期：")
        document.save(self.source)
        self.report = self.root / "reviewed.docx"
        self.results = reviewed_results()
        self.outputs = self.root / "review_outputs.json"
        self.events = self.root / "events.log"
        self.workflow = ContractReviewWorkflow(
            review_outputs_path=str(self.outputs), api_events_path=str(self.events),
        )
        self.workflow.client = SimpleNamespace(connect=AsyncMock(), cleanup=AsyncMock())
        self.workflow._phase_ingest = AsyncMock(return_value=("", "签署日期："))
        self.workflow._phase_build_index = AsyncMock(return_value={})
        self.workflow._phase_plan = AsyncMock(return_value=[
            {"id": result["criterion_id"], "criterion": result["criterion"]}
            for result in self.results
        ])
        self.workflow._phase_execute = AsyncMock(return_value=copy.deepcopy(self.results))

        async def generate(**values):
            return DocxReportGenerator.generate_annotated_docx(
                values["contract_path"], values["results"], values["output_path"],
                summary_sections=values["summary_sections"],
            )

        self.workflow._phase_generate_annotated_docx = AsyncMock(side_effect=generate)
        self.progress = []
        for target in (
            "workflow.workflow.LOGGING_ENABLED", "loggers.workflow_logger.LOGGING_ENABLED",
            "loggers.workflow_logger.LOGGING_DETAILED",
        ):
            patcher = patch(target, True)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_workflow(self):
        return await self.workflow.run(
            str(self.source), output_path=str(self.report), progress_callback=self.progress.append,
        )

    def tearDown(self):
        logger = api_event_logger._LOGGERS.pop(str(self.events.resolve()), None)
        if logger is not None:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

    async def test_known_summary_failure_keeps_comments_error_criterion_and_disclosure(self):
        async def fail_summary(_results):
            saved = json.loads(self.outputs.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["issues"][0]["issue_comment"], "请补充签署日期。")
            raise AgentOutputValidationError("Summary output did not match the schema.")

        self.workflow._phase_summarize = AsyncMock(side_effect=fail_summary)
        result = await self.run_workflow()

        self.assertEqual(result["summary"]["status"], "fallback")
        self.assertEqual(result["summary"]["error"]["type"], "AgentOutputValidationError")
        self.assertEqual(result["quality"]["error_criteria"], ["H04"])
        self.assertEqual(result["criteria"][1]["execution_status"], "error")
        self.assertIsNone(result["criteria"][1]["review_result"])
        self.assertTrue(any(update.get("summary", {}).get("status") == "fallback" for update in self.progress))
        self.assertIn('"status":"fallback"', self.events.read_text(encoding="utf-8"))
        saved = json.loads(self.outputs.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["issues"][0]["issue_comment"], "请补充签署日期。")
        self.assertEqual(saved[1]["status"], "ERROR")
        with ZipFile(self.report) as package:
            comments = ElementTree.fromstring(package.read("word/comments.xml"))
        text = "".join(comments.itertext())
        self.assertIn("自动总结未生成", text)
        self.assertIn("审查统计", text)
        self.assertIn("1项失败", text)
        self.assertIn("请补充签署日期", text)
        self.assertIn("未完成审查项", text)
        self.assertIn("检查标的物", text)

    async def test_summary_model_failure_uses_same_explicit_fallback(self):
        self.workflow._phase_summarize = AsyncMock(side_effect=ModelCallError("agent", "Summary timed out."))
        result = await self.run_workflow()
        self.assertEqual(result["summary"]["status"], "fallback")
        self.assertEqual(result["summary"]["error"]["type"], "ModelCallError")

    async def test_unknown_summary_exception_propagates_after_reviews_are_saved(self):
        self.workflow._phase_summarize = AsyncMock(side_effect=RuntimeError("Unexpected implementation defect"))
        with self.assertRaisesRegex(RuntimeError, "implementation defect"):
            await self.run_workflow()
        self.assertTrue(self.outputs.exists())
        self.workflow._phase_generate_annotated_docx.assert_not_awaited()
        self.workflow.client.cleanup.assert_awaited_once()

    async def test_contract_deadline_is_not_reclassified_as_summary_only_failure(self):
        error = ModelCallError("agent", "Contract execution deadline expired", error_type="TaskDeadlineExceeded")
        error.failure_stage = "task_deadline"
        self.workflow._phase_summarize = AsyncMock(side_effect=error)
        with self.assertRaises(ModelCallError):
            await self.run_workflow()
        self.assertTrue(self.outputs.exists())
        self.workflow._phase_generate_annotated_docx.assert_not_awaited()

    async def test_review_log_write_failure_is_not_summary_fallback(self):
        self.workflow._phase_summarize = AsyncMock()
        with patch.object(self.workflow, "_save_review_outputs", side_effect=PermissionError("Review log unavailable")):
            with self.assertRaises(PermissionError):
                await self.run_workflow()
        self.workflow._phase_summarize.assert_not_awaited()

    async def test_docx_failure_propagates_even_after_known_summary_fallback(self):
        self.workflow._phase_summarize = AsyncMock(side_effect=AgentOutputValidationError("Invalid summary"))
        self.workflow._phase_generate_annotated_docx.side_effect = OSError("Cannot write DOCX")
        with self.assertRaisesRegex(OSError, "Cannot write DOCX"):
            await self.run_workflow()
        self.assertTrue(self.outputs.exists())


class SummaryTaskStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_persists_fallback_without_marking_failed_criterion_complete(self):
        with TemporaryDirectory() as directory, patch.object(task_store, "DATA_DIR", directory), \
                patch.object(task_store, "LOGGING_ENABLED", False):
            root = Path(directory)
            source = root / "source.docx"
            source.write_bytes(b"source")
            report = root / "reviewed.docx"
            client_dir, task_id = "client", "20261020-120000-a123"
            task_store.create_task(
                client_id="client", client_dir=client_dir, task_id=task_id,
                contract_filename=source.name, contract_path=source,
                criteria_source="mandatory", criteria_filename=None, criteria_path=None,
                result_filename=report.name, result_path=report,
            )
            summary = {"status": "fallback", "error": {"type": "AgentOutputValidationError", "message": "Invalid summary"}}
            criteria = [
                {"id": "U01", "execution_status": "completed", "review_result": "issues_found", "error": None},
                {"id": "H04", "execution_status": "error", "review_result": None,
                 "error": {"type": "ModelCallError", "message": "Timed out"}},
            ]

            async def run(**values):
                await values["progress_callback"]({"stage": "summarizing", "summary": summary})
                self.assertEqual(task_store.read_task(client_dir, task_id)["summary"], summary)
                return {"report_docx": str(report), "criteria": criteria, "summary": summary}

            with patch.object(job_worker, "ContractReviewWorkflow", return_value=SimpleNamespace(run=run)):
                await job_worker.run_async_review_job(client_dir, task_id)
            response = present_review_task(task_store.read_task(client_dir, task_id))
            self.assertEqual(response["status"], "succeeded")
            self.assertEqual(response["summary"], summary)
            self.assertEqual(response["quality"]["error_criteria"], ["H04"])
            self.assertIn("1项失败", response["message"])
            self.assertIn("自动总结未生成", response["message"])
            self.assertEqual(response["output"]["kind"], "report")

    async def test_failed_download_returns_source_identity_and_overwrites_partial_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, result = root / "合同原件.docx", root / "【已AI审查】合同原件.docx"
            source.write_bytes(b"original")
            result.write_bytes(b"partial report")
            task = {
                "task_id": "20261020-120000-a123", "status": "failed",
                "input": {"contract_path": str(source), "contract_filename": source.name},
                "output": {"result_path": str(result), "result_filename": result.name},
            }
            response = await _export_review_job_result(client_dir="client", task=task, output_type="file")
            self.assertEqual(response.filename, source.name)
            self.assertEqual(response.headers["X-Review-Output-Kind"], "source")
            self.assertEqual(result.read_bytes(), b"original")

    async def test_failed_url_upload_uses_source_filename_and_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, result = root / "source.docx", root / "reviewed.docx"
            source.write_bytes(b"original")
            task = {
                "task_id": "20261020-120000-a123", "status": "failed",
                "input": {"contract_path": str(source), "contract_filename": source.name},
                "output": {"result_path": str(result), "result_filename": result.name},
            }
            upload = AsyncMock(return_value="https://files.test/source.docx")
            with patch("endpoints.api.review_jobs.API_RESULT_UPLOAD_ENABLED", True), \
                    patch("endpoints.api.review_jobs.upload_result_file", upload), \
                    patch("endpoints.api.review_jobs.write_task_log_event"):
                response = await _export_review_job_result(client_dir="client", task=task, output_type="url")
            upload.assert_awaited_once_with(result, "source.docx")
            payload = json.loads(response.body)
            self.assertEqual(payload["filename"], "source.docx")
            self.assertEqual(payload["output_kind"], "source")


if __name__ == "__main__":
    unittest.main()
