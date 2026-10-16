"""Prompt contracts and routing checks; these do not measure LLM accuracy."""

import unittest

from agents.prompts import (
    REFLECTOR_SYSTEM_PROMPT,
    SUB_AGENT_BASE_PROMPT,
    SUMMARIZER_SYSTEM_PROMPT,
)
from agents.summarizer import _summary_issue_records
from workflow.workflow import _prepare_inline_results


class AnnotationEvidencePromptTests(unittest.TestCase):
    def test_reviewer_and_reflector_distinguish_filled_values_and_instructions(self):
        for prompt in (SUB_AGENT_BASE_PROMPT, REFLECTOR_SYSTEM_PROMPT):
            with self.subTest(prompt=prompt[:30]):
                for text in ("【5‰】", "【5%】", "【45】", "（照抄）", "主体", "anchors=[]"):
                    self.assertIn(text, prompt)

    def test_reflector_rejects_masked_names_and_coarse_addresses(self):
        for text in ("连续星号", "仅填写省、市或区县", "签署栏为 edit"):
            self.assertIn(text, REFLECTOR_SYSTEM_PROMPT)

    def test_both_reviewers_use_the_same_body_attachment_conflict_boundary(self):
        for prompt in (SUB_AGENT_BASE_PROMPT, REFLECTOR_SYSTEM_PROMPT):
            with self.subTest(prompt=prompt[:30]):
                for text in (
                    "同一事项、适用同一条件、比较同一指标",
                    "只有互斥约定才构成冲突",
                    "概括与细则互补",
                    "响应、到场处理、排障和最终修复是不同阶段",
                    "正文“免费保修两年”",
                    "附件“免费保修三年”",
                    "quoted_text 只用于定位现有原文，comment_text 才提供修改建议",
                    "归属未提供时不得断言编号错误",
                ):
                    self.assertIn(text, prompt)

    def test_reflector_uses_explicit_audit_mode_and_accepts_resolved_removals(self):
        for text in (
            "finding_history",
            "改变前轮决定时",
            "audit_mode.coverage=true",
            "audit_mode.comparison=true",
            "不能开启对应审计",
            "允许 PASS 同时包含 remove",
            "PASS 不得包含 revise",
            "feedback 只解释结构化决定",
        ):
            self.assertIn(text, REFLECTOR_SYSTEM_PROMPT)

    def test_authorization_uncertainty_survives_all_model_stages(self):
        for prompt in (SUB_AGENT_BASE_PROMPT, REFLECTOR_SYSTEM_PROMPT, SUMMARIZER_SYSTEM_PROMPT):
            with self.subTest(prompt=prompt[:30]):
                for text in ("待核实", "分公司", "代理人", "签署无效"):
                    self.assertIn(text, prompt)

    def test_unanchored_pending_authority_goes_to_summary_unchanged(self):
        comment = "待核实事项：需核实乙方是否已取得相应签约授权；现有材料不足以确认。"
        results = [{
            "criterion_id": "C5", "criterion": "核对分公司授权", "section": "主体",
            "issues": [{
                "issue_id": "5.1", "risk_level": "low", "issue_comment": comment,
                "anchors": [], "reasoning": "外部授权材料未提供，不能断言未授权。",
                "check_point": "核对分公司授权",
            }],
        }]
        inline, notices = _prepare_inline_results(results)
        self.assertEqual(inline, [])
        self.assertEqual(notices, [])
        self.assertEqual(_summary_issue_records(results)[0]["issue_comment"], comment)


if __name__ == "__main__":
    unittest.main()
