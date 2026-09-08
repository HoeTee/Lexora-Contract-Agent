from __future__ import annotations

from typing import Any

from routing.schemas import RouteStep


def title_matches(index: dict[str, Any], step: RouteStep) -> list[dict[str, Any]]:
    if not step.all_titles and not step.terms:
        return []
    nodes = {node.get("node_id"): node for node in index.get("nodes") or []}
    result = []
    for item, depth in _walk(index.get("structure_tree") or []):
        node_id = item.get("node_id")
        title = str(item.get("title") or "").strip()
        if not node_id or not title or item.get("node_type") in {"body", "attachments"}:
            continue
        if not _is_catalog_title(item, depth):
            continue
        if not step.all_titles and not any(term in title for term in step.terms):
            continue
        node = nodes.get(node_id) or item
        candidate = _candidate(node, "title")
        if node.get("node_type") != "tail":
            candidate.update(
                {
                    "text_override": title,
                    "end_index": node.get("start_index"),
                    "end_anchor": node.get("start_anchor"),
                }
            )
        result.append(candidate)
    return result


def _walk(nodes: list[dict[str, Any]], depth: int = 0):
    for node in nodes:
        yield node, depth
        yield from _walk(node.get("nodes") or node.get("children") or [], depth + 1)


def _is_catalog_title(node: dict[str, Any], depth: int) -> bool:
    node_type = str(node.get("node_type") or "")
    return depth == 1 and node_type not in {"frontmatter", "body", "tail", "attachments"}


def _candidate(node: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "node_id": node.get("node_id"),
        "title": node.get("title"),
        "summary": node.get("summary"),
        "start_index": node.get("start_index"),
        "end_index": node.get("end_index"),
        "start_anchor": node.get("start_anchor"),
        "end_anchor": node.get("end_anchor"),
        "token_estimate": node.get("token_estimate"),
        "sources": [source],
    }
