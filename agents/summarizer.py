"""SummarizerAgent produces the opening comment for the annotated DOCX."""

import json
import re
from copy import deepcopy

from agents.base_agent import Agent, estimate_chat_prompt_tokens
from config import MAX_PROMPT_TOKENS
from agents.json_utils import chat_until_valid_json
from agents.prompts.cn_prompts import SUMMARIZER_SYSTEM_PROMPT
from agents.schemas import SummaryContentOutput, SummaryOutput


SUMMARY_EXPECTED_JSON = """
{
  "overall_comment": "总体审查总结",
  "priority_comments": [
    "优先修改建议1",
    "优先修改建议2"
  ],
  "duplicate_groups": [
    {
      "keep_issue_key": "0:H01:H01.1",
      "merge_issue_keys": ["1:H15:H15.1"],
      "canonical_comment": "能够脱离其他批注独立理解的唯一修改意见",
      "reason": "两条问题描述的是同一原文缺陷"
    }
  ]
}
"""

SUMMARIZER_MAX_PROMPT_TOKENS = MAX_PROMPT_TOKENS


def _normalize_user_facing_text(value: str) -> str:
    """Remove internal review jargon from a user-facing summary."""
    replacements = (
        ("合同条款序号存在多处断号", "合同条款编号多处不连续"),
        ("条款序号存在多处断号", "条款编号多处不连续"),
        ("合同条款断号严重", "合同条款编号多处不连续"),
        ("条款断号严重", "条款编号多处不连续"),
        ("断号严重", "编号多处不连续"),
        ("序号断号", "编号不连续"),
        ("断号", "编号不连续"),
    )
    rendered = value
    for source, replacement in replacements:
        rendered = rendered.replace(source, replacement)
    rendered = re.sub(r"path:(?:body|frontmatter|tail|attachments)/\S+", "", rendered)
    return rendered.strip()


def _summary_issue_records(results: list[dict]) -> list[dict]:
    """Flatten confirmed issues into the fields consumed by Summarizer."""
    records: list[dict] = []
    for result_index, result in enumerate(results):
        for issue in result.get("issues", []):
            issue_key = _issue_key(result_index, result, issue)
            records.append(
                {
                    "issue_key": issue_key,
                    "section": result.get("section", ""),
                    "criterion_id": result.get("criterion_id", ""),
                    "risk_level": issue.get("risk_level", ""),
                    "issue_comment": issue.get("issue_comment", ""),
                    "check_point": issue.get("check_point", ""),
                    "placement": (result.get("placement") or {}).get("target", "inline"),
                    "anchors": [
                        {
                            "xml_anchor_id": anchor.get("xml_anchor_id", ""),
                            "quoted_text": anchor.get("quoted_text", ""),
                            "comment_text": anchor.get("comment_text", ""),
                        }
                        for anchor in issue.get("anchors") or []
                        if anchor.get("role", "edit") == "edit"
                    ],
                }
            )
    return records


def _issue_key(result_index: int, result: dict, issue: dict) -> str:
    return f"{result_index}:{result.get('criterion_id', '')}:{issue.get('issue_id', '')}"


def _edit_anchor_signature(issue: dict) -> tuple[tuple[str, str, str], ...] | None:
    """Return the exact editable source locations represented by an issue."""
    anchors = [
        (
            str(anchor.get("xml_anchor_type") or ""),
            str(anchor.get("xml_anchor_id") or ""),
            "".join(str(anchor.get("quoted_text") or "").split()),
        )
        for anchor in issue.get("anchors") or []
        if anchor.get("role", "edit") == "edit"
    ]
    if not anchors or any(not all(anchor) for anchor in anchors):
        return None
    return tuple(sorted(anchors))


