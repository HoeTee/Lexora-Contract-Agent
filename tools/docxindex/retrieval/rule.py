from __future__ import annotations

import re
from typing import Any

from tools.docxindex.llm.schemas import RouteStep

from .title import _candidate


def rule_matches(index: dict[str, Any], step: RouteStep, query: str = "") -> list[dict[str, Any]]:
    terms = _literal_terms(query, step.terms)
    by_id = {node.get("node_id"): node for node in index.get("nodes") or []}
    scored: list[tuple[int, int, dict[str, Any]]] = []
    anchors = sorted(
        (index.get("anchor_map") or {}).items(),
        key=lambda item: item[1].get("body_child_index") or 0,
    )
    for anchor_id, record in anchors:
        text = str(record.get("text") or "")
        matched_term_count = sum(term in text for term in terms)
        if not terms or not matched_term_count:
            continue
        owner_id = record.get("node_id")
        owner = by_id.get(owner_id)
        if not owner:
            continue
        candidate = _candidate(owner, "rule")
        candidate.update(
            {
                "node_id": f"{owner_id}/match_{anchor_id}",
                "parent_node_id": owner_id,
                "text_override": text,
                "start_index": record.get("body_child_index"),
                "end_index": record.get("body_child_index"),
                "start_anchor": anchor_id,
                "end_anchor": anchor_id,
            }
        )
        candidate["rule_match_count"] = matched_term_count
        scored.append((matched_term_count, int(record.get("body_child_index") or 0), candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [candidate for _, _, candidate in scored]


def _literal_terms(query: str, planned: list[str]) -> list[str]:
    terms = {term.strip() for term in planned if 2 <= len(term.strip()) <= 30}
    terms.update(term.strip() for term in re.findall(r"[“\"]([^”\"]{2,30})[”\"]", query))
    if "：" in query or ":" in query:
        tail = re.split(r"[：:]", query, maxsplit=1)[1]
        for term in re.split(r"[、，,；;。]", tail):
            term = term.strip(" \t\r\n。；;：:")
            if 2 <= len(term) <= 20 and not any(mark in term for mark in ("是否", "如果", "需要", "检查")):
                terms.add(term)
    role_roots = {
        term[:-2]
        for term in terms
        if term.endswith("单位") and len(term) >= 4
    }
    role_roots.update(term[:-1] for term in terms if term.endswith("人") and len(term) >= 3)
    terms.update(root for root in role_roots if len(root) >= 2)
    return sorted(terms, key=lambda item: (-len(item), item))


def _scan_nodes(index: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for node in index.get("nodes") or []:
        children = node.get("nodes") or node.get("children") or []
        if not children and node.get("node_type") not in {"body", "attachments"}:
            result.append(node)
    return sorted(result, key=lambda item: (item.get("start_index") is None, item.get("start_index") or 0))


def section_root_ids(index: dict[str, Any], step: RouteStep) -> list[str]:
    """Return heading node IDs selected by a region step's anchor terms."""
    terms = [term.strip() for term in step.anchor_terms if term.strip()]
    if not terms:
        return []
    nodes = index.get("nodes") or []
    root_ids: list[str] = []
    for node in nodes:
        title = str(node.get("title") or "").strip()
        node_type = str(node.get("node_type") or "")
        node_id = str(node.get("node_id") or "")
        if not title or not node_id or node_type in {"frontmatter", "body", "tail", "attachments"}:
            continue
        if any(term in title for term in terms):
            root_ids.append(node_id)
    return root_ids
