from __future__ import annotations

from typing import Any

from tools.docxindex.indexing import estimate_tokens


def get_node(index: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in index["nodes"]:
        if node["node_id"] == node_id:
            return node
    raise KeyError(f"node_id not found: {node_id}")


def content_view(index: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = get_node(index, node_id)
    return {
        key: node.get(key)
        for key in (
            "node_id",
            "title",
            "node_type",
            "text",
            "start_index",
            "end_index",
            "start_anchor",
            "end_anchor",
            "token_estimate",
            "table_id",
            "mapping_ref",
            "row_start",
            "row_end",
        )
    }


def build_content_context(
    index: dict[str, Any],
    matches: list[dict[str, Any]],
    input_tokens: int,
    part: int = 1,
) -> dict[str, Any]:
    units = _content_units(index, _prefer_specific_nodes(index, matches), input_tokens)
    pages = _paginate_units(units, input_tokens)
    page_index = max(0, part - 1)
    selected = pages[page_index] if page_index < len(pages) else []
    used = sum(estimate_tokens(item.get("text") or "") for item in selected)
    has_more = page_index + 1 < len(pages)
    return {
        "content_context": selected,
        "pagination": {
            "part": part,
            "has_more": has_more,
            "next_part": part + 1 if has_more else None,
        },
        "budget": {
            "input_tokens": input_tokens,
            "used_tokens": used,
        },
    }


def _content_units(index: dict[str, Any], candidates: list[dict[str, Any]], input_tokens: int) -> list[dict[str, Any]]:
    units = []
    for candidate in candidates:
        node_id = candidate.get("node_id")
        if not node_id:
            continue
        node = get_node(index, candidate.get("parent_node_id") or node_id)
        child_ids = node.get("nodes") or node.get("children") or []
        if node.get("node_type") == "table" and not (node.get("text") or "").strip() and child_ids:
            units.extend(_table_child_units(index, node, candidate, input_tokens))
            continue
        item = _content_item(node, candidate)
        if node.get("node_type") in {"table", "table_chunk"}:
            units.append(item)
            continue
        item_tokens = estimate_tokens(item.get("text") or "")
        if input_tokens is not None and item_tokens > input_tokens:
            units.extend(_chunk_oversized_node(index, node, candidate, input_tokens))
        else:
            units.append(item)
    return units


def _table_child_units(
    index: dict[str, Any],
    table_node: dict[str, Any],
    candidate: dict[str, Any],
    input_tokens: int,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for child_id in table_node.get("nodes") or table_node.get("children") or []:
        child = get_node(index, child_id)
        child_candidate = {
            **candidate,
            "node_id": child_id,
            "parent_node_id": None,
            "start_index": child.get("start_index"),
            "end_index": child.get("end_index"),
            "start_anchor": child.get("start_anchor"),
            "end_anchor": child.get("end_anchor"),
        }
        item = _content_item(child, child_candidate)
        units.append(item)
    return units


def _paginate_units(units: list[dict[str, Any]], input_tokens: int) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for unit in units:
        tokens = estimate_tokens(unit.get("text") or "")
        if current and input_tokens is not None and used + tokens > input_tokens:
            pages.append(current)
            current = []
            used = 0
        current.append(unit)
        used += tokens
    if current:
        pages.append(current)
    return pages or [[]]


def _content_item(node: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": candidate.get("node_id") or node.get("node_id"),
        "title": node.get("title"),
        "text": candidate.get("text_override") if candidate.get("text_override") is not None else node.get("text") or "",
        "start_index": candidate.get("start_index", node.get("start_index")),
        "end_index": candidate.get("end_index", node.get("end_index")),
        "start_anchor": candidate.get("start_anchor", node.get("start_anchor")),
        "end_anchor": candidate.get("end_anchor", node.get("end_anchor")),
        "source": candidate.get("sources") or [candidate.get("source")],
        "selection_reason": candidate.get("reason"),
        "truncated": False,
        "table_id": node.get("table_id"),
        "mapping_ref": node.get("mapping_ref"),
        "row_start": node.get("row_start"),
        "row_end": node.get("row_end"),
    }


def _prefer_specific_nodes(index: dict[str, Any], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [match.get("node_id") for match in matches if match.get("node_id")]
    id_set = set(ids)
    filtered = []
    for match in matches:
        node_id = match.get("node_id")
        if not node_id:
            continue
        sources = set(match.get("sources") or [])
        has_selected_descendant = not sources.intersection({"title", "region"}) and any(
            other != node_id and str(other).startswith(f"{node_id}/") for other in id_set
        )
        if has_selected_descendant:
            continue
        filtered.append(match)
    return filtered


def _chunk_oversized_node(index: dict[str, Any], node: dict[str, Any], candidate: dict[str, Any], input_tokens: int) -> list[dict[str, Any]]:
    anchors = _anchors_for_node(index, node)
    if not anchors:
        return _chunk_text_without_anchors(node, candidate, input_tokens)
    chunks = []
    parts: list[str] = []
    used = 0
    chunk_start_anchor = anchors[0].get("anchor")
    chunk_start_index = anchors[0].get("body_child_index")
    chunk_end_anchor = None
    chunk_end_index = None
    chunk_index = 1
    for anchor in anchors:
        text = anchor.get("text") or ""
        tokens = estimate_tokens(text)
        if parts and used + tokens > input_tokens:
            chunks.append(
                _chunk_item(
                    node,
                    candidate,
                    chunk_index,
                    parts,
                    chunk_start_anchor,
                    chunk_end_anchor,
                    chunk_start_index,
                    chunk_end_index,
                )
            )
            chunk_index += 1
            parts = []
            used = 0
            chunk_start_anchor = anchor.get("anchor")
            chunk_start_index = anchor.get("body_child_index")
        parts.append(text)
        used += tokens
        chunk_end_anchor = anchor.get("anchor")
        chunk_end_index = anchor.get("body_child_index")
    if parts:
        chunks.append(
            _chunk_item(
                node,
                candidate,
                chunk_index,
                parts,
                chunk_start_anchor,
                chunk_end_anchor,
                chunk_start_index,
                chunk_end_index,
            )
        )
    return chunks


def _chunk_item(
    node: dict[str, Any],
    candidate: dict[str, Any],
    chunk_index: int,
    parts: list[str],
    start_anchor: str | None,
    end_anchor: str | None,
    start_index: int | None,
    end_index: int | None,
) -> dict[str, Any]:
    return {
        "node_id": f"{node.get('node_id')}/chunk_{chunk_index:03d}",
        "title": node.get("title"),
        "text": "\n".join(part for part in parts if part),
        "start_index": start_index,
        "end_index": end_index,
        "start_anchor": start_anchor,
        "end_anchor": end_anchor,
        "source": candidate.get("sources") or [candidate.get("source")],
        "truncated": True,
        "parent_node_id": node.get("node_id"),
    }


def _chunk_text_without_anchors(node: dict[str, Any], candidate: dict[str, Any], input_tokens: int) -> list[dict[str, Any]]:
    text = node.get("text") or ""
    if not text:
        return [_content_item(node, candidate)]
    chunks = []
    chunk_index = 1
    start = 0
    # Character slicing is a last-resort fallback when anchor text is unavailable.
    approx_chars = max(1, input_tokens * 2)
    while start < len(text):
        part = text[start : start + approx_chars]
        chunks.append(
            {
                **_content_item(node, candidate),
                "node_id": f"{node.get('node_id')}/chunk_{chunk_index:03d}",
                "text": part,
                "truncated": True,
                "parent_node_id": node.get("node_id"),
            }
        )
        start += approx_chars
        chunk_index += 1
    return chunks


def _anchors_for_node(index: dict[str, Any], node: dict[str, Any]) -> list[dict[str, Any]]:
    start = node.get("start_anchor")
    end = node.get("end_anchor")
    if not start or not end:
        return []
    start_index = node.get("start_index")
    end_index = node.get("end_index")
    anchors = []
    for anchor, record in (index.get("anchor_map") or {}).items():
        body_index = record.get("body_child_index")
        in_range = (
            start_index is not None
            and end_index is not None
            and body_index is not None
            and start_index <= body_index <= end_index
        )
        if in_range:
            anchors.append({"anchor": anchor, **record})
    anchors.sort(key=lambda item: item.get("body_child_index") or 0)
    return anchors
