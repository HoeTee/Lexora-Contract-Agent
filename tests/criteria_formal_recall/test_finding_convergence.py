import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.orchestrator import OrchestratorAgent, _FindingLedger
from routing.schemas import ReviewExecutionPlan


def opinion():
    return {
        "status": "issues_found",
        "issues": [{
            "issue_id": "1", "risk_level": "medium", "issue_comment": "Potential conflict",
            "anchors": [{"role": "edit", "xml_anchor_type": "paragraph",
                         "xml_anchor_id": "path:body/p1", "quoted_text": "Three-year warranty",
                         "comment_text": "Clarify the warranty"}],
            "reasoning": "Check whether both promises can hold", "criterion": "Consistency",
            "check_point": "Compare the same obligation",
        }],
        "coverage_decisions": [{"xml_anchor_type": "paragraph", "xml_anchor_id": "path:body/p1",
                                "decision": "issue", "reason": "Potential conflict"}],
        "comparison_decisions": [],
    }


def decision(action="remove", status="PASS"):
    return {"status": status, "feedback": "No conflicting promise",
            "finding_decisions": [{"finding_id": "F001", "decision": action,
                                   "reason": "A summary and its details are compatible"}],
            "coverage_gaps": []}


class FindingConvergenceTests(unittest.TestCase):
    def test_last_round_removal_resolves_finding_and_coverage(self):
        value = opinion()
        ledger = _FindingLedger(value)
        self.assertTrue(ledger.review(value, decision(), 10))
        result = ledger.apply_removals(value)
        self.assertEqual(result["status"], "compliant")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["coverage_decisions"][0]["decision"], "compliant")
        self.assertEqual(ledger.unresolved_issues(result), [])
        self.assertEqual(value["status"], "issues_found")

    def test_pass_cannot_hide_pending_work_or_unknown_findings(self):
        for change in ("revise", "gap", "unknown", "missing", "duplicate"):
            with self.subTest(change=change):
                value = opinion()
                review = decision()
                if change == "revise":
                    review["finding_decisions"][0]["decision"] = "revise"
                elif change == "gap":
                    review["coverage_gaps"] = [{"xml_anchor_id": "path:body/p2"}]
                elif change == "unknown":
                    review["finding_decisions"][0]["finding_id"] = "F999"
                elif change == "missing":
                    review["finding_decisions"] = []
                else:
                    review["finding_decisions"] *= 2
                self.assertFalse(_FindingLedger(value).review(value, review, 10))

    def test_empty_rejection_never_finishes_via_empty_all(self):
        value = {"status": "compliant", "issues": []}
        self.assertFalse(_FindingLedger(value).review(value, {
            "status": "REJECT", "finding_decisions": [], "coverage_gaps": [],
        }, 10))

    def test_missing_historical_confirmation_requires_restoration(self):
        value = opinion()
        ledger = _FindingLedger(value)
        empty = {"status": "compliant", "issues": []}
        self.assertFalse(ledger.review(empty, decision("confirm"), 10))
        self.assertTrue(ledger.review(empty, decision("remove", "REJECT"), 10))

    def test_removed_finding_keeps_bounded_reasons_for_next_reviewer(self):
        value = opinion()
        ledger = _FindingLedger(value)
        for round_number, action in enumerate(("revise", "confirm", "remove"), 1):
            ledger.review(value, decision(action), round_number)
        history = ledger.decision_history()
        self.assertEqual([d["decision"] for d in history[0]["decisions"]], ["confirm", "remove"])
        self.assertEqual(history[0]["location"]["xml_anchor_id"], "path:body/p1")
        self.assertIn("compatible", history[0]["decisions"][-1]["reason"])

    def test_removal_does_not_silently_reclassify_a_pairwise_mismatch(self):
        value = opinion()
        value["comparison_decisions"] = [{
            "edit_anchor_type": "paragraph", "edit_anchor_id": "path:body/p1",
            "evidence_anchor_type": "paragraph", "evidence_anchor_id": "path:body/p2",
            "decision": "mismatch", "reason": "Different promises",
        }]
        ledger = _FindingLedger(value)
        ledger.review(value, decision(), 1)
        resolved = ledger.apply_removals(value)
        context = (
            "source_region: body\nxml_anchor_type: paragraph\nxml_anchor_id: path:body/p1\n内容：\nThree-year warranty"
            "\n\n---\n\nsource_region: attachments\nxml_anchor_type: paragraph\nxml_anchor_id: path:body/p2\n内容：\nTwo-year warranty"
        )
        self.assertEqual(resolved["comparison_decisions"][0]["decision"], "mismatch")
        self.assertTrue(OrchestratorAgent._execution_output_errors(
            resolved, ReviewExecutionPlan(audit_mode={"comparison": True}, evidence_region="attachments"), context,
        ))


