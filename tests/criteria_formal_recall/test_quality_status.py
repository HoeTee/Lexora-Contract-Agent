from __future__ import annotations

import unittest
import json
from unittest.mock import AsyncMock, patch

from agents.orchestrator import OrchestratorAgent
from agents.summarizer import SummarizerAgent
from endpoints.review.task_store import summarize_criteria
from endpoints.runtime.errors import ModelCallError
from workflow.workflow import (
    _confirmed_review_instruction,
    _incomplete_review_notices,
    _prepare_inline_results,
    _public_criteria,
)


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
        self.assertIsNone(criteria[1]["review_result"])
        self.assertEqual(criteria[2]["execution_status"], "error")
        self.assertEqual(criteria[2]["error"]["type"], "ModelCallError")

    def test_incomplete_result_keeps_confirmed_report_content_without_a_conclusion(self) -> None:
        result = {
            "criterion_id": "U01",
            "section": "签署",
            "criterion": "检查签署信息",
            "status": None,
            "quality_status": "incomplete",
            "issues": [{
                "issue_id": "F001",
                "issue_comment": "请补充签署日期。",
                "anchors": [{
                    "role": "edit",
                    "xml_anchor_type": "paragraph",
                    "xml_anchor_id": "p3",
                    "quoted_text": "签署日期：",
                    "comment_text": "请补充签署日期。",
                }],
            }],
        }

        public = _public_criteria([{"id": "U01", "criterion": result["criterion"]}], [result])[0]
        inline, _ = _prepare_inline_results([result])
        notices = _incomplete_review_notices([result])

        self.assertEqual(public["execution_status"], "incomplete")
        self.assertIsNone(public["review_result"])
        self.assertEqual(inline[0]["issues"][0]["issue_id"], "F001")
        self.assertIn("已确认意见已列出", notices[0])
        self.assertNotIn("U01", notices[0])

    def test_incomplete_summary_placement_keeps_confirmed_instruction(self) -> None:
        result = {
            "status": None,
            "quality_status": "incomplete",
            "placement": {"target": "summary"},
            "review_instruction": "签署前核实授权材料。",
            "issues": [{"issue_id": "F001", "anchors": []}],
        }

        self.assertEqual(_confirmed_review_instruction(result), "签署前核实授权材料。")
        self.assertEqual(_prepare_inline_results([result]), ([], ["签署前核实授权材料。"]))
        self.assertIsNone(_confirmed_review_instruction({**result, "issues": []}))

    async def test_summary_receives_incomplete_scope_even_when_no_issues_are_confirmed(self) -> None:
        summarizer = SummarizerAgent.__new__(SummarizerAgent)
        response = AsyncMock(return_value=({
            "overall_comment": "本次仍有审查内容未完成。",
            "priority_comments": [],
            "duplicate_groups": [],
        }, "{}"))
        results = [{
            "criterion_id": "U02",
            "criterion": "检查附件条款",
            "status": None,
            "quality_status": "incomplete",
            "issues": [],
        }]

        with patch("agents.summarizer.chat_until_valid_json", response), patch(
            "agents.summarizer.render_prompt",
            side_effect=lambda _key, **values: values["prompt_payload_json"],
        ):
            await summarizer.compile_summary_comment(results)

        payload = json.loads(response.await_args.args[1])
        self.assertEqual(payload["issue_count"], 0)
        self.assertEqual(payload["incomplete_reviews"], [{
            "criterion_id": "U02",
            "section": "",
            "criterion": "检查附件条款",
            "execution_status": "incomplete",
            "confirmed_issue_count": 0,
        }])

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
