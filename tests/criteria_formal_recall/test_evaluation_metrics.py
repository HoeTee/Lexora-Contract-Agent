from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.criteria_formal_recall.evaluate_results import criterion_id, write_metrics


class EvaluationMetricsTests(unittest.TestCase):
    def test_current_mandatory_and_uploaded_ids_map_to_numeric_criteria(self) -> None:
        self.assertEqual(criterion_id({"criterion_id": "H01"}), 1)
        self.assertEqual(criterion_id({"criterion_id": "C18"}), 18)

    def test_metrics_cover_contract_criterion_time_recall_and_accuracy(self) -> None:
        expected = [
            {"contract_id": "D01", "criterion_id": "1"},
            {"contract_id": "D01", "criterion_id": "2"},
        ]
        records = {
            "D01": {"status": "succeeded", "duration_seconds": 120.5},
        }
        inventory = [
            {"contract_id": "D01", "criterion_id": 1},
            {"contract_id": "D01", "criterion_id": 2},
        ]

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            passed = write_metrics(
                output_dir,
                expected,
                records,
                inventory,
                incorrect_rows=[],
                failures=[],
                max_duration_seconds=300.0,
                min_recall=0.85,
                min_accuracy=0.90,
            )
            with (output_dir / "metrics_summary.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as stream:
                summary = next(csv.DictReader(stream))

        self.assertTrue(passed)
        self.assertEqual(summary["all_within_time_limit"], "yes")
        self.assertEqual(summary["recall"], "100.0000%")
        self.assertEqual(summary["accuracy"], "100.0000%")

    def test_unrecalled_case_fails_recall_threshold(self) -> None:
        expected = [
            {"contract_id": "D01", "criterion_id": "1"},
            {"contract_id": "D01", "criterion_id": "2"},
        ]
        records = {"D01": {"status": "succeeded", "duration_seconds": 100.0}}

        with TemporaryDirectory() as temp_dir:
            passed = write_metrics(
                Path(temp_dir),
                expected,
                records,
                inventory=[{"contract_id": "D01", "criterion_id": 1}],
                incorrect_rows=[],
                failures=[{"contract_id": "D01", "criterion_id": 2}],
                max_duration_seconds=300.0,
                min_recall=0.85,
                min_accuracy=0.90,
            )

        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()
