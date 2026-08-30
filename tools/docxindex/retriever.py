from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from tools.docxindex import build_document_index
from tools.docxindex.indexing.config import IndexingConfig
from tools.docxindex.llm import LLMClient, LLMSettings
from tools.docxindex.retrieval import (
    RetrievalConfig,
    RouteConfig,
    build_content_context,
    join_matches,
    llm_query,
    plan_route,
    region_matches,
    rerank_matches,
    rule_matches,
    scan_matches,
    title_matches,
)
from tools.docxindex.vector import (
    EmbeddingClient,
    EmbeddingSettings,
    build_vector_index,
    vector_search,
)


class DocxIndexRetriever:
    """In-process adapter for the docxindex build and retrieval pipeline."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        llm_api_key: str | None = None,
        embedding_model: str | None = None,
        embedding_base_url: str | None = None,
        embedding_api_key: str | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.retrieval_config = RetrievalConfig.from_sources(self.config_path)
        self.indexing_config = IndexingConfig.from_sources(self.config_path)
        self.route_config = RouteConfig.load(self.config_path)
        self.llm_settings = LLMSettings.from_sources(
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            config_path=self.config_path,
        )
        self.embedding_settings = EmbeddingSettings.from_sources(
            model=embedding_model,
            base_url=embedding_base_url,
            api_key=embedding_api_key,
            config_path=self.config_path,
        )
        self.llm_client = LLMClient(
            self.llm_settings,
            max_concurrency=self.retrieval_config.concurrency.llm,
        )
        self.embedding_client = EmbeddingClient(
            self.embedding_settings,
            max_concurrency=self.retrieval_config.concurrency.embedding,
        )
        self._index: dict[str, Any] | None = None
        self._vector_index: Any | None = None

    async def build_index(self, docx_path: str) -> str:
        """Build one temporary in-memory index from a DOCX path."""
        started_at = time.monotonic()
        _terminal(f"[DocxIndex] Building temporary index: {Path(docx_path).name}")
        try:
            result = await asyncio.to_thread(self._build_index_sync, Path(docx_path))
            result["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
            _terminal(
                "[DocxIndex] Index ready: "
                f"nodes={result['num_nodes']}, anchors={result['num_anchors']}, "
                f"vectors={result['vector_items']}, elapsed={result['elapsed_seconds']}s"
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            _terminal(f"[DocxIndex] Index build failed: {type(exc).__name__}: {exc}")
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    async def search(self, query: str) -> str:
        """Retrieve review-ready, DOCX-anchor-aware text for one query."""
        started_at = time.monotonic()
        _terminal(f"[DocxIndex] Searching temporary index: query_chars={len(query)}")
        try:
            result = await asyncio.to_thread(self._search_sync, query)
            _terminal(
                "[DocxIndex] Search complete: "
                f"result_chars={len(result)}, elapsed={round(time.monotonic() - started_at, 2)}s"
            )
            return result
        except Exception as exc:
            _terminal(f"[DocxIndex] Search failed: {type(exc).__name__}: {exc}")
            return f"Error in contract DocxIndex search: {exc}"

    def _build_index_sync(self, docx_path: Path) -> dict[str, Any]:
        if docx_path.suffix.lower() != ".docx":
            raise ValueError("Only .docx contract files are supported for review indexing.")
        if not docx_path.is_file():
            raise FileNotFoundError(f"Contract file does not exist: {docx_path}")

        indexing = self.indexing_config
        retrieval = self.retrieval_config
        document_index = build_document_index(
            docx_path,
            llm_expand=True,
            llm_summary=True,
            llm_settings=self.llm_settings,
            cache_dir=None,
            llm_concurrency=retrieval.concurrency.llm,
            paragraph_split_threshold_tokens=indexing.paragraph.split_threshold_tokens,
            paragraph_chunk_target_tokens=indexing.paragraph.chunk_target_tokens,
            table_split_threshold_tokens=indexing.table.split_threshold_tokens,
            table_chunk_target_tokens=indexing.table.chunk_target_tokens,
            heading_profiles=indexing.heading.compile_profiles(),
            llm_attachment_hierarchy=True,
            hierarchy_input_max_tokens=indexing.hierarchy.input_max_tokens,
            hierarchy_batch_target_tokens=indexing.hierarchy.batch_target_tokens,
            hierarchy_batch_overlap_tokens=indexing.hierarchy.batch_overlap_tokens,
            hierarchy_max_levels=indexing.hierarchy.max_levels,
            hierarchy_retry_count=indexing.hierarchy.retry_count,
            attachment_max_levels=indexing.attachment.max_levels,
            summary_batch_max_nodes=indexing.summary.batch_max_nodes,
            summary_batch_max_tokens=indexing.summary.batch_max_tokens,
        ).to_json_dict()

        vector_index = None
        if retrieval.vector.enabled:
            vector_index = build_vector_index(
                document_index,
                self.embedding_client,
                source_index=docx_path.name,
                concurrency=retrieval.concurrency.embedding,
            )

        self._index = document_index
        self._vector_index = vector_index
        return {
            "status": "built_temporary_index",
            "backend": "docxindex",
            "num_nodes": len(document_index.get("nodes") or []),
            "num_anchors": len(document_index.get("anchor_map") or {}),
            "vector_items": len(vector_index.items) if vector_index is not None else 0,
        }

    def _search_sync(self, query: str) -> str:
        if self._index is None:
            raise RuntimeError("docxindex_build_index must be called before docxindex_search")

        index = self._index
        config = self.retrieval_config
        route = self.route_config
        route_plan = plan_route(
            index,
            query,
            self.llm_client,
            route,
            cache_dir=None,
            criterion_text=query,
        )
        routed_matches = _execute_route(index, query, route_plan, config.scan_batch_tokens)
        fallback_used = _should_use_fallback(
            route_plan,
            routed_matches,
            fallback_enabled=route.fallback_enabled,
        )

        structure_matches: list[dict[str, Any]] = []
        vector_matches: list[dict[str, Any]] = []
        if fallback_used:
            structure_matches = llm_query(
                index,
                [query],
                self.llm_client,
                cache_dir=None,
                input_tokens=config.input_tokens,
                concurrency=config.concurrency.llm,
            )[: route.llm_candidates]

        if fallback_used and config.vector.enabled and self._vector_index is not None:
            vector_matches = vector_search(
                index,
                self._vector_index,
                query,
                self.embedding_client,
                score_threshold=route.vector_threshold,
                input_tokens=config.input_tokens,
                concurrency=config.concurrency.embedding,
            )[: route.vector_candidates]

        fallback_matches = _merge_matches(structure_matches, vector_matches)
        candidates = _merge_routed_matches(routed_matches, fallback_matches)
        if fallback_used and config.rerank.enabled:
            ranked_matches = rerank_matches(
                index,
                query,
                candidates,
                self.llm_client,
                config.input_tokens,
                cache_dir=None,
                concurrency=config.concurrency.reranker,
            )[: route.max_nodes]
        else:
            ranked_matches = candidates

        content_budget = (
            config.scan_batch_tokens
            if any(step.method == "scan" for step in route_plan.steps)
            else config.output_tokens
        )
        context = build_content_context(index, ranked_matches, content_budget)
        return _format_anchor_context(index, context["content_context"])


def _execute_route(index: dict[str, Any], query: str, plan: Any, scan_batch_tokens: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for step in plan.steps:
        if step.method == "title":
            current = title_matches(index, step)
        elif step.method == "region":
            current = region_matches(index, step, matches)
        elif step.method == "rule":
            current = rule_matches(index, step, query)
        elif step.method == "join":
            current = join_matches(index, query, step)
        elif step.method == "scan":
            current = scan_matches(index, scan_batch_tokens)
        elif step.method == "hybrid":
            current = []
        else:
            raise ValueError(f"Unsupported docxindex route method: {step.method}")
        matches = _merge_routed_matches(matches, current)
    return matches


def _route_is_complete(plan: Any) -> bool:
    return any(step.method == "scan" or (step.method == "title" and step.all_titles) for step in plan.steps)


def _should_use_fallback(
    plan: Any,
    routed_matches: list[dict[str, Any]],
    *,
    fallback_enabled: bool,
) -> bool:
    """Supplement every incomplete deterministic route when fallback is enabled."""
    hybrid_requested = any(step.method == "hybrid" for step in plan.steps)
    return (
        hybrid_requested
        or not routed_matches
        or (fallback_enabled and not _route_is_complete(plan))
    )


def _merge_matches(structure_matches: list[dict[str, Any]], vector_matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source, matches in (("structure", structure_matches), ("vector", vector_matches)):
        for match in matches:
            node_id = match.get("node_id")
            if not node_id:
                continue
            item = merged.setdefault(node_id, dict(match))
            sources = set(item.get("sources") or [])
            sources.add(source)
            item["sources"] = sorted(sources)
            if source == "vector":
                item["vector_score"] = match.get("score")
            if source == "structure":
                item["structure_reason"] = match.get("reason")
    return list(merged.values())


def _merge_routed_matches(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for matches in groups:
        for match in matches:
            node_id = match.get("node_id")
            if not node_id:
                continue
            if node_id not in merged:
                merged[node_id] = dict(match)
                order.append(node_id)
            item = merged[node_id]
            sources = set(item.get("sources") or [])
            sources.update(match.get("sources") or [])
            item["sources"] = sorted(source for source in sources if source)
            if "region" in (match.get("sources") or []):
                item.pop("text_override", None)
            for key in ("vector_score", "structure_reason", "reason", "rerank_score", "rerank_reason"):
                if match.get(key) is not None:
                    item[key] = match[key]
    return [merged[node_id] for node_id in order]


def _format_anchor_context(index: dict[str, Any], nodes: list[dict[str, Any]]) -> str:
    anchors = sorted(
        (index.get("anchor_map") or {}).items(),
        key=lambda item: item[1].get("body_child_index") or 0,
    )
    parts: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        start_index = node.get("start_index")
        end_index = node.get("end_index")
        for _, record in anchors:
            body_index = record.get("body_child_index")
            if body_index is None or start_index is None or end_index is None:
                continue
            if not (start_index <= body_index <= end_index):
                continue
            anchor_type = "table" if record.get("type") == "tbl" else "paragraph"
            anchor_id = f"path:body/{'tbl' if anchor_type == 'table' else 'p'}{body_index}"
            if anchor_id in seen:
                continue
            text = str(record.get("raw_text") or record.get("text") or "").strip()
            if not text:
                continue
            seen.add(anchor_id)
            header = f"## 合同检索结果 {len(parts) + 1}"
            title = str(node.get("title") or "").strip()
            if title:
                header += f"\n检索片段标题：{title}"
            header += f"\nxml_anchor_type: {anchor_type}\nxml_anchor_id: {anchor_id}"
            parts.append(f"{header}\n内容：\n{text}")
    return "\n\n---\n\n".join(parts) if parts else "未找到相关合同内容。"


def _terminal(message: str) -> None:
    """Emit concise progress without writing a tool-owned log file."""
    print(message, file=sys.stderr, flush=True)
