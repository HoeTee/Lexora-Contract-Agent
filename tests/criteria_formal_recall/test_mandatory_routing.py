import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from docx import Document

from routing import load_mandatory_tasks
from tools.document.reporting.docx_report import deduplicate_review_results_by_range
from workflow.workflow import ContractReviewWorkflow


class MandatoryRoutingTests(unittest.TestCase):
    def test_yaml_profile_defines_all_18_fixed_routes(self) -> None:
        tasks = load_mandatory_tasks()

        self.assertEqual(len(tasks), 18)
        self.assertEqual([task["id"] for task in tasks], [f"H{i:02d}" for i in range(1, 19)])
        self.assertTrue(all(task["review_source"] == "mandatory" for task in tasks))
        self.assertTrue(all(not task["fallback_llm_route"] for task in tasks))
        self.assertEqual(tasks[11]["dedupe_domain"], "language_quality")
        self.assertEqual(tasks[14]["dedupe_domain"], "language_quality")
        self.assertEqual(tasks[10]["evidence_region"], "attachments")
        methods = {step["method"] for task in tasks for step in task["route"]["steps"]}
        self.assertEqual(methods, {"title", "region", "rule", "scan", "hybrid"})

    def test_mandatory_only_phase_skips_planner_model_call(self) -> None:
        workflow = ContractReviewWorkflow.__new__(ContractReviewWorkflow)
        workflow.state_store = None
        workflow.settings = None
        workflow.logger = SimpleNamespace(log=lambda **_kwargs: None)

        with patch("workflow.workflow.PlannerAgent", side_effect=AssertionError("Planner must be skipped")):
            tasks = asyncio.run(workflow._phase_plan("", None))

        self.assertEqual(len(tasks), 18)
        self.assertEqual(workflow._planner_tokens, 0)

    def test_uploaded_criteria_are_appended_as_planned_tasks(self) -> None:
        workflow = ContractReviewWorkflow.__new__(ContractReviewWorkflow)
        workflow.state_store = None
        workflow.settings = None
        workflow.logger = SimpleNamespace(log=lambda **_kwargs: None)
        planner = SimpleNamespace(
            extract_strategies=AsyncMock(return_value={
                "U01": {
                    "route": {"steps": [{"method": "scan"}], "fallback": False},
                    "placement": {"target": "inline", "note": ""},
                }
            }),
            token_usage={"total_tokens": 12},
        )
        skeletons = [{"number": 1, "section": "用户标准", "criterion": "检查自定义事项"}]

        with (
            patch("workflow.workflow.split_criteria_from_docx", return_value=skeletons),
            patch("workflow.workflow.PlannerAgent", return_value=planner),
        ):
            tasks = asyncio.run(workflow._phase_plan("custom", "uploaded-criteria.docx"))

        self.assertEqual(len(tasks), 19)
        self.assertEqual(tasks[-1]["id"], "U01")
        self.assertEqual(tasks[-1]["review_source"], "uploaded")
        planner.extract_strategies.assert_awaited_once()
        self.assertEqual(workflow._planner_tokens, 12)

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

    def test_same_source_exact_range_keeps_higher_priority_finding(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contract.docx"
            document = Document()
            document.add_paragraph("乙方：#乙方名称#")
            document.save(path)
            first = _result("H01", "mandatory", "请填写乙方名称。")
            second = _result("H10", "mandatory", "乙方名称未填写。")
            first["dedupe_priority"] = 100
            second["dedupe_priority"] = 200

            results, stats = deduplicate_review_results_by_range(str(path), [first, second])

        self.assertEqual([len(result["issues"]) for result in results], [0, 1])
        self.assertEqual(stats["removed_duplicate_issues"], 1)

    def test_uploaded_exact_range_never_overrides_mandatory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contract.docx"
            document = Document()
            document.add_paragraph("乙方：#乙方名称#")
            document.save(path)
            mandatory = _result("H01", "mandatory", "请填写乙方名称。")
            uploaded = _result("U01", "uploaded", "请核实乙方名称。")
            uploaded["dedupe_priority"] = 999

            results, _ = deduplicate_review_results_by_range(
                str(path),
                [uploaded, mandatory],
            )

        self.assertEqual(results[0]["issues"], [])
        self.assertEqual(len(results[1]["issues"]), 1)

    def test_contained_ranges_in_same_domain_keep_higher_priority(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contract.docx"
            document = Document()
            document.add_paragraph("不得贿赂贵方项目和管理人员。")
            document.save(path)
            typo = _result("H12", "mandatory", "项目不能作为贿赂对象。")
            grammar = _result("H15", "mandatory", "建议改为项目人员和管理人员。")
            typo["issues"][0]["anchors"][0]["quoted_text"] = "贿赂贵方项目和管理人员。"
            grammar["issues"][0]["anchors"][0]["quoted_text"] = "不得贿赂贵方项目和管理人员。"
            typo.update(dedupe_priority=100, dedupe_domain="language_quality")
            grammar.update(dedupe_priority=200, dedupe_domain="language_quality")

            results, stats = deduplicate_review_results_by_range(str(path), [typo, grammar])

        self.assertEqual([len(result["issues"]) for result in results], [0, 1])
        self.assertEqual(stats["removed_overlap_anchors"], 1)

    def test_contained_ranges_in_different_domains_are_preserved(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "contract.docx"
            document = Document()
            document.add_paragraph("合同金额人民币捌万元整。")
            document.save(path)
            first = _result("H12", "mandatory", "文字问题。")
            second = _result("H14", "mandatory", "金额问题。")
            first["issues"][0]["anchors"][0]["quoted_text"] = "人民币捌万元整"
            second["issues"][0]["anchors"][0]["quoted_text"] = "合同金额人民币捌万元整。"
            first["dedupe_domain"] = "language_quality"
            second["dedupe_domain"] = "amount"

            results, stats = deduplicate_review_results_by_range(str(path), [first, second])

        self.assertEqual([len(result["issues"]) for result in results], [1, 1])
        self.assertEqual(stats["removed_overlap_anchors"], 0)


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
