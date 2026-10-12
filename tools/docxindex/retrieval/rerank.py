from __future__ import annotations

import json
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from pydantic import model_validator
from loggers.call_log import logical_call

from tools.docxindex.indexing import estimate_tokens
from tools.docxindex.llm import LLMClient
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash
from tools.docxindex.llm.schemas import RerankResponse

RERANK_PROMPT_VERSION = "docx_rerank_v3"


def rerank_matches(
    index: dict[str, Any],
    query: str,
    candidates: list[dict[str, Any]],
    client: LLMClient,
    input_tokens: int,
    cache_dir: Path | None,
    concurrency: int = 10,
    max_selected_nodes: int | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    keep = max_selected_nodes or len(candidates)
    remaining = candidates
    while True:
        views = _candidate_batches(index, query, remaining, input_tokens)
        if len(views) == 1:
            return _rank_batch(query, remaining, views[0], client, cache_dir, concurrency)
        if all(len(view) <= keep for view in views):
            raise ValueError("Rerank token budget cannot reduce the candidate pool; increase input_tokens or reduce max_selected_nodes")
        by_id = {item["node_id"]: item for item in remaining}
        survivors = []
        for view in views:
            batch = [by_id[item["node_id"]] for item in view]
            ranked = _rank_batch(query, batch, view, client, cache_dir, concurrency) if len(batch) > 1 else batch
            survivors.extend(ranked[:keep])
        remaining = survivors


def _rank_batch(query, candidates, candidate_view, client, cache_dir, concurrency):
    payload = json.dumps(candidate_view, ensure_ascii=False)
    cache = JsonlCache(cache_dir / "llm_rerank.jsonl" if cache_dir else None)
    key = cache_key(RERANK_PROMPT_VERSION, client.settings.model, query, text_hash(payload))
    cached = cache.get(key)
    allowed = {item["node_id"] for item in candidate_view}
    schema = _complete_rerank_schema(allowed)
    if cached is None:
        limiter = BoundedSemaphore(max(1, concurrency))
        with limiter, logical_call("docxindex.reranker", client.settings.model):
            response = client.complete_model(_rerank_prompt(query, payload), schema)
            cached = schema.model_validate(response.model_dump(mode="json")).model_dump(mode="json")
        cache.set(key, cached)
    else:
        cached = schema.model_validate(cached).model_dump(mode="json")
    by_id = {item.get("node_id"): item for item in candidates}
    ranked = []
    for item in sorted(cached["nodes"], key=lambda item: item["score"], reverse=True):
        node_id = item["node_id"]
        merged = dict(by_id[node_id])
        merged["rerank_score"] = float(item.get("score") or 0.0)
        merged["rerank_reason"] = str(item.get("reason") or "").strip()
        ranked.append(merged)

    return ranked


def _complete_rerank_schema(allowed: set[str]) -> type[RerankResponse]:
    class CompleteRerankResponse(RerankResponse):
        @model_validator(mode="after")
        def check_candidates(self):
            ids = [item.node_id for item in self.nodes]
            if len(ids) != len(allowed) or set(ids) != allowed:
                raise ValueError(f"Score each candidate exactly once; required node_ids: {sorted(allowed)}")
            return self
    return CompleteRerankResponse


def _candidate_batches(index: dict[str, Any], query: str, candidates: list[dict[str, Any]], input_tokens: int) -> list[list[dict[str, Any]]]:
    nodes = {node.get("node_id"): node for node in index.get("nodes") or []}
    result = []
    batches = []
    for candidate in candidates:
        node_id = candidate.get("node_id")
        node = nodes.get(node_id) or {}
        item = {
            "node_id": node_id,
            "sources": candidate.get("sources") or [candidate.get("source")],
            "title": node.get("title") or candidate.get("title"),
            "summary": node.get("summary") or candidate.get("summary"),
            "node_type": node.get("node_type") or candidate.get("node_type"),
            "token_estimate": node.get("token_estimate") or candidate.get("token_estimate"),
            "vector_score": candidate.get("vector_score") or candidate.get("score"),
        }
        proposed = json.dumps([*result, item], ensure_ascii=False)
        if result and estimate_tokens(_rerank_prompt(query, proposed)) > input_tokens:
            batches.append(result)
            result = []
        if estimate_tokens(_rerank_prompt(query, json.dumps([item], ensure_ascii=False))) > input_tokens:
            raise ValueError(f"Rerank candidate {node_id} exceeds input_tokens including the query and prompt")
        result.append(item)
    if result:
        batches.append(result)
    return batches


def _rerank_prompt(query: str, candidates_json: str) -> str:
    return f"""你正在对 DOCX 合同检索候选 node 做相关性重排序。

审查问题：
{query}

候选 node：
{candidates_json}

重排序规则：
- 只允许返回候选列表中真实存在的 node_id。
- 必须对每个候选 node 评分，返回全部候选，不能遗漏或重复；按 score 从高到低排列。
- 优先选择能够直接支撑审查问题判断的 node。
- 如果父 node 和更具体的子 node 都相关，优先选择更具体的子 node。
- 可以保留跨章节判断所需的多个 node。
- score 使用 0 到 1，1 表示最相关。

输出规范：
- 必须只返回一个合法 JSON object。
- 不要返回 Markdown。
- 不要返回解释。
- 不要添加 schema 以外的字段。
- JSON 必须严格符合：
{{
  "nodes": [
    {{"node_id": "body/sec_001", "score": 0.95, "reason": "选择原因"}}
  ]
}}"""
