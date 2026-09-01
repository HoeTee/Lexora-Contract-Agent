"""SummarizerAgent produces the opening comment for the annotated DOCX."""

import json

from agents.base_agent import Agent
from agents.json_utils import chat_until_valid_json
from agents.prompts.cn_prompts import SUMMARIZER_SYSTEM_PROMPT
from agents.review_presentation import prompt_summary_records
from agents.schemas import SummaryContentOutput, SummaryOutput


SUMMARY_EXPECTED_JSON = """
{
  "overall_comment": "总体审查总结",
  "priority_comments": [
    "优先修改建议1",
    "优先修改建议2"
  ]
}
"""


class SummarizerAgent(Agent):
    """Summarizes canonical review issues for the DOCX opening comment."""

    def __init__(self, **kwargs):
        super().__init__(
            system_prompt=SUMMARIZER_SYSTEM_PROMPT,
            name="Summarizer",
            **kwargs,
        )

    async def compile_summary_comment(self, results: list[dict]) -> dict:
        """Generate overall_comment and priority_comments from canonical issues."""
        issues = prompt_summary_records(results)
        prompt_payload = {
            "criteria_count": len(results),
            "issue_count": len(issues),
            "issues": issues,
        }
        prompt = (
            "以下问题已由程序完成去重、批注位置选择和总结分流。"
            "请只生成总体审查总结和优先修改建议：\n"
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
