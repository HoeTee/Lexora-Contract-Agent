"""Keep optional duplicate suggestions from discarding valid summary text."""

import json
import unittest
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

from pydantic import ValidationError

from agents.json_utils import AgentOutputValidationError
from agents.schemas import SummaryContentOutput, SummaryDuplicateGroup, SummaryOutput
from agents.summarizer import (
    SummarizerAgent,
    _summary_duplicate_candidates,
    _summary_issue_records,
    apply_summary_duplicate_groups,
)
from endpoints.runtime.errors import ModelCallError


def _results(count=2):
    return [
        {
            "criterion_id": f"C{index}",
            "placement": {"target": "inline"},
            "issues": [{
                "issue_id": "1", "issue_comment": "请删除重复的原因。",
                "anchors": [{
                    "role": "edit", "xml_anchor_type": "paragraph",
                    "xml_anchor_id": "path:body/p1", "quoted_text": "因乙方原因原因",
                    "comment_text": "请删除重复的原因。",
                }],
            }],
        }
        for index in range(count)
    ]


def _group(keep=0, merge=1):
    return {
        "keep_issue_key": f"{keep}:C{keep}:1",
        "merge_issue_keys": [f"{merge}:C{merge}:1"],
        "canonical_comment": "删除重复的“原因”，改为“因乙方原因”。",
        "reason": "同一原文重复词、同一修改动作。",
    }


