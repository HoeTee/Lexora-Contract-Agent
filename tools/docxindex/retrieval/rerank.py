from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from loggers.call_log import call_stage

from tools.docxindex.indexing import estimate_tokens
from tools.docxindex.reranker import RerankerClient
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash



def rerank_matches(
    index: dict[str, Any],
    query: str,
    candidates: list[dict[str, Any]],
    client: RerankerClient,
    input_tokens: int,
    cache_dir: Path | None,
    max_selected_nodes: int | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    keep = max_selected_nodes or len(candidates)
    remaining = candidates
    pass_number = 1
    while True:
        views = _candidate_batches(index, query, remaining, input_tokens)
        if len(views) == 1:
            return _rank_batch(query, remaining, views[0], client, cache_dir,
                               stage=f"rerank-pass{pass_number}-batch1")
        if all(len(view) <= keep for view in views):
            raise ValueError("Rerank token budget cannot reduce the candidate pool; increase input_tokens or reduce max_selected_nodes")
        by_id = {item["node_id"]: item for item in remaining}
        survivors = []
        for batch_number, view in enumerate(views, 1):
            batch = [by_id[item["node_id"]] for item in view]
            ranked = _rank_batch(query, batch, view, client, cache_dir,
                                 stage=f"rerank-pass{pass_number}-batch{batch_number}") if len(batch) > 1 else batch
            survivors.extend(ranked[:keep])
        remaining = survivors
        pass_number += 1


def _rank_batch(query, candidates, candidate_view, client, cache_dir, *, stage):
    payload = json.dumps(candidate_view, ensure_ascii=False)
    cache = JsonlCache(cache_dir / "dedicated_rerank_v1.jsonl" if cache_dir else None)
    key = cache_key("dedicated_rerank_v1", client.model, query, text_hash(payload))
    cached = cache.get(key)
    if cached is None:
        with call_stage(stage):
            scores = client.rank(query, [json.dumps(item, ensure_ascii=False) for item in candidate_view])
        cached = [{"node_id": candidate_view[index]["node_id"], "score": score} for index, score in scores]
        cache.set(key, cached)
    by_id = {item["node_id"]: item for item in candidates}
    return [{**by_id[item["node_id"]], "rerank_score": item["score"], "rerank_reason": ""}
            for item in cached]


def _candidate_batches(index: dict[str, Any], query: str, candidates: list[dict[str, Any]], input_tokens: int) -> list[list[dict[str, Any]]]:
    nodes = {node.get("node_id"): node for node in index.get("nodes") or []}
    result = []
    batches = []
    for candidate in candidates:
        node_id = candidate.get("node_id")
        node = nodes.get(node_id) or {}
        item = {
            "node_id": node_id,
            "text": candidate.get("text_override") or node.get("text") or candidate.get("text") or "",
            "sources": candidate.get("sources") or [candidate.get("source")],
            "title": node.get("title") or candidate.get("title"),
            "summary": node.get("summary") or candidate.get("summary"),
            "node_type": node.get("node_type") or candidate.get("node_type"),
            "token_estimate": node.get("token_estimate") or candidate.get("token_estimate"),
            "vector_score": candidate.get("vector_score") or candidate.get("score"),
        }
        proposed = json.dumps([*result, item], ensure_ascii=False)
        if result and _rerank_tokens(query, proposed) > input_tokens:
            batches.append(result)
            result = []
        if _rerank_tokens(query, json.dumps([item], ensure_ascii=False)) > input_tokens:
            raise ValueError(f"Rerank candidate {node_id} exceeds input_tokens including the query and prompt")
        result.append(item)
    if result:
        batches.append(result)
    return batches



def _rerank_tokens(query: str, candidates_json: str) -> int:
    return estimate_tokens(query) + estimate_tokens(candidates_json)
