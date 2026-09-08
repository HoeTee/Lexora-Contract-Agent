import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from docx import Document

from routing import load_mandatory_tasks
from tools.document.reporting.docx_report import deduplicate_review_results_by_range
from workflow.workflow import ContractReviewWorkflow


class MandatoryRoutingTests(unittest.TestCase):
    def test_profile_maps_all_18_docx_criteria_to_fixed_routes(self) -> None:
        tasks = load_mandatory_tasks()

        self.assertEqual(len(tasks), 18)
        self.assertEqual([task["id"] for task in tasks], [f"H{i:02d}" for i in range(1, 19)])
        self.assertTrue(all(task["review_source"] == "mandatory" for task in tasks))
        self.assertTrue(all(not task["fallback_llm_route"] for task in tasks))
        methods = {step["method"] for task in tasks for step in task["route"]["steps"]}
        self.assertEqual(methods, {"title", "region", "rule", "scan", "hybrid"})

    def test_formal_criteria_phase_skips_planner_model_call(self) -> None:
        workflow = ContractReviewWorkflow.__new__(ContractReviewWorkflow)
        workflow.state_store = None
        workflow.settings = None
        workflow.logger = SimpleNamespace(log=lambda **_kwargs: None)

        with patch("workflow.workflow.PlannerAgent", side_effect=AssertionError("Planner must be skipped")):
            tasks = asyncio.run(
                workflow._phase_plan("", "resources/criteria/criteria-formal.docx")
            )

        self.assertEqual(len(tasks), 18)
        self.assertEqual(workflow._planner_tokens, 0)

    def test_same_resolved_range_prefers_mandatory_over_uploaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contract.docx"
            document = Document()
            document.add_paragraph("乙方：#乙方名称#")
            document.save(path)
            mandatory = _result("H01", "mandatory", "请填写乙方名称。")
            uploaded = _result("U01", "uploaded", "请核实乙方名称。")

            results, stats = deduplicate_review_results_by_range(
                str(path),
                [uploaded, mandatory],
            )

        self.assertEqual(results[0]["issues"], [])
        self.assertEqual(len(results[1]["issues"]), 1)
        self.assertEqual(stats["removed_duplicate_issues"], 1)

    def test_same_source_findings_are_not_removed_by_priority_rule(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contract.docx"
            document = Document()
            document.add_paragraph("乙方：#乙方名称#")
            document.save(path)
            first = _result("H01", "mandatory", "请填写乙方名称。")
            second = _result("H10", "mandatory", "乙方名称未填写。")

            results, stats = deduplicate_review_results_by_range(str(path), [first, second])

        self.assertEqual([len(result["issues"]) for result in results], [1, 1])
        self.assertEqual(stats["removed_duplicate_issues"], 0)


def _result(criterion_id: str, source: str, comment: str) -> dict:
    return {
        "criterion_id": criterion_id,
        "review_source": source,
        "issues": [{
            "issue_id": f"{criterion_id}.1",
            "risk_level": "low",
            "issue_comment": comment,
            "anchors": [{
                "role": "edit",
                "defect_type": "missing_value",
                "xml_anchor_type": "paragraph",
                "xml_anchor_id": "path:body/p1",
                "quoted_text": "乙方：#乙方名称#",
                "comment_text": comment,
            }],
        }],
    }


if __name__ == "__main__":
    unittest.main()
