import unittest

from agents.orchestrator import OrchestratorAgent
from agents.json_utils import AgentOutputValidationError
from endpoints.runtime.errors import ModelCallError


class _FailingReflector:
    token_usage = {"total_tokens": 0}

    async def review(self, **_kwargs):
        raise ModelCallError("agent", "Reflector: Request timed out.")


class _InvalidJsonReflector:
    token_usage = {"total_tokens": 0}

    async def review(self, **_kwargs):
        raise AgentOutputValidationError("Reflector output failed schema validation")


class ReflectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reflector_model_failure_preserves_valid_subagent_review(self) -> None:
        orchestrator = OrchestratorAgent()

        result = await orchestrator._review_with_fallback(
            _FailingReflector(),
            cid="C01",
            round_num=1,
            reflector_input='{"subagent_output": {"status": "issues_found"}}',
            missing_text_review_notes="",
        )

        self.assertIsNone(result)

    async def test_reflector_schema_failure_preserves_valid_subagent_review(self) -> None:
        orchestrator = OrchestratorAgent()

        result = await orchestrator._review_with_fallback(
            _InvalidJsonReflector(),
            cid="C04",
            round_num=1,
            reflector_input='{"subagent_output": {"status": "issues_found"}}',
            missing_text_review_notes="",
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
