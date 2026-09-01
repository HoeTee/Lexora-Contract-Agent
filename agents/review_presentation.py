"""Prepare stable issue keys and apply the final DOCX presentation decisions."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


_RISK_ORDER = {"low": 1, "medium": 2, "high": 3}
_QUOTED_TEXT_PATTERN = re.compile(r"“([^”]+)”")
_GLOBAL_STRUCTURE_TERMS = ("条款序号", "标题顺序", "断号", "序号杂乱")


def collect_issue_records(results: list[dict]) -> list[dict[str, Any]]:
    """Flatten raw results into stable issue and anchor records for the finalizer."""
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

            anchors = []
            for anchor_index, anchor in enumerate(issue.get("anchors") or []):
                anchors.append(
                    {
                        "anchor_key": f"{issue_key}#anchor_{anchor_index + 1}",
                        "xml_anchor_type": anchor.get("xml_anchor_type", ""),
                        "xml_anchor_id": anchor.get("xml_anchor_id", ""),
                        "quoted_text": anchor.get("quoted_text", ""),
                        "comment_text": anchor.get("comment_text", ""),
                    }
                )

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
    """Return the LLM-safe view of raw issue records."""
    records = collect_issue_records(results)
    return [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in records
    ]


def apply_issue_decisions(results: list[dict], final_review: dict) -> list[dict]:
    """Materialize validated finalizer decisions into report-writer input.

    Unknown or omitted issue keys fall back to their original inline result so an
    imperfect finalizer response cannot silently reduce review recall.
    """
    records = collect_issue_records(results)
    record_by_key = {record["issue_key"]: record for record in records}
    anchor_by_key = {
        anchor["anchor_key"]: (record, original_anchor)
        for record in records
        for anchor, original_anchor in zip(
            record["anchors"],
            record["_issue"].get("anchors") or [],
            strict=True,
        )
    }
    consumed: set[str] = set()
    rendered: list[dict] = []

    for decision_index, decision in enumerate(final_review.get("issue_decisions") or []):
        source_keys = [
            key for key in decision.get("source_issue_keys") or []
            if key in record_by_key and key not in consumed
        ]
        if not source_keys:
            continue
        source_records = [record_by_key[key] for key in source_keys]
        source_key_set = set(source_keys)
        selected_anchors = []
        for annotation in decision.get("selected_anchors") or []:
            anchor_key = annotation.get("anchor_key", "")
            mapped = anchor_by_key.get(anchor_key)
            if mapped is None or mapped[0]["issue_key"] not in source_key_set:
                continue
            anchor = deepcopy(mapped[1])
            anchor["comment_text"] = annotation.get("comment_text", "")
            selected_anchors.append(anchor)

        scope = decision.get("output_scope")
        if scope in {"inline", "both"} and not selected_anchors:
            continue

        consumed.update(source_keys)
        if scope in {"summary_only", "discard"}:
            continue

        primary = source_records[0]
        risk_level = max(
            (record.get("risk_level", "low") for record in source_records),
            key=lambda value: _RISK_ORDER.get(value, 0),
        )
        rendered.append(
            {
                "section": primary.get("section", ""),
                "criterion_id": "+".join(record["criterion_id"] for record in source_records),
                "criterion": "\n".join(
                    dict.fromkeys(str(record.get("criterion") or "") for record in source_records)
                ),
                "status": "issues_found",
                "issues": [
                    {
                        "issue_id": f"final.{decision_index + 1}",
                        "risk_level": risk_level,
                        "issue_comment": decision.get("merged_comment", ""),
                        "anchors": selected_anchors,
                        "reasoning": "Finalized from: " + ", ".join(source_keys),
                        "criterion": primary.get("criterion", ""),
                        "check_point": primary.get("check_point", ""),
                    }
                ],
            }
        )

    for record in records:
        if record["issue_key"] in consumed:
            continue
        fallback_result = deepcopy(record["_result"])
        fallback_result["issues"] = [deepcopy(record["_issue"])]
        rendered.append(fallback_result)
    return rendered


def add_summary_issue_comments(final_review: dict) -> dict:
    """Derive complete summary-only text so the renderer cannot omit routed issues."""
    enriched = deepcopy(final_review)
    enriched["summary_issue_comments"] = [
        decision.get("merged_comment", "").strip()
        for decision in final_review.get("issue_decisions") or []
        if decision.get("output_scope") in {"summary_only", "both"}
        and decision.get("merged_comment", "").strip()
    ]
    return enriched


def normalize_issue_decisions(results: list[dict], final_review: dict) -> dict:
    """Apply deterministic presentation invariants after the LLM finalizer."""
    normalized = deepcopy(final_review)
    records = collect_issue_records(results)
    record_by_key = {record["issue_key"]: record for record in records}
    anchor_by_key = {
        anchor["anchor_key"]: original_anchor
        for record in records
        for anchor, original_anchor in zip(
            record["anchors"],
            record["_issue"].get("anchors") or [],
            strict=True,
        )
    }

    for decision in normalized.get("issue_decisions") or []:
        source_records = [
            record_by_key[key]
            for key in decision.get("source_issue_keys") or []
            if key in record_by_key
        ]
        selected = []
        seen_ranges: set[tuple[str, str, str]] = set()
        for annotation in decision.get("selected_anchors") or []:
            anchor = anchor_by_key.get(annotation.get("anchor_key", ""))
            if anchor is None:
                continue
            range_key = (
                str(anchor.get("xml_anchor_type") or ""),
                str(anchor.get("xml_anchor_id") or ""),
                str(anchor.get("quoted_text") or "").strip(),
            )
            if range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)
            selected.append(annotation)
        decision["selected_anchors"] = selected

        context = " ".join(
            [str(decision.get("merged_comment") or "")]
            + [
                " ".join(
                    str(record.get(field) or "")
                    for field in ("criterion", "check_point", "issue_comment")
                )
                for record in source_records
            ]
        )
        if len(selected) >= 3 and any(term in context for term in _GLOBAL_STRUCTURE_TERMS):
            decision["output_scope"] = "summary_only"
            decision["selected_anchors"] = []
            continue

        if len(source_records) == 1 and decision.get("output_scope") in {"inline", "both"}:
            fixed_text = _extract_fixed_instruction(source_records[0])
            if fixed_text and selected:
                decision["merged_comment"] = fixed_text
                selected[0]["comment_text"] = fixed_text
                decision["selected_anchors"] = selected[:1]

    return normalized


def _extract_fixed_instruction(record: dict[str, Any]) -> str | None:
    source = " ".join(
        str(record.get(field) or "")
        for field in ("criterion", "check_point")
    )
    candidates = [
        value.strip()
        for value in _QUOTED_TEXT_PATTERN.findall(source)
        if value.strip().startswith(("注意：", "补充"))
    ]
    return max(candidates, key=len) if candidates else None
