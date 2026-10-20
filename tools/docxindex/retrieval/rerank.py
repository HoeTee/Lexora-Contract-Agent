from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loggers.call_log import call_stage
from tools.docxindex.indexing import estimate_tokens
from tools.docxindex.reranker import RerankerClient
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash
from tools.docxindex.model_input import MODEL_INPUT_BATCH_SIZE, model_tokenizer, split_input


def rerank_matches(
    index: dict[str, Any],
    query: str,
    candidates: list[dict[str, Any]],
    client: RerankerClient,
    input_tokens: int | None = None,
    cache_dir: Path | None = None,
    max_selected_nodes: int | None = None,
) -> list[dict[str, Any]]:
    """Score all pieces and return original candidates ordered by their best score.

    input_tokens is an optional compatibility argument for library callers.
    Runtime clients use rerank.max_input_tokens, independently of retrieval budgets.
    The retriever applies max_selected_nodes after this node-level merge.
    """
    if not candidates:
        return []
    batches = _candidate_batches(index, query, candidates, input_tokens, client=client)
    best = {}
    for number, view in enumerate(batches, 1):
        ranked = _rank_batch(query, candidates, view, client, cache_dir, stage=f"rerank-batch{number}")
        for candidate in ranked:
            node_id = candidate["node_id"]
            if node_id not in best or candidate["rerank_score"] > best[node_id]["rerank_score"]:
                best[node_id] = candidate
    if best.keys() != {candidate["node_id"] for candidate in candidates}:
        raise ValueError("Reranker did not score every original candidate.")
    return sorted([best[candidate["node_id"]] for candidate in candidates],
                  key=lambda candidate: candidate["rerank_score"], reverse=True)


def _rank_batch(query, candidates, candidate_view, client, cache_dir, *, stage):
    documents = [item["_document"] for item in candidate_view]
    tokenizer = model_tokenizer(client.model)
    payload = json.dumps(documents, ensure_ascii=False)
    cache = JsonlCache(cache_dir / "dedicated_rerank_chunks_v2.jsonl" if cache_dir else None)
    key = cache_key("dedicated_rerank_chunks_v2", client.model, tokenizer.identity,
                    getattr(client, "instruct", ""), query, text_hash(payload))
    cached = cache.get(key)
    if cached is None:
        with call_stage(stage):
            scores = client.rank(query, documents)
        if {index for index, _ in scores} != set(range(len(documents))) or len(scores) != len(documents):
            raise ValueError("Reranker must score every input piece before node aggregation.")
        cached = [{"node_id": candidate_view[index]["node_id"], "score": score} for index, score in scores]
        cache.set(key, cached)
    by_id = {item["node_id"]: item for item in candidates}
    return [{**by_id[item["node_id"]], "rerank_score": item["score"], "rerank_reason": ""}
            for item in cached]


def _candidate_batches(index: dict[str, Any], query: str, candidates: list[dict[str, Any]],
                       input_tokens: int | None = None, *, client=None) -> list[list[dict[str, Any]]]:
    nodes = {node.get("node_id"): node for node in index.get("nodes") or []}
    tokenizer = model_tokenizer(getattr(client, "model", "legacy-rerank"))
    limit = tokenizer.effective_limit(getattr(client, "max_input_tokens", input_tokens or 20000))
    provider = getattr(client, "provider", None)
    instruct = "" if provider == "bge" or tokenizer.slug == "bge-reranker-v2-m3" else getattr(client, "instruct", "")
    pieces = []
    for candidate in candidates:
        node_id = candidate.get("node_id")
        node = nodes.get(node_id) or {}
        text = (candidate["text_override"] if candidate.get("text_override") is not None
                else node.get("text") or candidate.get("text") or "")
        item = {
            "node_id": node_id, "text": text,
            "sources": candidate.get("sources") or [candidate.get("source")],
            "title": node.get("title") or candidate.get("title"),
            "summary": node.get("summary") or candidate.get("summary"),
            "node_type": node.get("node_type") or candidate.get("node_type"),
            "token_estimate": node.get("token_estimate") or candidate.get("token_estimate"),
            "vector_score": candidate.get("vector_score") or candidate.get("score"),
        }
        document = json.dumps(item, ensure_ascii=False)
        chunks = split_input(document, lambda value: tokenizer.count_rerank(query, value, instruct) <= limit,
                             tokenizer=tokenizer)  # No reranker overlap.
        pieces.extend({**item, "_document": chunk.text} for chunk in chunks)
    return [pieces[start:start + MODEL_INPUT_BATCH_SIZE] for start in range(0, len(pieces), MODEL_INPUT_BATCH_SIZE)]


def _rerank_tokens(query: str, candidates_json: str) -> int:
    """Legacy diagnostic estimate; runtime enforcement uses the model tokenizer."""
    return estimate_tokens(query) + estimate_tokens(candidates_json)
