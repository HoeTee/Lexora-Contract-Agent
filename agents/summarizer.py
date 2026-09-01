"""
SummarizerAgent produces the opening comment for the annotated DOCX.
"""

import json

from agents.base_agent import Agent
from agents.json_utils import chat_until_valid_json
from agents.prompts.cn_prompts import SUMMARIZER_SYSTEM_PROMPT
from agents.review_presentation import prompt_issue_records
from agents.schemas import SummaryContentOutput, SummaryOutput


SUMMARY_EXPECTED_JSON = """
{
  "overall_comment": "总体审查总结",
  "priority_comments": [
    "优先修改建议1",
    "优先修改建议2"
  ],
  "issue_decisions": [
    {
      "source_issue_keys": ["C01::C01.1"],
      "output_scope": "inline",
      "merged_comment": "可直接写入 Word 的具体修改意见",
      "selected_anchors": [
        {
          "anchor_key": "C01::C01.1#anchor_1",
          "comment_text": "该锚点对应的直接修改意见"
        }
      ]
    },
    {
      "source_issue_keys": ["C02::C02.1", "C03::C03.1"],
      "output_scope": "summary_only",
      "merged_comment": "合并后的合同整体问题",
      "selected_anchors": []
    }
  ]
}
"""


class SummarizerAgent(Agent):
    """Summarizes structured review issues for the DOCX opening comment."""

    def __init__(self, **kwargs):
        super().__init__(
            system_prompt=SUMMARIZER_SYSTEM_PROMPT,
            name="Summarizer",
            **kwargs,
        )

    async def compile_summary_comment(self, results: list[dict]) -> dict:
        """Generate the concise opening DOCX summary comment."""
        issues = prompt_issue_records(results)

        prompt_payload = {
            "criteria_count": len(results),
            "issue_count": len(issues),
            "issues": issues,
        }
        prompt = (
            "请根据以下结构化审查问题，生成总体总结并完成最终批注分流：\n"
            + json.dumps(prompt_payload, ensure_ascii=False, indent=2)
        )
        parsed, _ = await chat_until_valid_json(
            self,
            prompt,
            SummaryOutput,
            SUMMARY_EXPECTED_JSON,
            final_attempt_schema=SummaryContentOutput,
        )
        return parsed

    async def compile_report(self, results: list[dict]) -> dict:
        """Backward-compatible wrapper for callers not yet renamed."""
        return await self.compile_summary_comment(results)
