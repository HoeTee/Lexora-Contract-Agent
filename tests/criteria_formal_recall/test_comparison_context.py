from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agents.orchestrator import OrchestratorAgent
from routing.schemas import ReviewExecutionPlan
from tools.docxindex.retriever import _anchor_source_region, _format_anchor_context


def _index() -> dict:
    return {
        "nodes": [
            {"node_id": "body", "start_index": 10, "end_index": 67},
            {"node_id": "tail", "start_index": 68, "end_index": 81},
            {"node_id": "attachments", "start_index": 90, "end_index": 100},
        ],
        "anchor_map": {
            "p_44": {
                "body_child_index": 44, "type": "p", "node_id": "body/section_a",
                "raw_text": "The invoice details are listed in the appendix Invoice List.",
            },
            "p_67": {
                "body_child_index": 67, "type": "p", "node_id": "body/section_b",
                "raw_text": "Appendix: Invoice List",
            },
            "p_96": {
                "body_child_index": 96, "type": "p", "node_id": "attachments/appendix_a",
                "raw_text": "Invoice List",
            },
        },
    }


def _context(*, include_appendix: bool = False) -> str:
    selected = [{"node_id": "scan/body/batch_2", "start_index": 44, "end_index": 67}]
    if include_appendix:
        selected.append({"node_id": "scan/attachments/batch_1", "start_index": 96, "end_index": 96})
    return _format_anchor_context(_index(), selected)


def _execution() -> ReviewExecutionPlan:
    return ReviewExecutionPlan(audit_mode={"comparison": True}, evidence_region="attachments")


def _not_found_opinion() -> dict:
    return {
        "status": "issues_found",
        "issues": [
            {
                "issue_id": f"appendix.{position}", "risk_level": "low",
                "issue_comment": "The referenced appendix was not provided.",
                "anchors": [{
                    "role": "edit", "xml_anchor_type": "paragraph",
                    "xml_anchor_id": f"path:body/p{position}", "quoted_text": text,
                    "comment_text": "Please provide the referenced appendix.",
                }],
                "reasoning": "No counterpart is present in the provided appendix evidence.",
                "criterion": "Compare referenced appendices", "check_point": "Compare each reference with its appendix.",
            }
            for position, text in (
                (44, "The invoice details are listed in the appendix Invoice List."),
                (67, "Appendix: Invoice List"),
            )
        ],
        "coverage_decisions": [],
        "comparison_decisions": [
            {
                "edit_anchor_type": "paragraph", "edit_anchor_id": f"path:body/p{position}",
                "evidence_anchor_type": None, "evidence_anchor_id": None,
                "decision": "not_found", "reason": "No counterpart was provided.",
            }
            for position in (44, 67)
        ],
    }


