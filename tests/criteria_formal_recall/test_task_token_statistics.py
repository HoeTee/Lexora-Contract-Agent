from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from endpoints.review import task_store
from endpoints.review.response import present_review_task
from workflow.workflow import ContractReviewWorkflow


class TaskTokenStatisticsTests(unittest.TestCase):
    def test_failed_task_and_api_preserve_sent_attempts_and_unknown_output(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(task_store, "DATA_DIR", temporary):
            root = Path(temporary)
            source = root / "contract.docx"
            source.write_bytes(b"fixture")
            client, task_id = "api", "20261025-120000-a001"
            task_store.create_task(
                client_id="test", client_dir=client, task_id=task_id,
                contract_filename=source.name, contract_path=source,
                criteria_source="mandatory", criteria_filename=None, criteria_path=None,
                result_filename="report.docx", result_path=root / "report.docx")
            directory = task_store.logs_dir(client, task_id) / "logical_calls"
            directory.mkdir(parents=True, exist_ok=True)
            record = {"source": "SubAgent_H01", "category": "agent", "attempts": [
                {"call_id": "sent", "sent": True, "input_tokens": 20, "output_tokens": None,
                 "input_token_source": "local", "output_completeness": "unknown", "model_type": "llm"},
                {"call_id": "rejected", "sent": False, "input_tokens": 1000,
                 "failure_stage": "input_validation"},
            ]}
            (directory / "one.json").write_text(json.dumps(record), encoding="utf-8")
            # Repeated references must not count the same HTTP attempt twice.
            (directory / "duplicate.json").write_text(json.dumps(record), encoding="utf-8")
            workflow = ContractReviewWorkflow.__new__(ContractReviewWorkflow)
            workflow.api_events_path = str(directory.parent / "events.log")
            expected = workflow._collect_token_stats([])
            task = task_store.mark_failed(client, task_id, code="MODEL_FAILED", message="fixture")
            self.assertEqual(task["token_stats"], expected)
            self.assertEqual(present_review_task(task)["token_stats"], expected)
            self.assertEqual(expected["total"], 20)
            self.assertEqual(expected["sent_attempts"], 1)
            self.assertEqual(expected["input_rejections"], 1)
            self.assertEqual(expected["unknown_output_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