class DuplicateSuggestionTests(unittest.TestCase):
    def test_valid_group_merges_only_the_duplicate_and_keeps_input_unchanged(self):
        original = _results()
        snapshot = deepcopy(original)

        merged, stats = apply_summary_duplicate_groups(original, [_group()])

        self.assertEqual([len(result["issues"]) for result in merged], [1, 0])
        self.assertEqual(stats["removed_duplicate_issues"], 1)
        self.assertEqual(stats["skipped_group_details"], [])
        self.assertEqual(merged[0]["issues"][0]["anchors"][0]["comment_text"], _group()["canonical_comment"])
        self.assertEqual(original, snapshot)

    def test_empty_merge_group_is_optional_but_single_group_schema_stays_strict(self):
        group = dict(_group(), merge_issue_keys=[])
        summary = {"overall_comment": "请修正重复词。", "duplicate_groups": [group]}
        self.assertEqual(SummaryOutput.model_validate(summary).duplicate_groups, [group])
        with self.assertRaises(ValidationError):
            SummaryDuplicateGroup.model_validate(group)

        original = _results()
        merged, stats = apply_summary_duplicate_groups(original, [group])

        self.assertEqual(merged, original)
        self.assertEqual(stats["skipped_groups"], 1)
        self.assertEqual(stats["skipped_group_details"][0]["reason"], "invalid_group_schema")

    def test_candidate_bucket_requires_full_anchor_signature_and_placement(self):
        original = _results(6)
        original[1]["issues"][0]["anchors"][0]["quoted_text"] = "因 乙方 原因原因"
        original[2]["issues"][0]["anchors"][0]["xml_anchor_type"] = "table"
        original[3]["issues"][0]["anchors"][0]["xml_anchor_id"] = "path:body/p2"
        original[4]["issues"][0]["anchors"][0]["quoted_text"] = "另一段原文"
        original[5]["placement"] = {"target": "summary"}

        records = _summary_issue_records(original)
        candidates = _summary_duplicate_candidates(records)

        self.assertEqual(records[0]["anchors"][0]["xml_anchor_type"], "paragraph")
        self.assertEqual(candidates, [{"issue_keys": ["0:C0:1", "1:C1:1"]}])
        for index in range(2, 6):
            with self.subTest(index=index):
                merged, stats = apply_summary_duplicate_groups(original, [_group(0, index)])
                self.assertEqual(merged, original)
                self.assertEqual(stats["skipped_group_details"][0]["reason"], "outside_duplicate_candidate")

    def test_unanchored_issues_are_not_duplicate_candidates(self):
        original = _results()
        for result in original:
            result["issues"][0]["anchors"] = []
        self.assertEqual(_summary_duplicate_candidates(_summary_issue_records(original)), [])
        merged, stats = apply_summary_duplicate_groups(original, [_group()])
        self.assertEqual(merged, original)
        self.assertEqual(stats["removed_duplicate_issues"], 0)

    def test_duplicate_input_keys_cannot_be_overwritten_and_merged(self):
        original = _results()
        original[0]["issues"].append(deepcopy(original[0]["issues"][0]))

        self.assertEqual(_summary_duplicate_candidates(_summary_issue_records(original)), [])
        merged, stats = apply_summary_duplicate_groups(original, [_group()])

        self.assertEqual(merged, original)
        self.assertEqual(stats["skipped_group_details"][0]["reason"], "unknown_or_ambiguous_issue_key")

    def test_invalid_shapes_unknown_keys_and_non_self_contained_comments_preserve_opinions(self):
        invalid_groups = [
            None, "invalid", [],
            dict(_group(), keep_issue_key="missing"),
            dict(_group(), merge_issue_keys=["0:C0:1"]),
            dict(_group(), merge_issue_keys=["1:C1:1", "1:C1:1"]),
            dict(_group(), canonical_comment="同上"),
            dict(_group(), canonical_comment="请修改 path:body/p1"),
            dict(_group(), canonical_comment="   "),
            dict(_group(), extra_field="unsupported"),
        ]
        original = _results()
        for group in invalid_groups:
            with self.subTest(group=group):
                merged, stats = apply_summary_duplicate_groups(original, [group])
                self.assertEqual(merged, original)
                self.assertEqual(stats["skipped_groups"], 1)
                self.assertTrue(stats["skipped_group_details"][0]["detail"])
        for container in (None, {}, "invalid"):
            merged, stats = apply_summary_duplicate_groups(original, container)
            self.assertEqual(merged, original)
            self.assertEqual(stats["skipped_group_details"][0]["reason"], "invalid_group_container")

    def test_overlapping_chains_and_cycles_skip_every_related_group(self):
        original = _results(3)
        for groups in (
            [_group(0, 1), _group(1, 0)],
            [_group(0, 1), _group(1, 2)],
            [_group(0, 1), _group(0, 2)],
            [_group(0, 1), _group(2, 1)],
            [_group(0, 1), dict(_group(0, 2), merge_issue_keys=[])],
        ):
            with self.subTest(groups=groups):
                merged, stats = apply_summary_duplicate_groups(original, groups)
                self.assertEqual(merged, original)
                self.assertEqual(stats["applied_groups"], 0)
                self.assertEqual(stats["skipped_groups"], 2)

    def test_unrelated_valid_group_survives_an_invalid_suggestion(self):
        original = _results(3)
        merged, stats = apply_summary_duplicate_groups(original, [None, _group()])
        self.assertEqual([len(result["issues"]) for result in merged], [1, 0, 1])
        self.assertEqual(stats["applied_groups"], 1)
        self.assertEqual(stats["skipped_groups"], 1)

    def test_top_level_summary_fields_stay_strict(self):
        with self.assertRaises(ValidationError):
            SummaryContentOutput.model_validate({"overall_comment": "总结", "issue_decisions": []})
        with self.assertRaises(ValidationError):
            SummaryContentOutput.model_validate({"priority_comments": [""]})
        for schema in (SummaryOutput, SummaryContentOutput):
            for value in ({}, {"overall_comment": "  ", "priority_comments": []}):
                with self.subTest(schema=schema.__name__, value=value), self.assertRaises(ValidationError):
                    schema.model_validate(value)
            self.assertEqual(schema.model_validate({"priority_comments": ["请核实合同信息。"]}).overall_comment, "")


