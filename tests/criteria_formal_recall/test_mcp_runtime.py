import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mcp_service.client.client import _stdio_environment
from mcp_service.server.server import docxindex_build_index
from workflow.workflow import ContractReviewWorkflow


class MCPRuntimeTests(unittest.TestCase):
    def test_stdio_environment_preserves_tokenizer_cache_without_overrides(self) -> None:
        with patch.dict(os.environ, {"TIKTOKEN_CACHE_DIR": "/opt/tiktoken-cache"}, clear=True):
            environment = _stdio_environment(None)

        self.assertEqual(environment["TIKTOKEN_CACHE_DIR"], "/opt/tiktoken-cache")

    def test_stdio_environment_applies_task_overrides(self) -> None:
        with patch.dict(os.environ, {"LLM_MODEL_NAME": "default"}, clear=True):
            environment = _stdio_environment({"LLM_MODEL_NAME": "task-model"})

        self.assertEqual(environment["LLM_MODEL_NAME"], "task-model")

    def test_failed_index_build_does_not_export_artifacts(self) -> None:
        error_payload = {
            "error": {
                "error_type": "ConnectionError",
                "detail": "offline tokenizer data unavailable",
            }
        }
        retriever = SimpleNamespace(
            build_index=AsyncMock(return_value=json.dumps(error_payload)),
            artifact_snapshot=lambda: self.fail("artifacts must not be exported after a failed build"),
        )

        with patch("mcp_service.server.server._get_docxindex_retriever", return_value=retriever):
            result = asyncio.run(
                docxindex_build_index("contract.docx", artifact_dir="unused-after-failure")
            )

        self.assertEqual(json.loads(result), error_payload)

    def test_workflow_preserves_non_json_mcp_error(self) -> None:
        workflow = ContractReviewWorkflow.__new__(ContractReviewWorkflow)
        workflow.client = SimpleNamespace(
            call_tool=AsyncMock(return_value="Error executing tool: original index failure")
        )
        workflow.api_events_path = None
        workflow.index_artifacts_path = None
        workflow.logger = SimpleNamespace(log=lambda **_kwargs: None)

        with self.assertRaisesRegex(RuntimeError, "original index failure"):
            asyncio.run(workflow._phase_build_index("contract.docx"))


if __name__ == "__main__":
    unittest.main()
