"""Configuration migration failures must be explicit before serving work."""
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import runpy
import unittest
from unittest.mock import patch

import config


class ControlConfigurationTests(unittest.TestCase):
    def test_model_wait_budget_omission_is_unlimited_and_reported_independently(self):
        for value in (None, 25.5, 3600):
            raw = deepcopy(config.CONFIG)
            if value is None:
                raw["workflow"].pop("model_wait_timeout_seconds", None)
            else:
                raw["workflow"]["model_wait_timeout_seconds"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                loaded = runpy.run_path(config.__file__)
                expected = value
                self.assertEqual(loaded["MODEL_WAIT_TIMEOUT_SECONDS"], expected)
                summary = loaded["control_configuration_summary"]()
                self.assertEqual(summary["workflow"]["model_wait_timeout_seconds"], expected)
                self.assertEqual(loaded["MODEL_CALL_TIMEOUT_SECONDS"], config.MODEL_CALL_TIMEOUT_SECONDS)
                self.assertEqual(summary["model_semaphore"]["llm"]["lease_seconds"],
                    config.MODEL_SEMAPHORE_CONFIGS["llm"]["lease_seconds"])

    def test_model_wait_budget_requires_a_positive_finite_number(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            raw = deepcopy(config.CONFIG)
            raw["workflow"]["model_wait_timeout_seconds"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "workflow.model_wait_timeout_seconds"):
                    runpy.run_path(config.__file__)

    def test_removed_fields_are_rejected_with_a_migration_message(self):
        for section, field in (("workflow", "max_api_concurrent_reviews"),
                               ("queue", "enabled"), ("queue", "task_soft_time_limit_seconds"),
                               ("embedding", "timeout_seconds"), ("rerank", "max_retries")):
            with self.subTest(field=f"{section}.{field}"):
                raw = deepcopy(config.CONFIG)
                raw[section][field] = 1
                with patch.object(config, "CONFIG", raw), self.assertRaisesRegex(RuntimeError, "Removed configuration"):
                    config._validate_control_config()

    def test_criterion_attempts_are_configurable_and_omission_is_unlimited(self):
        for value in (None, 2, 3):
            raw = deepcopy(config.CONFIG)
            if value is None:
                raw["workflow"].pop("criterion_retry_max_attempts", None)
            else:
                raw["workflow"]["criterion_retry_max_attempts"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                loaded = runpy.run_path(config.__file__)
                self.assertEqual(loaded["CRITERION_RETRY_MAX_ATTEMPTS"], value)

    def test_criterion_attempts_must_be_a_positive_integer(self):
        for value in (0, -1, 1.5, True):
            raw = deepcopy(config.CONFIG)
            raw["workflow"]["criterion_retry_max_attempts"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "workflow.criterion_retry_max_attempts"):
                    runpy.run_path(config.__file__)

    def test_legacy_shared_semaphore_and_retrieval_limits_are_not_silently_accepted(self):
        raw = deepcopy(config.CONFIG)
        raw["model_semaphore"]["limits"] = {"llm": 5}
        with patch.object(config, "CONFIG", raw), self.assertRaisesRegex(RuntimeError, "Migrate shared"):
            config._validate_control_config()
        raw = deepcopy(config.CONFIG)
        raw["retrieval"].setdefault("docxindex", {})["concurrency"] = {"llm": 5}
        with patch.object(config, "CONFIG", raw), self.assertRaisesRegex(RuntimeError, "Remove retrieval"):
            config._validate_control_config()

    def test_per_model_bypass_and_wait_timeout_are_removed(self):
        for field in ("enabled", "wait_timeout_seconds"):
            raw = deepcopy(config.CONFIG)
            raw["model_semaphore"]["llm"][field] = 1
            with patch.object(config, "CONFIG", raw), self.assertRaisesRegex(RuntimeError, "Removed/unknown"):
                config._model_semaphore_config("llm", default_max=5)

    def test_renewed_lease_does_not_need_to_cover_entire_request(self):
        raw = deepcopy(config.CONFIG)
        raw["model_semaphore"]["llm"]["lease_seconds"] = 10
        with patch.object(config, "CONFIG", raw):
            loaded = config._model_semaphore_config("llm", default_max=5)
        self.assertEqual(loaded["lease_seconds"], 10)
        self.assertLess(loaded["lease_seconds"], raw["workflow"]["model_call_timeout_seconds"])

    def test_null_limits_are_explicitly_unlimited_across_models_and_queue(self):
        raw = deepcopy(config.CONFIG)
        for field in ("model_wait_timeout_seconds", "model_call_timeout_seconds", "model_call_max_retries",
                      "criterion_retry_max_attempts", "max_reflection_rounds", "max_orchestrator_concurrency"):
            raw["workflow"][field] = None
        for field in ("task_time_limit_seconds", "pending_timeout_seconds", "queued_timeout_seconds"):
            raw["queue"][field] = None
        for kind in ("llm", "embedding", "reranker"):
            raw["model_semaphore"][kind]["max_concurrent_requests"] = None
            raw["model_semaphore"][kind]["max_requests_per_minute"] = None
        with patch("yaml.safe_load", return_value=raw), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            loaded = runpy.run_path(config.__file__)
        for name in ("MODEL_WAIT_TIMEOUT_SECONDS", "MODEL_CALL_TIMEOUT_SECONDS", "MODEL_CALL_MAX_RETRIES",
                     "CRITERION_RETRY_MAX_ATTEMPTS", "MAX_REFLECTION_ROUNDS", "MAX_ORCHESTRATOR_CONCURRENCY",
                     "QUEUE_TASK_TIME_LIMIT_SECONDS", "QUEUE_PENDING_TIMEOUT_SECONDS", "QUEUE_QUEUED_TIMEOUT_SECONDS"):
            self.assertIsNone(loaded[name], name)
        for values in loaded["MODEL_SEMAPHORE_CONFIGS"].values():
            self.assertIsNone(values["max_concurrent_requests"])
            self.assertIsNone(values["max_requests_per_minute"])

    def test_non_finite_and_fractional_limits_are_rejected(self):
        for value in (float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                config._as_float(value, "timeout")
        for value in (1.5, True):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                config._as_int(value, "concurrency")

    def test_startup_audit_redacts_redis_credentials_and_shows_hidden_defaults(self):
        with patch.object(config, "QUEUE_BROKER_URL", "redis://username:secret@redis:6379/0"):
            summary = config.control_configuration_summary()
        self.assertEqual(summary["queue"]["broker"], "redis://redis:6379/0")
        self.assertEqual(summary["queue"]["soft_time_limit"], "disabled")
        self.assertEqual(summary["queue"]["visibility_timeout_seconds"], 3600)
        self.assertEqual(summary["queue"]["queued_timeout_seconds"], 7200)
        self.assertEqual(summary["model_semaphore"]["llm"]["key_ttl_seconds"], 660)

    def test_valid_budget_pressure_warns_without_rejecting_startup(self):
        raw = deepcopy(config.CONFIG)
        raw["llm"]["max_input_tokens"] = 2000
        raw["embedding"]["max_input_tokens"] = 16
        raw["retrieval"]["docxindex"]["scan_batch_tokens"] = 2000
        raw["retrieval"]["docxindex"].setdefault("indexing", {})["hierarchy"] = {
            "input_max_tokens": 2000, "batch_target_tokens": 4000,
        }
        stderr = StringIO()
        with patch("yaml.safe_load", return_value=raw), redirect_stdout(StringIO()), redirect_stderr(stderr):
            loaded = runpy.run_path(config.__file__)
        fields = {warning["field"] for warning in loaded["input_configuration_warnings"]()}
        self.assertIn("retrieval.docxindex.scan_batch_tokens", fields)
        self.assertIn("retrieval.docxindex.indexing.hierarchy.batch_target_tokens", fields)
        self.assertIn("embedding.chunk_overlap_tokens", fields)
        self.assertIn("WARN input-budget", stderr.getvalue())
        self.assertEqual(loaded["LLM_MAX_INPUT_TOKENS"], 2000)

    def test_unknown_nested_field_and_wait_typo_fail_before_becoming_unlimited(self):
        for path, field in (("workflow", "model_wait_timeout_second"),
                            ("retrieval.docxindex.indexing.summary", "batch_max_token"),
                            ("retrieval.docxindex.indexing", "mystery")):
            raw = deepcopy(config.CONFIG)
            section = raw
            for component in path.split("."):
                section = section.setdefault(component, {})
            section[field] = 1000
            with self.subTest(path=path), patch("yaml.safe_load", return_value=raw), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "Unknown or removed"):
                    runpy.run_path(config.__file__)

    def test_invalid_retrieval_numbers_are_errors_not_budget_warnings(self):
        for value in (True, 1.5, -1, float("nan"), float("inf")):
            raw = deepcopy(config.CONFIG)
            raw["retrieval"]["docxindex"]["vector"]["probe_limit"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "probe_limit"):
                    runpy.run_path(config.__file__)

    def test_omitted_global_caps_do_not_restore_old_defaults(self):
        raw = deepcopy(config.CONFIG)
        for kind in ("llm", "embedding", "reranker"):
            raw["model_semaphore"][kind].pop("max_concurrent_requests", None)
            raw["model_semaphore"][kind].pop("max_requests_per_minute", None)
        for field in ("model_call_max_retries", "max_reflection_rounds", "max_orchestrator_concurrency"):
            raw["workflow"].pop(field, None)
        raw["retrieval"]["llamaindex"] = {"chunk_size": 512, "chunk_overlap": 100}
        with patch("yaml.safe_load", return_value=raw), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            loaded = runpy.run_path(config.__file__)
        for kind in ("llm", "embedding", "reranker"):
            self.assertIsNone(loaded["MODEL_SEMAPHORE_CONFIGS"][kind]["max_concurrent_requests"])
            self.assertIsNone(loaded["MODEL_SEMAPHORE_CONFIGS"][kind]["max_requests_per_minute"])
        for name in ("MODEL_CALL_MAX_RETRIES", "MAX_REFLECTION_ROUNDS", "MAX_ORCHESTRATOR_CONCURRENCY",
                     "SIMILARITY_TOP_K", "RERANK_TOP_N"):
            self.assertIsNone(loaded[name], name)

    def test_watchdog_interval_is_positive_finite_and_not_a_nullable_limit(self):
        for value in (0, -1, True, None, float("nan"), float("inf")):
            raw = deepcopy(config.CONFIG)
            raw["queue"]["watchdog_interval_seconds"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "queue.watchdog_interval_seconds"):
                    runpy.run_path(config.__file__)
        raw = deepcopy(config.CONFIG)
        raw["queue"]["watchdog_interval_seconds"] = 0.25
        with patch("yaml.safe_load", return_value=raw), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            loaded = runpy.run_path(config.__file__)
        self.assertEqual(loaded["QUEUE_WATCHDOG_INTERVAL_SECONDS"], 0.25)


if __name__ == "__main__":
    unittest.main()
