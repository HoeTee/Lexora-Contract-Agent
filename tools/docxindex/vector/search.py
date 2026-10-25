from __future__ import annotations

import math
from typing import Any
from loggers.call_log import call_stage, retrieval_call_stage

from .client import EmbeddingClient
from .schema import VectorIndex
from tools.docxindex.indexing import estimate_tokens


VECTOR_INTERNAL_CANDIDATE_LIMIT = 30


def vector_search(
    document_index: dict[str, Any],
    vector_index: VectorIndex,
    query: str,
    client: EmbeddingClient,
    top_k: int | None = None,
    score_threshold: float | None = None,
    input_tokens: int | None = None,
    allowed_node_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    with call_stage(retrieval_call_stage("vector-query")):
        query_embedding = client.embed([query])[0]
    nodes = {node.get("node_id"): node for node in document_index.get("nodes") or []}
    by_node = {}
    for item in vector_index.items:
        if allowed_node_ids is not None and item.node_id not in allowed_node_ids:
            continue
        score = cosine_similarity(query_embedding, item.embedding)
        previous = by_node.get(item.node_id)
        if previous is not None and previous["score"] >= score:
            continue
        node = nodes.get(item.node_id) or {}
        by_node[item.node_id] = {
                "score": score,
                "node_id": item.node_id,
                "title": node.get("title") or item.metadata.title,
                "summary": node.get("summary"),
                "start_index": node.get("start_index") or item.metadata.start_index,
                "end_index": node.get("end_index") or item.metadata.end_index,
                "start_anchor": node.get("start_anchor") or item.metadata.start_anchor,
                "end_anchor": node.get("end_anchor") or item.metadata.end_anchor,
                "node_type": node.get("node_type") or item.metadata.node_type,
                "token_estimate": node.get("token_estimate") or item.metadata.token_estimate,
                "matched_vector_id": item.vector_id,
            }
    scored = list(by_node.values())
    scored.sort(key=lambda item: item["score"], reverse=True)
    limit = top_k
    result = []
    used = 0
    for item in scored[:limit]:
        if score_threshold is not None and item["score"] < score_threshold:
            continue
        tokens = estimate_tokens(_candidate_text(item))
        if input_tokens is not None and result and used + tokens > input_tokens:
            break
        result.append(item)
        used += tokens
    return result


def _candidate_text(item: dict[str, Any]) -> str:
    return "\n".join(
        str(item.get(key) or "")
        for key in ("node_id", "title", "summary", "node_type", "start_index", "end_index", "token_estimate")
    )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