def apply_summary_duplicate_groups(
    results: list[dict],
    duplicate_groups: list[dict],
) -> tuple[list[dict], dict[str, int]]:
    """Apply validated semantic duplicate groups without adding another model call."""
    canonical = deepcopy(results)
    issue_lookup: dict[str, tuple[int, int]] = {}
    for result_index, result in enumerate(canonical):
        for issue_index, issue in enumerate(result.get("issues") or []):
            issue_lookup[_issue_key(result_index, result, issue)] = (result_index, issue_index)

    merged_keys: set[str] = set()
    comments: dict[str, str] = {}
    applied_groups = 0
    skipped_groups = 0
    for group in duplicate_groups:
        keep_key = str(group.get("keep_issue_key") or "")
        merge_keys = [str(key) for key in group.get("merge_issue_keys") or []]
        members = [keep_key, *merge_keys]
        member_records = [
            (canonical[result_index], canonical[result_index]["issues"][issue_index])
            for key in members
            if key in issue_lookup
            for result_index, issue_index in [issue_lookup[key]]
        ]
        anchor_signatures = {
            signature
            for _, issue in member_records
            if (signature := _edit_anchor_signature(issue)) is not None
        }
        placements = {
            str((result.get("placement") or {}).get("target", "inline"))
            for result, _ in member_records
        }
        if (
            not keep_key
            or not merge_keys
            or any(key not in issue_lookup for key in members)
            or keep_key in merged_keys
            or any(key in merged_keys for key in merge_keys)
            or len(member_records) != len(members)
            or len(anchor_signatures) != 1
            or any(_edit_anchor_signature(issue) is None for _, issue in member_records)
            or len(placements) != 1
        ):
            skipped_groups += 1
            continue
        merged_keys.update(merge_keys)
        comments[keep_key] = str(group.get("canonical_comment") or "").strip()
        applied_groups += 1

    removed_issues = 0
    for result_index, result in enumerate(canonical):
        kept: list[dict] = []
        for issue in result.get("issues") or []:
            key = _issue_key(result_index, result, issue)
            if key in merged_keys:
                removed_issues += 1
                continue
            canonical_comment = comments.get(key)
            if canonical_comment:
                issue["issue_comment"] = canonical_comment
                for anchor in issue.get("anchors") or []:
                    if anchor.get("role", "edit") == "edit":
                        anchor["comment_text"] = canonical_comment
            kept.append(issue)
        result["issues"] = kept
    return canonical, {
        "applied_groups": applied_groups,
        "skipped_groups": skipped_groups,
        "removed_duplicate_issues": removed_issues,
    }


def conservative_message_token_upper_bound(messages: list[dict]) -> int:
    """Compatibility wrapper around the global chat prompt estimator."""
    return estimate_chat_prompt_tokens(messages)


def normalize_summary_output(summary: dict) -> dict:
    """Normalize LLM wording before it reaches the user-facing DOCX comment."""
    return {
        "overall_comment": _normalize_user_facing_text(
            str(summary.get("overall_comment", ""))
        ),
        "priority_comments": [
            _normalize_user_facing_text(str(item))
            for item in summary.get("priority_comments", [])
        ],
        "duplicate_groups": [
            {
                **group,
                "canonical_comment": _normalize_user_facing_text(
                    str(group.get("canonical_comment", ""))
                ),
            }
            for group in summary.get("duplicate_groups", [])
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

    async def compile_summary_comment(self, results: list[dict]) -> dict:
        """Generate overall_comment and priority_comments from canonical issues."""
        issues = _summary_issue_records(results)
        prompt_payload = {
            "criteria_count": len(results),
            "issue_count": len(issues),
            "issues": issues,
        }
        prompt = (
            "以下问题已完成精确范围防重，但仍可能存在跨审查要点或相邻位置的语义重复。"
            "请生成总体审查总结、优先修改建议，并只把真正描述同一缺陷的问题放入 duplicate_groups。"
            "只有 edit anchor 的 xml_anchor_id 和 quoted_text 均完全相同，且 placement 相同的问题才允许合并。"
            "无锚点汇总问题不得吸收有锚点的行内问题；同属未填写不代表同一缺陷，合同号、日期、地址等不同字段必须分别保留。"
            "不同事实、不同位置、不同修改动作不得合并；keep_issue_key 应选择更具体、更可执行的问题：\n"
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
