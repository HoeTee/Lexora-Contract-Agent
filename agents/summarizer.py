"""SummarizerAgent produces the opening comment for the annotated DOCX."""

import json
import re
from collections import Counter, defaultdict
from copy import deepcopy

from pydantic import ValidationError

from agents.base_agent import Agent, estimate_chat_prompt_tokens
from config import MAX_PROMPT_TOKENS
from agents.json_utils import chat_until_valid_json
from agents.prompts import get_prompt, render_prompt
from agents.schemas import SummaryContentOutput, SummaryDuplicateGroup, SummaryOutput


SUMMARIZER_SYSTEM_PROMPT = get_prompt("SUMMARIZER_SYSTEM_PROMPT")
SUMMARY_EXPECTED_JSON = get_prompt("SUMMARY_EXPECTED_JSON")

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
                            "xml_anchor_type": anchor.get("xml_anchor_type", ""),
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


def _summary_duplicate_candidates(issues: list[dict]) -> list[dict]:
    """Limit semantic comparisons to identical editable source and placement."""
    key_counts = Counter(issue["issue_key"] for issue in issues)
    buckets: dict[tuple, list[str]] = defaultdict(list)
    for issue in issues:
        signature = _edit_anchor_signature(issue)
        key = issue["issue_key"]
        if signature is not None and key_counts[key] == 1:
            buckets[(issue["placement"], signature)].append(key)
    return [
        {"issue_keys": keys}
        for keys in buckets.values()
        if len(keys) >= 2
    ]


def _validate_summary_duplicate_groups(
    results: list[dict], raw_groups: object,
) -> tuple[list[dict], list[dict]]:
    """Reject optional suggestions independently and retain every affected opinion."""
    if not isinstance(raw_groups, list):
        return [], [{
            "group_index": None,
            "reason": "invalid_group_container",
            "detail": "duplicate_groups must be an array",
        }]

    issues = _summary_issue_records(results)
    candidates = _summary_duplicate_candidates(issues)
    candidate_by_key = {
        key: candidate_index
        for candidate_index, candidate in enumerate(candidates)
        for key in candidate["issue_keys"]
    }
    key_counts = Counter(issue["issue_key"] for issue in issues)
    # Even an invalid group cannot share a member with a group we apply. This
    # conservatively retains opinions when suggestions overlap or form a cycle.
    references: dict[str, set[int]] = defaultdict(set)
    for index, group in enumerate(raw_groups):
        if isinstance(group, dict):
            keep_key = group.get("keep_issue_key")
            merge_keys = group.get("merge_issue_keys")
            keys = ([keep_key] if isinstance(keep_key, str) else []) + (
                [key for key in merge_keys if isinstance(key, str)]
                if isinstance(merge_keys, list) else []
            )
            for key in keys:
                references[key].add(index)

    accepted: list[dict] = []
    skipped: list[dict] = []
    for index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            skipped.append({
                "group_index": index, "reason": "invalid_group_schema", "detail": "group must be an object",
            })
            continue
        try:
            group = SummaryDuplicateGroup.model_validate(raw_group)
        except ValidationError as exc:
            skipped.append({
                "group_index": index,
                "reason": "invalid_group_schema",
                "detail": [
                    {"field": ".".join(map(str, error["loc"])), "message": error["msg"][:300]}
                    for error in exc.errors(include_url=False, include_input=False)[:8]
                ],
            })
            continue
        members = [group.keep_issue_key, *group.merge_issue_keys]
        unavailable = [key for key in members if key_counts[key] != 1]
        if unavailable:
            skipped.append({
                "group_index": index, "reason": "unknown_or_ambiguous_issue_key", "detail": unavailable,
            })
            continue
        candidate_indexes = {candidate_by_key.get(key) for key in members}
        if None in candidate_indexes or len(candidate_indexes) != 1:
            skipped.append({
                "group_index": index,
                "reason": "outside_duplicate_candidate",
                "detail": "members must share all edit anchor types, IDs, normalized quotes and placement",
            })
            continue
        overlaps = sorted({other for key in members for other in references[key] if other != index})
        if overlaps:
            skipped.append({
                "group_index": index, "reason": "overlapping_group_membership", "detail": overlaps,
            })
            continue
        accepted.append(group.model_dump())
    return accepted, skipped


def apply_summary_duplicate_groups(
    results: list[dict],
    duplicate_groups: object,
) -> tuple[list[dict], dict]:
    """Apply only independent suggestions in a program-generated candidate bucket."""
    groups, skipped = _validate_summary_duplicate_groups(results, duplicate_groups)
    canonical = deepcopy(results)
    merged_keys: set[str] = set()
    comments: dict[str, str] = {}
    for group in groups:
        merged_keys.update(group["merge_issue_keys"])
        comments[group["keep_issue_key"]] = group["canonical_comment"].strip()

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
        "applied_groups": len(groups),
        "skipped_groups": len(skipped),
        "skipped_group_details": skipped,
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
        self.call_stage = "summary"
        self.summary_metadata: dict = {}

    async def compile_summary_comment(self, results: list[dict]) -> dict:
        """Generate overall_comment and priority_comments from canonical issues."""
        issues = _summary_issue_records(results)
        duplicate_candidates = _summary_duplicate_candidates(issues)
        self.summary_metadata = {"candidate_group_count": len(duplicate_candidates)}
        prompt_payload = {
            "criteria_count": len(results),
            "issue_count": len(issues),
            "issues": issues,
            "duplicate_candidates": duplicate_candidates,
            "incomplete_reviews": [
                {
                    "criterion_id": result.get("criterion_id", ""),
                    "section": result.get("section", ""),
                    "criterion": result.get("criterion", ""),
                    "execution_status": (
                        "error" if result.get("status") == "ERROR" else "incomplete"
                    ),
                    "confirmed_issue_count": len(result.get("issues") or []),
                }
                for result in results
                if result.get("status") == "ERROR"
                or result.get("quality_status") == "incomplete"
            ],
        }
        prompt = render_prompt(
            "SUMMARIZER_TASK_PROMPT",
            prompt_payload_json=json.dumps(prompt_payload, ensure_ascii=False, indent=2),
        )
        parsed, _ = await chat_until_valid_json(
            self,
            prompt,
            SummaryOutput,
            SUMMARY_EXPECTED_JSON,
            final_attempt_schema=SummaryContentOutput,
        )
        groups, skipped = _validate_summary_duplicate_groups(results, parsed.get("duplicate_groups", []))
        parsed["duplicate_groups"] = groups
        self.summary_metadata.update({
            "accepted_group_count": len(groups),
            "skipped_group_count": len(skipped),
            "skipped_group_details": skipped,
            "text_limit_relaxed": (
                len(parsed["overall_comment"]) + sum(map(len, parsed["priority_comments"])) > 200
            ),
        })
        return normalize_summary_output(parsed)

    async def compile_report(self, results: list[dict]) -> dict:
        """Backward-compatible wrapper for callers not yet renamed."""
        return await self.compile_summary_comment(results)
