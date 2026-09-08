"""Quality gate tests use synthetic judgements, never model scoring."""

import asyncio
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.docxindex_ablation.formal import formal
from tests.docxindex_ablation.live import live
from tests.docxindex_ablation.quality import check, evaluate, require_baseline_quality, reservation_conflict
from tests.docxindex_ablation.run import write_json


def fixture():
    frozen = {
        "expected": [{"case_id": "D01-C01", "contract_id": "D01"}],
        "contracts": {
            "D01": {"comments": [{"comment_id": "7", "author": "AI 条款审查", "text": "地址缺失，请补充。"}]}
        },
    }
    review = {
        "cases": [
            {
                "case_id": "D01-C01",
                "expected_status": "valid",
                "acceptance_condition": "指出地址缺失",
                "baseline_verdict": "hit",
                "comment_ids": ["7"],
                "evidence_excerpt": "地址缺失",
                "location_correct": True,
                "reviewer": "test-only",
                "reason": "synthetic fixture",
            }
        ],
        "comments": [
            {
                "contract_id": "D01",
                "comment_id": "7",
                "verdict": "correct",
                "criterion_ids": [1],
                "location_correct": True,
                "reviewer": "test-only",
                "reason": "synthetic fixture",
            }
        ],
    }
    return frozen, review


class BaselineQualityTests(unittest.TestCase):
    def test_reviewed_baseline_can_have_misses_and_incorrect_comments(self):
        frozen, review = fixture()
        review["cases"][0]["baseline_verdict"] = "miss"
        review["comments"][0]["verdict"] = "incorrect"
        result = evaluate(frozen, review)
        self.assertTrue(result["baseline_ready"])
        self.assertEqual((result["recall"], result["precision"]), (0.0, 0.0))

    def test_incomplete_disputed_and_invalid_cases_block_without_scores(self):
        frozen, original = fixture()
        for status in ("pending", "disputed", "invalid"):
            review = deepcopy(original)
            review["cases"][0]["expected_status"] = status
            result = evaluate(frozen, review)
            self.assertFalse(result["baseline_ready"])
            self.assertIsNone(result["recall"])
            self.assertIsNone(result["precision"])

    def test_missing_duplicate_and_invented_entries_block(self):
        frozen, original = fixture()
        for field in ("cases", "comments"):
            for replacement in ([], original[field] * 2):
                review = deepcopy(original)
                review[field] = replacement
                self.assertFalse(evaluate(frozen, review)["baseline_ready"])
        for field, value in (("comment_ids", ["999"]), ("evidence_excerpt", "编造原文"), ("location_correct", False)):
            review = deepcopy(original)
            review["cases"][0][field] = value
            self.assertFalse(evaluate(frozen, review)["baseline_ready"])

    def test_unaccountable_or_uncertain_comment_blocks(self):
        frozen, original = fixture()
        for field, value in (("reviewer", ""), ("reason", ""), ("verdict", "uncertain"), ("criterion_ids", [])):
            review = deepcopy(original)
            review["comments"][0][field] = value
            self.assertFalse(evaluate(frozen, review)["baseline_ready"])

    def test_partial_recall_is_not_a_full_hit(self):
        frozen, review = fixture()
        review["cases"][0]["baseline_verdict"] = "partial"
        result = evaluate(frozen, review)
        self.assertTrue(result["baseline_ready"])
        self.assertEqual(result["recall"], 0)

    def test_reservation_conflict_does_not_claim_general_quality_failure(self):
        items = [
            {"body_child_index": 4, "text": "项目负责人：姓名【未填写】，身份证号码【未填写】，电话号码【未填写】"}
        ]
        self.assertEqual(reservation_conflict("预留姓名、身份证号码及电话号码", "项目负责人信息未填写", items), items)
        self.assertEqual(reservation_conflict("必须填写完整", "项目负责人信息未填写", items), [])

    def test_stale_snapshot_blocks_before_using_judgements(self):
        with TemporaryDirectory() as folder:
            output = Path(folder)
            write_json(output / "baseline_quality/snapshot.json", {"old": True})
            with patch("tests.docxindex_ablation.quality.snapshot", return_value={"old": False}):
                with self.assertRaisesRegex(ValueError, "Frozen evidence changed"):
                    check(output)

    def test_unreviewed_baseline_and_old_combination_cannot_submit_tasks(self):
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "unreviewed"):
                require_baseline_quality(Path(folder))
            args = SimpleNamespace(output=Path(folder), variants=["safe"])
            with patch("tests.docxindex_ablation.formal.run_one") as submit:
                with self.assertRaisesRegex(ValueError, "unreviewed"):
                    asyncio.run(formal(args))
                submit.assert_not_called()
                with patch("tests.docxindex_ablation.formal.require_baseline_quality"):
                    with self.assertRaisesRegex(ValueError, "combined treatment is disabled"):
                        asyncio.run(formal(args))
                submit.assert_not_called()

    def test_live_reranks_are_blocked_before_model_access(self):
        with TemporaryDirectory() as folder:
            with patch("tests.docxindex_ablation.live.ORIGINAL_RERANK") as rerank:
                with self.assertRaisesRegex(ValueError, "unreviewed"):
                    live(SimpleNamespace(output=Path(folder)))
                rerank.assert_not_called()


if __name__ == "__main__":
    unittest.main()
