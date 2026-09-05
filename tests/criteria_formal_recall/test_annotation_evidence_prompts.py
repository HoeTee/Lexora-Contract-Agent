"""Prompt contracts and routing checks; these do not measure LLM accuracy."""

import unittest

from agents.prompts.cn_prompts import (
    REFLECTOR_SYSTEM_PROMPT,
    SUB_AGENT_BASE_PROMPT,
    SUMMARIZER_SYSTEM_PROMPT,
)
from agents.review_presentation import build_review_presentation, prompt_summary_records


class AnnotationEvidencePromptTests(unittest.TestCase):
    def test_reviewer_and_reflector_distinguish_filled_values_and_instructions(self):
        for prompt in (SUB_AGENT_BASE_PROMPT, REFLECTOR_SYSTEM_PROMPT):
            with self.subTest(prompt=prompt[:30]):
                for text in ("【5‰】", "【5%】", "【45】", "（照抄）", "主体", "anchors=[]"):
                    self.assertIn(text, prompt)

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
        presentation = build_review_presentation(results)
        self.assertEqual(presentation.inline_results, [])
        self.assertEqual(presentation.global_issues, [comment])
        self.assertEqual(prompt_summary_records(presentation.summarizer_results)[0]["issue_comment"], comment)


if __name__ == "__main__":
    unittest.main()
