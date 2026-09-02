"""Prepare deterministic inline annotations and summary-only review issues."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any


_GLOBAL_STRUCTURE_TERMS = ("条款序号", "标题顺序", "断号", "序号杂乱", "序号不连续")
_MISSING_STRUCTURE_TERMS = ("合同框架", "必备标题", "关键标题", "缺失多项")
_DURATION_SUMMARY_TERMS = ("合同履行期限", "起止日期不明确", "补充合同履行期限")
_QUOTED_TEXT_PATTERN = re.compile(r"[“\"]([^”\"]+)[”\"]")
_PRESCRIBED_TEXT_PATTERN = re.compile(r"(?:写明|写入)[“\"](.+)[”\"]")
_REPLACEMENT_PATTERN = re.compile(r"(?:修改|改)为[：:]?[“\"]?([^”\"。；]+)")
_REFERENCE_ONLY_TERMS = ("作为修正基准", "作为正文引用的修正基准", "作为对比", "用于对比", "此处为正确", "此处为附件")
_TEXT_QUALITY_TERMS = ("错别字", "语病", "重复")
_ATTACHMENT_HEADING_PATTERN = re.compile(r"^附件[一二三四五六七八九十\d]+[：:]")


@dataclass(frozen=True)
class ReviewPresentation:
    """Inputs for the DOCX writer and the summary-only LLM call."""

    inline_results: list[dict]
    summarizer_results: list[dict]
    opening_notices: list[str]
    global_issues: list[str]


def collect_issue_records(results: list[dict]) -> list[dict[str, Any]]:
    """Flatten raw results into stable issue and anchor records."""
    records: list[dict[str, Any]] = []
    used_issue_keys: set[str] = set()
    for result_index, result in enumerate(results):
        criterion_id = str(result.get("criterion_id") or f"criterion_{result_index + 1}")
        for issue_index, issue in enumerate(result.get("issues") or []):
            issue_id = str(issue.get("issue_id") or f"issue_{issue_index + 1}")
            base_key = f"{criterion_id}::{issue_id}"
            issue_key = base_key
            suffix = 2
            while issue_key in used_issue_keys:
                issue_key = f"{base_key}::{suffix}"
                suffix += 1
            used_issue_keys.add(issue_key)

            anchors = [
                {
                    "anchor_key": f"{issue_key}#anchor_{anchor_index + 1}",
                    "xml_anchor_type": anchor.get("xml_anchor_type", ""),
                    "xml_anchor_id": anchor.get("xml_anchor_id", ""),
                    "quoted_text": anchor.get("quoted_text", ""),
                    "comment_text": anchor.get("comment_text", ""),
                }
                for anchor_index, anchor in enumerate(issue.get("anchors") or [])
            ]
            records.append(
                {
                    "issue_key": issue_key,
                    "section": result.get("section", ""),
                    "criterion_id": criterion_id,
                    "criterion": result.get("criterion", ""),
                    "issue_id": issue_id,
                    "risk_level": issue.get("risk_level", ""),
                    "issue_comment": issue.get("issue_comment", ""),
                    "reasoning": issue.get("reasoning", ""),
                    "check_point": issue.get("check_point", ""),
                    "anchors": anchors,
                    "_result": result,
                    "_issue": issue,
                }
            )
    return records


def prompt_issue_records(results: list[dict]) -> list[dict[str, Any]]:
    """Return the complete LLM-safe issue view; retained for trace compatibility."""
    return [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in collect_issue_records(results)
    ]


def prompt_summary_records(results: list[dict]) -> list[dict[str, Any]]:
    """Return only the fields needed to write an overall review summary."""
    return [
        {
            "section": record["section"],
            "criterion_id": record["criterion_id"],
            "risk_level": record["risk_level"],
            "issue_comment": record["issue_comment"],
            "check_point": record["check_point"],
        }
        for record in collect_issue_records(results)
    ]


def build_review_presentation(results: list[dict]) -> ReviewPresentation:
    """Route every raw issue to one canonical inline annotation or the summary.

    Input: reviewer result dictionaries containing issues and XML anchors.
    Output: canonical inline writer input, complete summarizer input, and
    deterministic summary-only comments. No issue may be silently discarded.
    """
    records = collect_issue_records(results)
    inline_results: list[dict] = []
    summary_results: list[dict] = []
    opening_notices: list[str] = []
    global_issues: list[str] = []
    represented: set[str] = set()
    annotations: dict[tuple[str, str, str, str], dict] = {}

    for record in records:
        issue_key = record["issue_key"]
        if _is_summary_only(record):
            comment = _summary_comment(record)
            summary_results.append(_materialize_result(record, [], comment))
            _append_summary_comment(record, comment, opening_notices, global_issues)
            represented.add(issue_key)
            continue

        fixed_instruction = _extract_fixed_instruction(record)
        selected_anchors = []
        duplicate_issues: list[dict] = []
        source_anchors = [
            anchor
            for anchor in record["_issue"].get("anchors") or []
            if _valid_anchor(anchor)
        ]
        substantive_anchors = [
            anchor for anchor in source_anchors if not _is_reference_only_anchor(anchor)
        ]
        if substantive_anchors:
            source_anchors = substantive_anchors
        if fixed_instruction:
            source_anchors = source_anchors[:1]
        for source_anchor in source_anchors:
            anchor = deepcopy(source_anchor)
            comment = fixed_instruction or str(anchor.get("comment_text") or record["issue_comment"]).strip()
            anchor["comment_text"] = comment
            annotation_key = _annotation_key(anchor, comment)
            duplicate_issue = _find_duplicate_annotation(
                annotations,
                annotation_key,
                record,
            )
            if duplicate_issue is not None:
                duplicate_issues.append(duplicate_issue)
                continue
            selected_anchors.append(anchor)

        if not selected_anchors:
            if duplicate_issues:
                for duplicate_issue in duplicate_issues:
                    source_keys = duplicate_issue.setdefault("source_issue_keys", [])
                    if issue_key not in source_keys:
                        source_keys.append(issue_key)
                represented.add(issue_key)
            else:
                comment = _summary_comment(record)
                summary_results.append(_materialize_result(record, [], comment))
                _append_summary_comment(record, comment, opening_notices, global_issues)
                represented.add(issue_key)
            continue

        rendered = _materialize_result(
            record,
            selected_anchors,
            fixed_instruction or str(record["issue_comment"]).strip(),
        )
        inline_results.append(rendered)
        rendered_issue = rendered["issues"][0]
        for anchor in selected_anchors:
            annotations[_annotation_key(anchor, anchor["comment_text"])] = rendered_issue
        represented.add(issue_key)

    expected = {record["issue_key"] for record in records}
    if represented != expected:
        missing = sorted(expected - represented)
        raise ValueError(f"Review presentation omitted source issues: {missing}")

    return ReviewPresentation(
        inline_results=inline_results,
        summarizer_results=deepcopy(inline_results) + deepcopy(summary_results),
        opening_notices=opening_notices,
        global_issues=global_issues,
    )


def _materialize_result(record: dict[str, Any], anchors: list[dict], comment: str) -> dict:
    return {
        "section": record["section"],
        "criterion_id": record["criterion_id"],
        "criterion": record["criterion"],
        "status": "issues_found",
        "issues": [
            {
                "issue_id": record["issue_id"],
                "risk_level": record["risk_level"],
                "issue_comment": comment,
                "anchors": anchors,
                "reasoning": record["reasoning"],
                "criterion": record["criterion"],
                "check_point": record["check_point"],
                "source_issue_keys": [record["issue_key"]],
            }
        ],
    }


def _is_summary_only(record: dict[str, Any]) -> bool:
    anchors = record.get("anchors") or []
    if not anchors:
        return True
    if _is_keyword_triggered_opening_notice(record):
        return True
    context = " ".join(
        str(record.get(field) or "")
        for field in ("criterion", "check_point", "issue_comment")
    )
    if any(term in context for term in _DURATION_SUMMARY_TERMS):
        return True
    if "缺失" in context and any(term in context for term in _MISSING_STRUCTURE_TERMS):
        return True
    return any(term in context for term in _GLOBAL_STRUCTURE_TERMS)


def _is_keyword_triggered_opening_notice(record: dict[str, Any]) -> bool:
    """Identify fixed opening notices triggered only by a title keyword."""
    if not _extract_fixed_instruction(record):
        return False
    context = " ".join(
        str(record.get(field) or "")
        for field in ("criterion", "check_point")
    )
    return (
        "出现" in context
        and "首部" in context
        and any(term in context for term in ("合同名称", "合同目录", "标题"))
    )


def _is_opening_notice(record: dict[str, Any]) -> bool:
    if not _extract_fixed_instruction(record):
        return False
    context = " ".join(
        str(record.get(field) or "")
        for field in ("criterion", "check_point")
    )
    return "首部" in context


def _append_summary_comment(
    record: dict[str, Any],
    comment: str,
    opening_notices: list[str],
    global_issues: list[str],
) -> None:
    if not comment:
        return
    target = opening_notices if _is_opening_notice(record) else global_issues
    rendered = comment if target is opening_notices else normalize_user_facing_review_text(comment)
    if rendered not in target:
        target.append(rendered)


def normalize_user_facing_review_text(comment: str) -> str:
    """Replace internal review jargon in user-facing summary text."""
    replacements = (
        ("合同条款序号存在多处断号", "合同条款编号多处不连续"),
        ("条款序号存在多处断号", "条款编号多处不连续"),
        ("合同条款断号严重", "合同条款编号多处不连续"),
        ("条款断号严重", "条款编号多处不连续"),
        ("断号严重", "编号多处不连续"),
        ("序号断号", "编号不连续"),
        ("断号", "编号不连续"),
    )
    rendered = comment
    for source, replacement in replacements:
        rendered = rendered.replace(source, replacement)
    return rendered


def _extract_fixed_instruction(record: dict[str, Any]) -> str | None:
    """Extract prescribed wording only from the current check point."""
    source = str(record.get("check_point") or "")
    prescribed = [
        value.strip()
        for value in _PRESCRIBED_TEXT_PATTERN.findall(source)
        if value.strip().startswith(("注意：", "补充"))
    ]
    if prescribed:
        return max(prescribed, key=len)
    candidates = [
        value.strip()
        for value in _QUOTED_TEXT_PATTERN.findall(source)
        if value.strip().startswith(("注意：", "补充"))
    ]
    return max(candidates, key=len) if candidates else None


def _valid_anchor(anchor: dict) -> bool:
    return bool(
        str(anchor.get("xml_anchor_type") or "").strip()
        and str(anchor.get("xml_anchor_id") or "").strip()
        and str(anchor.get("quoted_text") or "").strip()
    )


def _is_reference_only_anchor(anchor: dict) -> bool:
    comment = str(anchor.get("comment_text") or "")
    quoted_text = str(anchor.get("quoted_text") or "").strip()
    return (
        any(term in comment for term in _REFERENCE_ONLY_TERMS)
        or (
            bool(_ATTACHMENT_HEADING_PATTERN.match(quoted_text))
            and "正文" in comment
        )
    )


def _summary_comment(record: dict[str, Any]) -> str:
    return (
        _extract_fixed_instruction(record)
        or str(record.get("issue_comment") or record.get("check_point") or "").strip()
    )


def _annotation_key(anchor: dict, comment: str) -> tuple[str, str, str, str]:
    replacement = _REPLACEMENT_PATTERN.search(comment)
    action = replacement.group(1) if replacement else comment
    return (
        str(anchor.get("xml_anchor_type") or "").strip(),
        str(anchor.get("xml_anchor_id") or "").strip(),
        _normalize_text(str(anchor.get("quoted_text") or "")),
        _normalize_text(action),
    )


def _find_duplicate_annotation(
    annotations: dict[tuple[str, str, str, str], dict],
    candidate: tuple[str, str, str, str],
    record: dict[str, Any],
) -> dict | None:
    exact = annotations.get(candidate)
    if exact is not None:
        return exact
    candidate_range = candidate[:3]
    candidate_action = candidate[3]
    for existing_key, issue in annotations.items():
        if existing_key[:3] != candidate_range:
            continue
        existing_criterion = str(issue.get("criterion") or "")
        candidate_criterion = str(record.get("criterion") or "")
        if (
            any(term in existing_criterion for term in _TEXT_QUALITY_TERMS)
            and any(term in candidate_criterion for term in _TEXT_QUALITY_TERMS)
        ):
            return issue
        existing_action = existing_key[3]
        if (
            len(candidate_action) >= 4
            and len(existing_action) >= 4
            and (
                candidate_action in existing_action
                or existing_action in candidate_action
            )
        ):
            return issue
    return None


def _normalize_text(value: str) -> str:
    value = re.sub(r"^(?:建议|请)", "", value.strip())
    return re.sub(r"[\s，。：；、“”\"']", "", value)
