"""Configuration migration failures must be explicit before serving work."""
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import runpy
import unittest
from unittest.mock import patch

import config


class ControlConfigurationTests(unittest.TestCase):
    def test_model_wait_budget_defaults_to_180_and_is_reported_independently(self):
        for value in (None, 25.5, 3600):
            raw = deepcopy(config.CONFIG)
            if value is None:
                raw["workflow"].pop("model_wait_timeout_seconds", None)
            else:
                raw["workflow"]["model_wait_timeout_seconds"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                loaded = runpy.run_path(config.__file__)
                expected = 180 if value is None else value
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

    def test_criterion_attempts_are_configurable_and_default_to_three(self):
        for value in (None, 2, 3):
            raw = deepcopy(config.CONFIG)
            if value is None:
                raw["workflow"].pop("criterion_retry_max_attempts", None)
            else:
                raw["workflow"]["criterion_retry_max_attempts"] = value
            with self.subTest(value=value), patch("yaml.safe_load", return_value=raw), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                loaded = runpy.run_path(config.__file__)
                self.assertEqual(loaded["CRITERION_RETRY_MAX_ATTEMPTS"], value or 3)

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

    def test_lease_must_cover_execution_and_cleanup_margin(self):
        raw = deepcopy(config.CONFIG)
        raw["model_semaphore"]["llm"]["lease_seconds"] = raw["workflow"]["model_call_timeout_seconds"]
        with patch.object(config, "CONFIG", raw), self.assertRaisesRegex(RuntimeError, "plus 5 seconds"):
            config._model_semaphore_config("llm", default_max=5)

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


if __name__ == "__main__":
    unittest.main()
