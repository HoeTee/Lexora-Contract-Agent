from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from loggers import api_event_logger
from loggers.task_log_io import allocate_logical_no, append_task_log_line


def _reserve_and_append(directory: str, count: int) -> list[int]:
    logs = Path(directory)
    allocated = []
    for _ in range(count):
        number = allocate_logical_no(logs)
        append_task_log_line(logs / "events.log", f"{number} " + "合同 evidence " * 100)
        allocated.append(number)
    return allocated


class TaskLogIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_processes_share_unique_numbers_and_append_whole_utf8_lines(self):
        logs = self.root / "shared" / "logs"
        # Match Linux deployment's prefork; Windows uses Python's spawn runner.
        context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
            results = list(executor.map(_reserve_and_append, [str(logs)] * 4, [16] * 4))
        numbers = [number for result in results for number in result]
        self.assertEqual(sorted(numbers), list(range(1, 65)))
        lines = (logs / "events.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 64)
        self.assertEqual({int(line.split(" ", 1)[0]) for line in lines}, set(numbers))
        for line in lines:
            self.assertEqual(line.count("合同 evidence "), 100)
        self.assertEqual(allocate_logical_no(logs), 65)

    def test_threads_reserve_distinct_numbers(self):
        logs = self.root / "logs"
        with ThreadPoolExecutor(max_workers=8) as executor:
            numbers = list(executor.map(allocate_logical_no, [logs] * 40))
        self.assertEqual(sorted(numbers), list(range(1, 41)))

    def test_tasks_start_independently_and_counter_resumes_without_json(self):
        first, second = self.root / "first" / "logs", self.root / "second" / "logs"
        self.assertEqual(allocate_logical_no(first), 1)
        self.assertEqual(allocate_logical_no(second), 1)
        self.assertEqual(allocate_logical_no(first), 2)
        self.assertEqual(allocate_logical_no(first), 3)
        self.assertEqual(allocate_logical_no(second), 2)

    def test_existing_numbered_files_prevent_reuse_and_legacy_files_are_untouched(self):
        logs = self.root / "logs"
        calls = logs / "logical_calls"
        calls.mkdir(parents=True)
        legacy = calls / "SubAgent_H11__review-initial__abc.json"
        legacy.write_text("legacy", encoding="utf-8")
        (calls / "12__agent__SubAgent_H11__review-initial.json").write_text("{}", encoding="utf-8")
        self.assertEqual(allocate_logical_no(logs), 13)
        (calls / "13__agent__SubAgent_H11__review-initial.json").write_text("{}", encoding="utf-8")
        (logs / ".logical_call_counter").write_text("2\n", encoding="ascii")
        self.assertEqual(allocate_logical_no(logs), 14)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "legacy")

    def test_corrupt_counter_does_not_silently_reset_or_overwrite(self):
        logs = self.root / "logs"
        logs.mkdir()
        counter = logs / ".logical_call_counter"
        for invalid in ("", "broken", "-1", "1.5"):
            with self.subTest(invalid=invalid):
                counter.write_text(invalid, encoding="ascii")
                with self.assertRaisesRegex(ValueError, "refusing to reuse"):
                    allocate_logical_no(logs)
                self.assertEqual(counter.read_text(encoding="ascii"), invalid)

    def test_multiline_append_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one line"):
            append_task_log_line(self.root / "events.log", "one\ntwo")
        self.assertFalse((self.root / "events.log").exists())


class TaskEventFormatterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "logs" / "events.log"
        enabled = patch("loggers.api_event_logger.LOGGING_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.addCleanup(self._close_logger)

    def _close_logger(self):
        logger = api_event_logger._LOGGERS.pop(str(self.path.resolve()), None)
        if logger is not None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_model_rows_use_fixed_columns_and_english_summary(self):
        api_event_logger.append_api_event(
            self.path, "model_request_sent", summary="Request 1 sent. Input: 5970 tokens.",
            logical_no=8, category="agent", criterion_id="H10", source="SubAgent_H10", stage="review-initial",
        )
        api_event_logger.append_api_event(
            self.path, "model_request_timeout", summary="Request 1 timed out after 60.002s. Output: unknown.",
            level="ERROR", logical_no=8, category="agent", criterion_id="H10", source="SubAgent_H10", stage="review-initial",
        )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        prefix = r"8 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} "
        self.assertRegex(lines[0], prefix + r"INFO\s+agent H10/SubAgent_H10/review-initial \| Request 1 sent\. Input: 5970 tokens\.$")
        self.assertRegex(lines[1], prefix + r"ERROR\s+agent H10/SubAgent_H10/review-initial \| Request 1 timed out")
        self.assertNotIn(str(self.path.parent), "\n".join(lines))
        self.assertNotIn("APIEvents", "\n".join(lines))

    def test_lifecycle_rows_have_placeholders_and_keep_structured_summary_status(self):
        api_event_logger.append_api_event(
            self.path, "review_completed", task_id="20261026-120000-abcd", summary={"status": "fallback"},
            result_file="C:/private/large/path/report.docx",
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertRegex(text, r"^- \S+ INFO\s+- -/-/- \| review_completed task_id=20261026-120000-abcd ")
        self.assertIn('summary={"status":"fallback","error":null}', text)
        self.assertNotIn("C:/private", text)

    def test_payloads_and_credentials_are_excluded_in_both_detail_modes(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), patch("loggers.api_event_logger.LOGGING_DETAILED", detailed):
                api_event_logger.append_api_event(
                    self.path, "request_failed", prompt="secret contract", messages=[{"content": "secret contract"}],
                    response={"payload": "secret contract"}, request={"secret": "secret contract"},
                    api_key="plain-credential", authorization="Bearer plain-credential",
                    error="API_KEY=plain-credential failed\nBearer another-credential sk-hiddencredential",
                    summary={"status": "failed", "error": {"message": "secret contract", "type": "Error"}},
                )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        text = "\n".join(lines)
        for private in ("secret contract", "plain-credential", "another-credential", "sk-hiddencredential"):
            self.assertNotIn(private, text)
        self.assertIn("[redacted]", text)
        self.assertIn('"status":"failed"', text)

    def test_disabled_logging_creates_no_files(self):
        with patch("loggers.api_event_logger.LOGGING_ENABLED", False):
            api_event_logger.append_api_event(self.path, "review_started")
        self.assertFalse(self.path.parent.exists())

    def test_json_credentials_and_structured_error_body_are_not_basic_log_payloads(self):
        api_event_logger.append_api_event(
            self.path, "request_failed",
            error='BadRequestError: HTTP 400 {"api_key":"plain-credential","input":"private contract text"}',
            access_token="private-access", refresh_token="private-refresh",
            input_tokens=120, output_tokens=4, token_stats={"input": 120, "output": 4, "sent_attempts": 1},
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("BadRequestError HTTP 400 Response body omitted.", text)
        for private in ("plain-credential", "private contract text", "private-access", "private-refresh"):
            self.assertNotIn(private, text)
        self.assertIn("input_tokens=120", text)
        self.assertIn("output_tokens=4", text)
        self.assertIn('token_stats={"input":120,"output":4,"sent_attempts":1}', text)

    def test_quoted_credentials_in_explicit_summary_are_redacted(self):
        api_event_logger.append_api_event(
            self.path, "request_failed",
            summary='Provider rejected {"api_key": "private-api", "refresh_token": "private-refresh"}.',
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private-api", text)
        self.assertNotIn("private-refresh", text)
        self.assertEqual(text.count("[redacted]"), 2)

    def test_real_task_completion_metadata_stays_short_in_both_detail_modes(self):
        summary = {
            "status": "completed", "error": None,
            "deduplication": {"candidate_group_count": 3, "accepted_group_count": 3,
                              "application": {"skipped_group_details": ["large detail"] * 100}},
        }
        token_stats = {"planner": 0, "retrieval": 175726, "execute": 258689, "summarizer": 9116,
                       "total": 443531, "input": 404318, "output": 39213, "sent_attempts": 83,
                       "unknown_output_attempts": 0, "local_fallback_attempts": 4,
                       "output_complete": True, "stats_complete": True, "total_is_lower_bound": False}
        annotations = {"annotation_events_path": "/app/data/private/task/logs/annotation_events.json",
                       "annotation_stats": {"total_issues": 25, "xml_anchor_matched": 24,
                                            "missing_text_fallback": 1, "unmatched": 0},
                       "annotation_skipped_count": 1}
        for detailed in (False, True):
            with patch("loggers.api_event_logger.LOGGING_DETAILED", detailed):
                api_event_logger.append_api_event(
                    self.path, "review_completed", task_id="20261026-125853-0338", elapsed_seconds=338.2,
                    criteria_count=18, issue_count=30, token_stats=token_stats,
                    annotation_stats=annotations, summary=summary,
                )
                api_event_logger.append_api_event(self.path, "summary_completed", summary=summary)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines[::2]:
            self.assertLess(len(line), 600)
            self.assertIn('token_stats={"input":404318,"output":39213,"total":443531,"sent_attempts":83}', line)
            self.assertIn('annotation_stats={"total_issues":25,"xml_anchor_matched":24,"missing_text_fallback":1,"unmatched":0,"annotation_skipped_count":1}', line)
            self.assertNotIn("annotation_events_path", line)
            self.assertNotIn("deduplication", line)
            self.assertNotIn("/app/data", line)
        for line in lines[1::2]:
            self.assertLess(len(line), 200)
            self.assertIn('summary={"status":"completed","error":null}', line)

    def test_task_token_compaction_keeps_incomplete_and_lower_bound_flags(self):
        api_event_logger.append_api_event(
            self.path, "review_failed",
            token_stats={"input": 100, "output": 5, "total": 105, "sent_attempts": 2,
                         "input_complete": True, "output_complete": False, "stats_complete": False,
                         "total_is_lower_bound": True, "unknown_output_attempts": 1},
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertIn('"output_complete":false', text)
        self.assertIn('"stats_complete":false', text)
        self.assertIn('"total_is_lower_bound":true', text)
        self.assertNotIn('"input_complete"', text)
        self.assertNotIn('"unknown_output_attempts"', text)

    def test_summary_compaction_keeps_error_type_and_omits_response_details(self):
        api_event_logger.append_api_event(
            self.path, "summary_fallback",
            summary={"status": "fallback", "error": {"type": "AgentOutputValidationError",
                                                       "message": "private model response"},
                     "deduplication": {"skipped_groups": ["long details"] * 100}},
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertIn('"status":"fallback","error":{"type":"AgentOutputValidationError"}', text)
        self.assertNotIn("private model response", text)
        self.assertNotIn("deduplication", text)
        self.assertLess(len(text.strip()), 220)

    def test_threaded_rows_remain_complete_and_share_one_cached_handler(self):
        def write(number):
            api_event_logger.append_api_event(
                self.path, "model_response_received", summary=f"Response {number} received.",
                logical_no=number, category="docxindex.retrieval.llm", source="docxindex_H12", stage="scan-step2",
            )
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(1, 41)))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 40)
        for line in lines:
            number = int(line.split(" ", 1)[0])
            self.assertTrue(line.endswith(f" | Response {number} received."))
            self.assertIn(" docxindex.retrieval.llm -/docxindex_H12/scan-step2 | ", line)
        logger = api_event_logger._LOGGERS[str(self.path.resolve())]
        self.assertEqual(len(logger.handlers), 1)


if __name__ == "__main__":
    unittest.main()
