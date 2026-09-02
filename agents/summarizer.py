"""SummarizerAgent produces the opening comment for the annotated DOCX."""

import json

from agents.base_agent import Agent
from agents.json_utils import chat_until_valid_json
from agents.prompts.cn_prompts import SUMMARIZER_SYSTEM_PROMPT
from agents.review_presentation import (
    normalize_user_facing_review_text,
    prompt_summary_records,
)
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

SUMMARIZER_MAX_PROMPT_TOKENS = 90_000
_CHAT_MESSAGE_OVERHEAD_TOKENS = 64


def conservative_message_token_upper_bound(messages: list[dict]) -> int:
    """Return a byte-based upper bound for one chat request's prompt tokens."""
    content_bytes = sum(
        len(str(message.get("role", "")).encode("utf-8"))
        + len(str(message.get("content", "")).encode("utf-8"))
        for message in messages
    )
    return content_bytes + len(messages) * _CHAT_MESSAGE_OVERHEAD_TOKENS


def normalize_summary_output(summary: dict) -> dict:
    """Normalize LLM wording before it reaches the user-facing DOCX comment."""
    return {
        "overall_comment": normalize_user_facing_review_text(
            str(summary.get("overall_comment", ""))
        ),
        "priority_comments": [
            normalize_user_facing_review_text(str(item))
            for item in summary.get("priority_comments", [])
        ],
    }


class SummarizerAgent(Agent):
    """Summarizes canonical review issues for the DOCX opening comment."""

    def __init__(self, **kwargs):
        super().__init__(
            system_prompt=SUMMARIZER_SYSTEM_PROMPT,
            name="Summarizer",
            **kwargs,
        )

    def _check_context_limits(self):
        super()._check_context_limits()
        upper_bound = conservative_message_token_upper_bound(self.messages)
        if upper_bound > SUMMARIZER_MAX_PROMPT_TOKENS:
            raise ValueError(
                "Summarizer prompt exceeds the 90000-token safety limit "
                f"(conservative upper bound: {upper_bound})."
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
        return normalize_summary_output(parsed)

    async def compile_report(self, results: list[dict]) -> dict:
        """Backward-compatible wrapper for callers not yet renamed."""
        return await self.compile_summary_comment(results)