class OrchestratorConvergenceTests(unittest.IsolatedAsyncioTestCase):
    async def run_final_round(self, review):
        value = opinion()
        context = "source_region: body\nxml_anchor_type: paragraph\nxml_anchor_id: path:body/p1\n内容：\nThree-year warranty"

        class FakeAgent:
            def __init__(self, **kwargs):
                self.name = kwargs["name"]
                self.messages = [{"role": "system", "content": kwargs["system_prompt"]}]
                self.token_usage = {"total_tokens": 0}

        class FakeReflector:
            def __init__(self, **kwargs):
                self.token_usage = {"total_tokens": 0}

            async def review(self, agent_output, **kwargs):
                data = json.loads(agent_output)
                self_outer.assertEqual(data["audit_mode"], {"coverage": True, "comparison": False})
                return copy.deepcopy(review)

        self_outer = self
        self.events = []
        logger = SimpleNamespace(log=lambda **event: self.events.append(event))
        orchestrator = OrchestratorAgent(logger=logger)
        with patch("agents.orchestrator.Agent", FakeAgent), patch("agents.orchestrator.ReflectorAgent", FakeReflector), \
                patch("agents.orchestrator.chat_until_valid_json", AsyncMock(return_value=(value, json.dumps(value)))), \
                patch.object(orchestrator, "_retrieve_context", AsyncMock(return_value=(context, 0))), \
                patch.object(orchestrator, "_get_tools", AsyncMock(return_value=[])), \
                patch("agents.orchestrator.MAX_REFLECTION_ROUNDS", 1):
            result = await orchestrator.execute_single_criterion({
                "id": "U77", "criterion": "Consistency", "review_source": "uploaded",
                "execution": {"audit_mode": {"coverage": True}},
            })
        return result

    async def test_last_round_rejected_removal_finishes_without_another_model_call(self):
        result = await self.run_final_round(decision(status="REJECT"))
        self.assertEqual(result["quality_status"], "passed")
        self.assertEqual(result["status"], "COMPLIANT")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["unresolved_issues"], [])
        self.assertEqual(result["quality_history"][-1]["review"]["status"], "REJECT")
        self.assertEqual(result["quality_history"][-1]["effective_status"], "PASS")
        event = next(event for event in self.events if event["phase"] == "Reflect")
        logged = json.loads(event["output_summary"])
        self.assertEqual((logged["reported_status"], logged["effective_status"]), ("REJECT", "PASS"))

    async def test_last_round_pass_with_pending_revision_has_no_final_conclusion(self):
        result = await self.run_final_round(decision(action="revise", status="PASS"))
        self.assertEqual(result["quality_status"], "incomplete")
        self.assertIsNone(result["status"])
        self.assertEqual(len(result["unresolved_issues"]), 1)
        self.assertEqual(result["quality_history"][-1]["effective_status"], "REJECT")

    async def test_revision_discards_old_feedback_and_preserves_current_evidence(self):
        value = opinion()
        agent = SimpleNamespace(
            name="SubAgent_H11", messages=[{"role": "system", "content": "system"},
                                           {"role": "user", "content": "obsolete contradictory feedback"}],
            _review_task_prompt="Review BASE", _review_base_context="BASE", _review_context="BASE plus attachment evidence",
        )
        async def response(actual_agent, prompt, *args, **kwargs):
            self.assertNotIn("obsolete", json.dumps(actual_agent.messages))
            self.assertIn("attachment evidence", actual_agent.messages[1]["content"])
            self.assertIn("Potential conflict", prompt)
            self.assertIn("current instruction", prompt)
            return copy.deepcopy(value), "raw"

        with patch("agents.orchestrator.chat_until_valid_json", response):
            await OrchestratorAgent._revise_subagent_output(agent, "current instruction", value)

    async def test_unsolicited_arrays_do_not_enable_audit(self):
        value = opinion()
        value["comparison_decisions"] = [{"unrequested": True}]
        orchestrator = OrchestratorAgent()
        with patch.object(orchestrator, "_review_evidence_context", return_value=""), \
                patch.object(orchestrator, "_validate_and_repair_anchors", return_value={}), \
                patch.object(orchestrator, "_anchor_validation_errors", return_value=[]):
            output, _, _, _ = await orchestrator._validate_and_revise_output(
                SimpleNamespace(), value, "raw", ReviewExecutionPlan(), "",
            )
        self.assertEqual(output["coverage_decisions"], [])
        self.assertEqual(output["comparison_decisions"], [])


if __name__ == "__main__":
    unittest.main()