class ComparisonContextTests(unittest.TestCase):
    def test_scan_candidates_keep_real_body_and_attachment_owners(self) -> None:
        index = _index()
        before = deepcopy(index)
        context = _format_anchor_context(index, [
            {"node_id": "scan/body/batch_2", "start_index": 44, "end_index": 67},
            {"node_id": "region/attachments/result_1", "start_index": 96, "end_index": 96},
        ])

        evidence = OrchestratorAgent._retrieval_anchor_evidence(context)

        self.assertEqual([(item["xml_anchor_id"], item["source_region"]) for item in evidence], [
            ("path:body/p44", "body"), ("path:body/p67", "body"), ("path:body/p96", "attachments"),
        ])
        self.assertNotIn("source_region: scan", context)
        self.assertNotIn("source_region: region", context)
        self.assertEqual(index, before)

    def test_missing_owner_uses_real_region_boundaries_and_not_scan_id(self) -> None:
        index = _index()
        index["anchor_map"]["p_67"].pop("node_id")
        context = _format_anchor_context(index, [
            {"node_id": "scan/attachments/misleading", "start_index": 67, "end_index": 67},
        ])

        self.assertEqual(OrchestratorAgent._retrieval_anchor_evidence(context)[0]["source_region"], "body")
        self.assertEqual(_anchor_source_region({"nodes": [{
            "node_id": "scan/attachments/misleading", "start_index": 67, "end_index": 67,
        }]}, {"body_child_index": 67}), "")
        index["nodes"].append({"node_id": "attachments", "start_index": 67, "end_index": 67})
        self.assertEqual(_anchor_source_region(index, {"body_child_index": 67}), "")

    def test_body_directory_stays_target_when_no_actual_appendix_was_retrieved(self) -> None:
        comparison = OrchestratorAgent._comparison_context(_execution(), _context())

        self.assertEqual(comparison, {
            "edit_region": "", "evidence_region": "attachments",
            "targets": [{"xml_anchor_type": "paragraph", "xml_anchor_id": f"path:body/p{position}",
                         "source_region": "body"} for position in (44, 67)],
            "evidence": [],
        })
        self.assertEqual(OrchestratorAgent._execution_output_errors(
            _not_found_opinion(), _execution(), _context(),
        ), [])

    def test_body_directory_cannot_be_used_as_matched_or_mismatch_evidence(self) -> None:
        for outcome in ("matched", "mismatch"):
            with self.subTest(outcome=outcome):
                opinion = _not_found_opinion()
                opinion["comparison_decisions"][0].update({
                    "decision": outcome, "evidence_anchor_type": "paragraph",
                    "evidence_anchor_id": "path:body/p67",
                })
                errors = OrchestratorAgent._execution_output_errors(opinion, _execution(), _context())
                self.assertTrue(any("path:body/p67" in error for error in errors), errors)

        opinion = _not_found_opinion()
        opinion["comparison_decisions"].pop()
        errors = OrchestratorAgent._execution_output_errors(opinion, _execution(), _context())
        self.assertTrue(any("path:body/p67" in error for error in errors), errors)

    def test_subagent_and_reflector_share_exact_scope_with_explicit_empty_evidence(self) -> None:
        execution, context = _execution(), _context()
        scope = OrchestratorAgent._comparison_context(execution, context)
        prompt = OrchestratorAgent._comparison_prompt(execution, context)
        reflector_input = json.loads(OrchestratorAgent()._build_reflector_input(
            "Compare referenced appendices", ["Keep the original checkpoint."], _not_found_opinion(),
            context=context, execution=execution,
        ))

        self.assertIn(json.dumps(scope, ensure_ascii=False, indent=2), prompt)
        self.assertEqual(reflector_input["comparison_context"], scope)
        self.assertEqual(reflector_input["check_points"], ["Keep the original checkpoint."])
        self.assertEqual(reflector_input["comparison_context"]["evidence"], [])

    def test_first_supplemental_anchor_stays_separate_and_enters_comparison_scope(self) -> None:
        index, base = _index(), _context()
        supplemental = _format_anchor_context(index, [
            {"node_id": "scan/attachments/extra", "start_index": 96, "end_index": 96},
        ])
        agent = SimpleNamespace(_review_context=base, messages=[{"role": "tool", "content": supplemental}])
        orchestrator = OrchestratorAgent()

        merged = orchestrator._review_evidence_context(base, agent)
        parsed = orchestrator._retrieval_anchor_evidence(merged)
        originals = {item["xml_anchor_id"]: item for item in parsed}

        self.assertEqual([item["xml_anchor_id"] for item in parsed], [
            "path:body/p44", "path:body/p67", "path:body/p96",
        ])
        self.assertEqual(originals["path:body/p67"]["content"], "Appendix: Invoice List")
        self.assertEqual(originals["path:body/p96"]["content"], "Invoice List")
        self.assertNotIn("source_region:", originals["path:body/p67"]["content"])
        scope = orchestrator._comparison_context(_execution(), merged)
        self.assertEqual([item["xml_anchor_id"] for item in scope["evidence"]], ["path:body/p96"])
        opinion = {
            "status": "compliant", "issues": [],
            "comparison_decisions": [
                {"edit_anchor_type": "paragraph", "edit_anchor_id": f"path:body/p{position}",
                 "evidence_anchor_type": "paragraph", "evidence_anchor_id": "path:body/p96",
                 "decision": "matched", "reason": "The provided appendix matches the reference."}
                for position in (44, 67)
            ],
        }
        self.assertEqual(orchestrator._execution_output_errors(opinion, _execution(), merged), [])
        opinion["comparison_decisions"][0]["evidence_anchor_id"] = "path:body/p999"
        errors = orchestrator._execution_output_errors(opinion, _execution(), merged)
        self.assertTrue(any("path:body/p999" in error for error in errors), errors)

    def test_repeated_and_incremental_supplements_preserve_all_original_anchors(self) -> None:
        index, base = _index(), _context()
        for position in (97, 98):
            index["anchor_map"][f"p_{position}"] = {
                "body_child_index": position, "type": "p", "node_id": "attachments/appendix_a",
                "raw_text": f"Additional appendix text {position}",
            }
        first = _format_anchor_context(index, [
            {"node_id": "scan/attachments/extra", "start_index": 96, "end_index": 97},
        ])
        second = _format_anchor_context(index, [
            {"node_id": "scan/attachments/later", "start_index": 98, "end_index": 98},
        ])
        agent = SimpleNamespace(_review_context=base, messages=[
            {"role": "tool", "content": first}, {"role": "tool", "content": first},
        ])
        orchestrator = OrchestratorAgent()
        merged = orchestrator._review_evidence_context(base, agent)
        agent._review_context = merged
        self.assertEqual(orchestrator._review_evidence_context(base, agent), merged)
        agent.messages.append({"role": "tool", "content": second})
        merged = orchestrator._review_evidence_context(base, agent)
        parsed = orchestrator._retrieval_anchor_evidence(merged)
        self.assertEqual([item["xml_anchor_id"] for item in parsed], [
            "path:body/p44", "path:body/p67", "path:body/p96", "path:body/p97", "path:body/p98",
        ])
        originals = {item["xml_anchor_id"]: item for item in parsed}
        self.assertEqual(originals["path:body/p67"]["content"], "Appendix: Invoice List")
        self.assertEqual(originals["path:body/p97"]["content"], "Additional appendix text 97")
        self.assertEqual(originals["path:body/p98"]["content"], "Additional appendix text 98")


class ComparisonContextExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_review_and_reflection_receive_the_same_comparison_scope(self) -> None:
        context, execution, opinion = _context(), _execution(), _not_found_opinion()
        scope = OrchestratorAgent._comparison_context(execution, context)
        outer = self

        class FakeAgent:
            def __init__(self, **kwargs):
                self.name = kwargs["name"]
                self.messages = [{"role": "system", "content": kwargs["system_prompt"]}]
                self.token_usage = {"total_tokens": 0}

        class FakeReflector:
            def __init__(self, **kwargs):
                self.token_usage = {"total_tokens": 0}

            async def review(self, agent_output, **kwargs):
                payload = json.loads(agent_output)
                outer.assertEqual(payload["comparison_context"], scope)
                return {
                    "status": "PASS", "feedback": "Both references have no counterpart in scope.",
                    "finding_decisions": [{"finding_id": item["finding_id"], "decision": "confirm",
                                           "reason": "No permitted counterpart was provided."}
                                          for item in payload["current_findings"]],
                    "coverage_gaps": [],
                }

        async def response(_agent, prompt, *_args, **_kwargs):
            self.assertIn(json.dumps(scope, ensure_ascii=False, indent=2), prompt)
            return deepcopy(opinion), json.dumps(opinion)

        orchestrator = OrchestratorAgent()
        with (
            patch("agents.orchestrator.Agent", FakeAgent),
            patch("agents.orchestrator.ReflectorAgent", FakeReflector),
            patch("agents.orchestrator.chat_until_valid_json", response),
            patch.object(orchestrator, "_retrieve_context", AsyncMock(return_value=(context, 0))),
            patch.object(orchestrator, "_get_tools", AsyncMock(return_value=[])),
            patch.object(orchestrator, "_build_missing_text_review_notes", AsyncMock(return_value="")),
            patch("agents.orchestrator.MAX_REFLECTION_ROUNDS", 1),
        ):
            result = await orchestrator.execute_single_criterion({
                "id": "U77", "criterion": "Compare referenced appendices", "review_source": "uploaded",
                "check_points": ["Compare each reference with its appendix."],
                "execution": execution.model_dump(mode="json"),
            })

        self.assertEqual(result["quality_status"], "passed")
        self.assertEqual(result["comparison_decisions"], opinion["comparison_decisions"])

    async def test_each_revision_rebuilds_scope_from_latest_contract_search_evidence(self) -> None:
        execution, original_context = _execution(), _context()
        original_scope = OrchestratorAgent._comparison_context(execution, original_context)
        agent = SimpleNamespace(
            name="SubAgent_U77", messages=[{"role": "system", "content": "system"},
                                           {"role": "user", "content": "stale feedback"}],
            _review_task_prompt="Review originals:\n" + original_context,
            _review_base_context=original_context, _review_context=original_context,
            _review_execution=execution,
        )
        value = _not_found_opinion()

        for include_appendix in (False, True):
            agent._review_context = _context(include_appendix=include_appendix)
            scope = OrchestratorAgent._comparison_context(execution, agent._review_context)

            async def response(actual_agent, prompt, *_args, **_kwargs):
                task = actual_agent.messages[1]["content"]
                self.assertNotIn("stale feedback", json.dumps(actual_agent.messages))
                self.assertIn(json.dumps(scope, ensure_ascii=False, indent=2), task)
                if include_appendix:
                    self.assertNotIn(json.dumps(original_scope, ensure_ascii=False, indent=2), task)
                    self.assertEqual(scope["evidence"][0]["xml_anchor_id"], "path:body/p96")
                self.assertIn("current correction", prompt)
                return deepcopy(value), "raw"

            with patch("agents.orchestrator.chat_until_valid_json", response):
                await OrchestratorAgent._revise_subagent_output(agent, "current correction", value)

            reflector_input = json.loads(OrchestratorAgent()._build_reflector_input(
                "Compare referenced appendices", [], value, context=agent._review_context, execution=execution,
            ))
            self.assertEqual(reflector_input["comparison_context"], scope)


if __name__ == "__main__":
    unittest.main()