class _ReplaySummarizer(SummarizerAgent):
    def __init__(self, responses):
        self.name = "Summarizer"
        self.responses = iter(responses)
        self.prompts = []
        self.summary_metadata = {}
        self.scope_count = 0
        self.in_scope = False

    @contextmanager
    def validation_requests(self):
        self.scope_count += 1
        self.in_scope = True
        try:
            yield
        finally:
            self.in_scope = False

    async def chat(self, prompt):
        if not self.in_scope:
            raise AssertionError("summary sends must share the validation scope")
        self.prompts.append(prompt)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


class SummaryResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_noop_suggestion_does_not_request_model_repair(self):
        # This is the failing b274 response shape, without copying contract data.
        original = _results(3)
        group = dict(_group(2, 0), merge_issue_keys=[], reason="输入中无其他重复问题，无需合并。")
        agent = _ReplaySummarizer([{
            "overall_comment": "合同存在一处重复词，请修改。",
            "priority_comments": [], "duplicate_groups": [_group(), group],
        }])

        summary = await agent.compile_summary_comment(original)

        self.assertEqual(summary["duplicate_groups"], [_group()])
        self.assertEqual(len(agent.prompts), 1)
        self.assertEqual(agent.scope_count, 1)
        self.assertEqual(agent.summary_metadata["skipped_group_count"], 1)
        self.assertEqual(agent.summary_metadata["accepted_group_count"], 1)
        self.assertIn('"duplicate_candidates"', agent.prompts[0])
        self.assertIn('"xml_anchor_type": "paragraph"', agent.prompts[0])

    async def test_bad_group_container_does_not_discard_summary_text(self):
        agent = _ReplaySummarizer([{"overall_comment": "请修正重复词。", "duplicate_groups": {"invalid": True}}])

        summary = await agent.compile_summary_comment(_results())

        self.assertEqual(summary["overall_comment"], "请修正重复词。")
        self.assertEqual(summary["duplicate_groups"], [])
        self.assertEqual(agent.summary_metadata["skipped_group_details"][0]["reason"], "invalid_group_container")

    async def test_text_limit_fallback_and_repairs_share_one_existing_call_scope(self):
        response = {"overall_comment": "综" * 201, "duplicate_groups": [dict(_group(), merge_issue_keys=[])]}
        agent = _ReplaySummarizer([response, response])

        with patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", 1):
            summary = await agent.compile_summary_comment(_results())

        self.assertEqual(len(summary["overall_comment"]), 201)
        self.assertEqual(summary["duplicate_groups"], [])
        self.assertEqual(agent.scope_count, 1)
        self.assertEqual(len(agent.prompts), 2)
        self.assertTrue(agent.summary_metadata["text_limit_relaxed"])

    async def test_invalid_json_exhaustion_is_still_a_summary_failure(self):
        agent = _ReplaySummarizer(["not JSON", "still not JSON"])
        with patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", 1):
            with self.assertRaises(AgentOutputValidationError):
                await agent.compile_summary_comment(_results())
        self.assertEqual(agent.scope_count, 1)
        self.assertEqual(len(agent.prompts), 2)

    async def test_empty_summary_exhaustion_remains_a_failure_for_workflow_fallback(self):
        agent = _ReplaySummarizer([{}, {"overall_comment": "", "priority_comments": []}])
        with patch("agents.json_utils.MODEL_CALL_MAX_RETRIES", 1):
            with self.assertRaisesRegex(AgentOutputValidationError, "summary must contain"):
                await agent.compile_summary_comment(_results())
        self.assertEqual(agent.scope_count, 1)
        self.assertEqual(len(agent.prompts), 2)

    async def test_model_failure_propagates_without_creating_a_new_budget(self):
        error = ModelCallError("agent", "model deadline reached", error_type="model_call_timeout")
        agent = _ReplaySummarizer([error])
        with self.assertRaises(ModelCallError) as caught:
            await agent.compile_summary_comment(_results())
        self.assertIs(caught.exception, error)
        self.assertEqual(agent.scope_count, 1)
        self.assertEqual(len(agent.prompts), 1)


if __name__ == "__main__":
    unittest.main()
