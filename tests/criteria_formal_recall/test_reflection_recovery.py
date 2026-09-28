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
    async def test_reflector_model_failure_enters_criterion_recovery(self) -> None:
        orchestrator = OrchestratorAgent()

        with self.assertRaises(ModelCallError):
            await orchestrator._review_with_fallback(
                _FailingReflector(),
                cid="C01",
                round_num=1,
                reflector_input='{"subagent_output": {"status": "issues_found"}}',
                missing_text_review_notes="",
            )

    async def test_reflector_schema_failure_enters_criterion_recovery(self) -> None:
        orchestrator = OrchestratorAgent()

        with self.assertRaises(AgentOutputValidationError):
            await orchestrator._review_with_fallback(
                _InvalidJsonReflector(),
                cid="C04",
                round_num=1,
                reflector_input='{"subagent_output": {"status": "issues_found"}}',
                missing_text_review_notes="",
            )


if __name__ == "__main__":
    unittest.main()
