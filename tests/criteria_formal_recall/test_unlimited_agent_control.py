"""None removes optional agent limits without bypassing unrecoverable input errors."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import BaseModel

from agents.json_utils import AgentOutputValidationError, chat_until_valid_json
from agents.orchestrator import OrchestratorAgent
from endpoints.runtime.errors import ModelInputTooLong


class _Answer(BaseModel):
    value: int


class UnlimitedAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_model_retries_allows_json_repair_until_valid(self):
        agent = SimpleNamespace(name="test", chat=AsyncMock(side_effect=["bad", "bad", '{"value": 3}']))
        with patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", None):
            result, _ = await chat_until_valid_json(agent, "task", _Answer, '{"value": 3}')
        self.assertEqual(result, {"value": 3})
        self.assertEqual(agent.chat.await_count, 3)

    async def test_explicit_json_limit_still_applies_with_unlimited_global_retries(self):
        agent = SimpleNamespace(name="test", chat=AsyncMock(return_value="bad"))
        with patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", None):
            with self.assertRaises(AgentOutputValidationError):
                await chat_until_valid_json(agent, "task", _Answer, "{}", max_attempts=2)
        self.assertEqual(agent.chat.await_count, 2)

    async def test_input_too_long_never_enters_json_repair(self):
        agent = SimpleNamespace(name="test", chat=AsyncMock(side_effect=ModelInputTooLong("too long")))
        with patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", None):
            with self.assertRaises(ModelInputTooLong):
                await chat_until_valid_json(agent, "task", _Answer, "{}")
        self.assertEqual(agent.chat.await_count, 1)

    async def test_unlimited_criterion_recovery_stops_after_success(self):
        agent = OrchestratorAgent()
        result = {"criterion_id": "C01", "status": "COMPLIANT", "tokens": 0}
        agent.execute_single_criterion = AsyncMock(side_effect=[RuntimeError("transient"), result])
        actual = await agent._execute_criterion_with_recovery({"id": "C01"}, max_attempts=None)
        self.assertEqual(actual, result)
        self.assertEqual(agent.execute_single_criterion.await_count, 2)

    async def test_input_too_long_never_enters_criterion_recovery(self):
        agent = OrchestratorAgent()
        agent.execute_single_criterion = AsyncMock(side_effect=ModelInputTooLong("too long"))
        for attempts in (3, None):
            with self.subTest(attempts=attempts):
                agent.execute_single_criterion.reset_mock()
                with self.assertRaises(ModelInputTooLong):
                    await agent._execute_criterion_with_recovery({"id": "C01"}, max_attempts=attempts)
                self.assertEqual(agent.execute_single_criterion.await_count, 1)

    async def test_null_orchestrator_concurrency_does_not_construct_semaphore(self):
        agent = OrchestratorAgent()
        async def execute(criterion, *, max_attempts):
            await asyncio.sleep(0)
            return {"criterion_id": criterion["id"], "status": "COMPLIANT", "tokens": 0}
        agent._execute_criterion_with_recovery = execute
        criteria = [{"id": "C01", "criterion": "first"}, {"id": "C02", "criterion": "second"}]
        with patch("agents.orchestrator.MAX_ORCHESTRATOR_CONCURRENCY", None), \
                patch("agents.orchestrator.asyncio.Semaphore", side_effect=AssertionError("no cap")):
            result = await agent.execute_criteria(criteria)
        self.assertEqual([item["criterion_id"] for item in result], ["C01", "C02"])


if __name__ == "__main__":
    unittest.main()
