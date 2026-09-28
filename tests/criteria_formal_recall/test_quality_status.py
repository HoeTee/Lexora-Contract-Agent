from __future__ import annotations

import unittest

from agents.orchestrator import OrchestratorAgent
from endpoints.review.task_store import summarize_criteria
from endpoints.runtime.errors import ModelCallError
from workflow.workflow import _public_criteria


class QualityStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_quality_summary_is_derived_from_complete_criterion_list(self) -> None:
        criteria = [
            {"id": "H01", "execution_status": "completed"},
            {"id": "H08", "execution_status": "incomplete"},
            {"id": "H12", "execution_status": "error"},
        ]

        quality = summarize_criteria(criteria)

        self.assertEqual(quality["total_count"], 3)
        self.assertEqual(quality["completed_count"], 1)
        self.assertEqual(quality["incomplete_criteria"], ["H08"])
        self.assertEqual(quality["error_criteria"], ["H12"])

    def test_public_criteria_separates_execution_from_review_result(self) -> None:
        definitions = [
            {"id": "H01", "criterion": "标准一"},
            {"id": "H08", "criterion": "标准八"},
            {"id": "H12", "criterion": "标准十二"},
        ]
        results = [
            {"criterion_id": "H01", "status": "COMPLIANT", "quality_status": "not_required"},
            {"criterion_id": "H08", "status": "ISSUES_FOUND", "quality_status": "incomplete"},
            {
                "criterion_id": "H12",
                "status": "ERROR",
                "quality_status": "error",
                "error_type": "ModelCallError",
                "error_message": "Request timed out.",
            },
        ]

        criteria = _public_criteria(definitions, results)

        self.assertEqual(criteria[0]["execution_status"], "completed")
        self.assertEqual(criteria[0]["review_result"], "compliant")
        self.assertEqual(criteria[1]["execution_status"], "incomplete")
        self.assertEqual(criteria[2]["execution_status"], "error")
        self.assertEqual(criteria[2]["error"]["type"], "ModelCallError")

    async def test_reflector_model_failure_propagates_to_criterion_recovery(self) -> None:
        class FailingReflector:
            token_usage = {"total_tokens": 0}

            async def review(self, **_: object) -> dict:
                raise ModelCallError("agent", "Request timed out.", error_type="APITimeoutError")

        orchestrator = OrchestratorAgent.__new__(OrchestratorAgent)
        orchestrator.logger = None

        with self.assertRaises(ModelCallError):
            await orchestrator._review_with_fallback(
                FailingReflector(),
                cid="H12",
                round_num=1,
                reflector_input="input",
                missing_text_review_notes="",
            )


if __name__ == "__main__":
    unittest.main()
