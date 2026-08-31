from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any

from tools.docxindex.indexing import estimate_tokens
from tools.docxindex.llm import LLMClient
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash
from tools.docxindex.llm.schemas import QueryResponse

QUERY_PROMPT_VERSION = "docx_query_v3"
STRUCTURE_QUERY_BUDGET_TOKENS = 20000


def llm_query(
    index: dict[str, Any],
    queries: list[str],
    client: LLMClient,
    cache_dir: Path | None,
    input_tokens: int = STRUCTURE_QUERY_BUDGET_TOKENS,
    timer: Any | None = None,
    concurrency: int = 10,
    allowed_node_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    cache = JsonlCache(cache_dir / "llm_query.jsonl" if cache_dir else None)
    limiter = BoundedSemaphore(max(1, concurrency))
    structure = _compact_structure(index["structure_tree"], allowed_node_ids=allowed_node_ids)
    structure_parts = _split_structure(structure, input_tokens)
    if timer is not None:
        timer.note("ask.llm_structure_query.part_count", len(structure_parts))
    results = []
    for query in queries:
        seen = set()
        for part_index, structure_view in enumerate(structure_parts, 1):
            with _stage(timer, f"ask.llm_structure_query.part_{part_index}"):
                payload = json.dumps(structure_view, ensure_ascii=False)
                key = cache_key(QUERY_PROMPT_VERSION, client.settings.model, query, str(part_index), text_hash(payload))
                cached = cache.get(key)
                if cached is None:
                    with limiter:
                        response = client.complete_model(_query_prompt(query, payload), QueryResponse)
                    cached = response.model_dump(mode="json")
                    cache.set(key, cached)
            for item in cached.get("nodes") or []:
                node_id = str(item.get("node_id") or "").strip()
                node = _node_by_id(index, node_id)
                if not node or node_id in seen or (allowed_node_ids is not None and node_id not in allowed_node_ids):
                    continue
                seen.add(node_id)
                results.append(
                    {
                        "node_id": node_id,
                        "title": node.get("title"),
                        "summary": node.get("summary"),
                        "start_index": node.get("start_index"),
                        "end_index": node.get("end_index"),
                        "start_anchor": node.get("start_anchor"),
                        "end_anchor": node.get("end_anchor"),
                        "token_estimate": node.get("token_estimate"),
                        "reason": str(item.get("reason") or "").strip(),
                    }
                )
    return results


def _compact_structure(
    nodes: list[dict[str, Any]],
    allowed_node_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    compact = []
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        children = node.get("nodes") or node.get("children") or []
        compact_children = _compact_structure(children, allowed_node_ids=allowed_node_ids)
        if allowed_node_ids is not None and node_id not in allowed_node_ids and not compact_children:
            continue
        item = {
            "node_id": node_id,
            "title": node.get("title"),
            "node_type": node.get("node_type"),
            "summary": node.get("summary"),
            "token_estimate": node.get("token_estimate"),
        }
        if compact_children:
            item["nodes"] = compact_children
        compact.append(item)
    return compact


def _split_structure(structure: list[dict[str, Any]], input_tokens: int) -> list[list[dict[str, Any]]]:
    if estimate_tokens(json.dumps(structure, ensure_ascii=False)) <= input_tokens:
        return [structure]
    chunks: list[list[dict[str, Any]]] = []
    group: list[dict[str, Any]] = []
    group_tokens = 0
    for node in structure:
        node_tokens = estimate_tokens(json.dumps(node, ensure_ascii=False))
        if node_tokens > input_tokens:
            if group:
                chunks.append(group)
                group = []
                group_tokens = 0
            chunks.extend(_split_oversized_node(node, input_tokens))
            continue
        if group and group_tokens + node_tokens > input_tokens:
            chunks.append(group)
            group = []
            group_tokens = 0
        group.append(node)
        group_tokens += node_tokens
    if group:
        chunks.append(group)
    return chunks or [structure]


def _split_oversized_node(node: dict[str, Any], input_tokens: int) -> list[list[dict[str, Any]]]:
    children = node.get("nodes") or node.get("children") or []
    if not children:
        return [[node]]
    shell = {key: value for key, value in node.items() if key not in {"children", "nodes"}}
    child_budget = max(input_tokens - estimate_tokens(json.dumps(shell, ensure_ascii=False)), input_tokens // 2)
    result = []
    for child_chunk in _split_structure(children, child_budget):
        result.append([{**shell, "nodes": child_chunk}])
    return result


def _node_by_id(index: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    for node in index["nodes"]:
        if node.get("node_id") == node_id:
            return node
    return None


def _stage(timer: Any | None, name: str):
    if timer is None:
        return nullcontext()
    return timer.stage(name)


def _query_prompt(query: str, structure_json: str) -> str:
    return f"""你正在从 DOCX 合同的 PageIndex-like 结构树中选择与审查问题最相关的 node。
审查问题：{query}

结构树：
{structure_json}

选择规则：
- 只能返回结构树中真实存在的 node_id。
- 结构树字段含义：nodes 是子节点；start_index/end_index 是 DOCX w:body 子节点范围；summary/key_items 用于判断节点内容。
- 优先返回 title、summary 或 key_items 直接相关的 node。
- 如果父节点和子节点都相关，优先返回更具体的子节点；只有需要完整章节上下文时才返回父节点。
- 如果问题需要跨章节比对，可以返回多个 node。
- 不要返回 body 或 attachments 这类人工区域节点；应选择它们下面的具体业务节点。
- 避免返回弱相关 node。

输出规范：
- 必须只返回一个合法 JSON object。
- 不要返回 Markdown。
- 不要返回解释。
- 不要添加 schema 以外的字段。
- JSON 必须严格符合：
{{
  "nodes": [
    {{"node_id": "body/sec_001", "reason": "选择原因"}}
  ]
}}"""
