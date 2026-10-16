"""Validate executable reflection decisions and their existing retry budget."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from agents.base_agent import Settings
from agents.json_utils import AgentOutputValidationError
from agents.reflector import ReflectorAgent
from agents.schemas import ReflectorOutput
from endpoints.runtime.model_budget import ModelCallBudget
from loggers.call_log import reset_call_context, set_call_context
from tests.criteria_formal_recall._model_test_support import model_transport


def reflection(status="PASS", decision="remove", finding_id="F001", *, gaps=None):
    return {
        "status": status,
        "feedback": "revise F001",  # Structured decisions determine the action.
        "finding_decisions": [{
            "finding_id": finding_id,
            "decision": decision,
            "reason": "正文概括与附件细则能够同时成立，不构成冲突。",
        }],
        "coverage_gaps": gaps or [],
    }


def coverage_gap():
    return {
        "xml_anchor_type": "paragraph",
        "xml_anchor_id": "path:body/p1",
        "quoted_text": "合同编号：",
        "reason": "编号字段未填写且当前结果未报告。",
    }


class ReflectorOutputContractTests(unittest.TestCase):
    def test_pass_accepts_confirm_and_remove_including_the_last_finding(self):
        for decision in ("confirm", "remove"):
            with self.subTest(decision=decision):
                parsed = ReflectorOutput.model_validate(reflection(decision=decision))
                self.assertEqual(parsed.status, "PASS")
                self.assertEqual(parsed.finding_decisions[0].decision, decision)

    def test_pass_cannot_leave_a_revision_or_coverage_gap(self):
        for output in (
            reflection(decision="revise"),
            reflection(gaps=[coverage_gap()]),
        ):
            with self.subTest(output=output), self.assertRaises(ValidationError):
                ReflectorOutput.model_validate(output)

    def test_reject_must_identify_executable_remaining_work(self):
        for decisions in ([], reflection()["finding_decisions"], reflection(decision="confirm")["finding_decisions"]):
            output = {"status": "REJECT", "finding_decisions": decisions}
            with self.subTest(decisions=decisions), self.assertRaisesRegex(ValidationError, "REJECT requires"):
                ReflectorOutput.model_validate(output)

    def test_reject_accepts_a_revision_or_a_concrete_gap(self):
        for output in (
            reflection(status="REJECT", decision="revise"),
            reflection(status="REJECT", gaps=[coverage_gap()]),
        ):
            with self.subTest(output=output):
                self.assertEqual(ReflectorOutput.model_validate(output).status, "REJECT")

    def test_context_requires_exact_input_finding_ids(self):
        context = {"required_finding_ids": {"F001", "F002"}}
        for output in (reflection(), reflection(finding_id="F003")):
            with self.subTest(output=output), self.assertRaisesRegex(ValidationError, "cover exactly"):
                ReflectorOutput.model_validate(output, context=context)
        output = reflection()
        output["finding_decisions"].append(reflection(finding_id="F002")["finding_decisions"][0])
        self.assertEqual(len(ReflectorOutput.model_validate(output, context=context).finding_decisions), 2)

    def test_pass_cannot_confirm_a_missing_historical_finding(self):
        context = {"required_finding_ids": {"F001"}, "missing_finding_ids": {"F001"}}
        with self.assertRaisesRegex(ValidationError, "missing_previous_findings"):
            ReflectorOutput.model_validate(reflection(decision="confirm"), context=context)
        self.assertEqual(ReflectorOutput.model_validate(reflection(), context=context).status, "PASS")
        rejected = reflection(status="REJECT", decision="revise")
        self.assertEqual(ReflectorOutput.model_validate(rejected, context=context).status, "REJECT")

    def test_duplicate_finding_ids_remain_invalid(self):
        output = reflection()
        output["finding_decisions"] *= 2
        with self.assertRaisesRegex(ValidationError, "at most once"):
            ReflectorOutput.model_validate(output)

    def test_reject_accepts_confirmation_of_a_missing_finding_to_restore(self):
        context = {"required_finding_ids": {"F001"}, "missing_finding_ids": {"F001"}}
        output = reflection(status="REJECT", decision="confirm")
        parsed = ReflectorOutput.model_validate(output, context=context)
        self.assertEqual(parsed.status, "REJECT")
        self.assertEqual(parsed.finding_decisions[0].decision, "confirm")
        with self.assertRaisesRegex(ValidationError, "REJECT requires"):
            ReflectorOutput.model_validate(output, context={"required_finding_ids": {"F001"}})


class ReflectorSchemaRepairTests(unittest.IsolatedAsyncioTestCase):
    async def run_reflector(self, responses, *, max_retries=1, expected_failure=False):
        sent_requests = []
        budgets = []
        sequence = iter(responses)

        def handler(request):
            sent_requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0, "model": "fake",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(next(sequence))},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            })

        def budget_factory():
            budget = ModelCallBudget(timeout_seconds=60, max_retries=max_retries)
            budgets.append(budget)
            return budget

        task = json.dumps({
            "current_findings": [{"finding_id": "F001"}],
            "missing_previous_findings": [],
            "finding_history": [],
            "audit_mode": {"coverage": False, "comparison": False},
        })
        with tempfile.TemporaryDirectory() as directory, \
             model_transport("agents.base_agent", handler), \
             patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", max_retries), \
             patch("loggers.call_log.ModelCallBudget", side_effect=budget_factory):
            logs = Path(directory) / "logs"
            token = set_call_context(logs / "events.log", criterion_id="H11", stage="reflection-round10")
            reflector = ReflectorAgent(criterion_id="H11", settings=Settings(api_key="fake", model="fake"))
            try:
                if expected_failure:
                    with self.assertRaises(AgentOutputValidationError):
                        await reflector.review(task)
                    result = None
                else:
                    result = await reflector.review(task)
                records = [json.loads(path.read_text(encoding="utf-8")) for path in (logs / "logical_calls").glob("*.json")]
            finally:
                reset_call_context(token)
                await reflector.client.close()
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0].timeout_seconds, 60)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["send_count"], len(sent_requests))
        self.assertEqual(budgets[0].sends, len(sent_requests))
        return result, sent_requests, records[0]

    async def test_remove_only_rejection_repairs_to_pass_within_one_logical_call(self):
        result, requests, record = await self.run_reflector([reflection(status="REJECT"), reflection()])
        self.assertEqual((result["status"], result["finding_decisions"][0]["decision"]), ("PASS", "remove"))
        self.assertIn("REJECT requires", requests[1]["messages"][-1]["content"])
        self.assertEqual((record["status"], record["retry_count"]), ("succeeded", 1))
        self.assertEqual([attempt["send_number"] for attempt in record["attempts"]], [1, 2])

    async def test_unknown_and_missing_finding_decisions_repair_in_existing_loop(self):
        result, requests, record = await self.run_reflector([reflection(finding_id="F999"), reflection()])
        self.assertEqual(result["finding_decisions"][0]["finding_id"], "F001")
        self.assertIn("unknown=['F999']; missing=['F001']", requests[1]["messages"][-1]["content"])
        self.assertEqual(record["retry_count"], 1)

    async def test_invalid_reflection_never_resets_the_send_allowance(self):
        _, requests, record = await self.run_reflector(
            [reflection(status="REJECT")] * 3, expected_failure=True,
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual((record["status"], record["retry_count"]), ("failed", 1))


if __name__ == "__main__":
    unittest.main()
