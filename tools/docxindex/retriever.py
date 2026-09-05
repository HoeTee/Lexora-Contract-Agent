from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from loggers.model_event_context import append_model_event
from tools.docxindex import build_document_index
from tools.docxindex.indexing.config import IndexingConfig
from tools.docxindex.llm import LLMClient, LLMSettings
from tools.docxindex.retrieval import (
    RetrievalConfig,
    RouteConfig,
    build_content_context,
    llm_query,
    plan_route,
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
            return json.dumps(
                {
                    "error": {
                        "http_status": getattr(exc, "http_status", getattr(exc, "status_code", None)),
                        "error_type": getattr(exc, "error_type", type(exc).__name__),
                        "detail": str(exc),
                    }
                },
                ensure_ascii=False,
            )

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
            http_status = getattr(exc, "http_status", getattr(exc, "status_code", None))
            error_type = getattr(exc, "error_type", type(exc).__name__)
            return (
                "Error in contract DocxIndex search: "
                f"http_status={http_status if http_status is not None else 'null'}; "
                f"error_type={error_type}; detail={exc}"
            )

    def _build_index_sync(self, docx_path: Path) -> dict[str, Any]:
        if docx_path.suffix.lower() != ".docx":
            raise ValueError("Only .docx contract files are supported for review indexing.")
        if not docx_path.is_file():
            raise FileNotFoundError(f"Contract file does not exist: {docx_path}")

        indexing = self.indexing_config
        retrieval = self.retrieval_config
        document_index = build_document_index(
            docx_path,
            llm_expand=indexing.enrichment.llm_expand,
            llm_summary=indexing.enrichment.llm_summary,
            llm_settings=self.llm_settings,
            cache_dir=None,
            llm_concurrency=retrieval.concurrency.llm,
            paragraph_split_threshold_tokens=indexing.paragraph.split_threshold_tokens,
            paragraph_chunk_target_tokens=indexing.paragraph.chunk_target_tokens,
            table_split_threshold_tokens=indexing.table.split_threshold_tokens,
            table_chunk_target_tokens=indexing.table.chunk_target_tokens,
            heading_profiles=indexing.heading.compile_profiles(),
            llm_attachment_hierarchy=indexing.enrichment.llm_attachment_hierarchy,
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
            "region_boundaries": document_index.get("region_boundaries") or {},
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
        _route_event("plan", plan=route_plan.model_dump(mode="json"))
        step_groups: list[list[dict[str, Any]]] = []
        complete_title_matches: list[dict[str, Any]] = []
        deterministic_notices: list[str] = []
        for step_index, step in enumerate(route_plan.steps, 1):
            if step.method == "title":
                current = title_matches(index, step)
                if step.all_titles:
                    complete_title_matches.extend(current)
            elif step.method == "rule":
                current = rule_matches(index, step, query)
                if not current:
                    terms = "、".join(step.terms)
                    deterministic_notices.append(
                        "确定性全文精确检索（已覆盖全部合同原文）："
                        f"以下检索词的出现次数均为0：{terms}。"
                        "若当前审查标准以词语缺失为触发条件，应直接据此判断。"
                    )
            elif step.method == "region":
                allowed_node_ids = _region_node_ids(index, step.regions)
                scoped_query = _step_query(query, step.terms)
                current = self._semantic_matches(
                    index,
                    scoped_query,
                    allowed_node_ids,
                    scope=step.regions[0],
                )
            elif step.method == "scan":
                current = scan_matches(
                    index,
                    query,
                    self.llm_client,
                    config.scan_batch_tokens,
                    config.concurrency.llm,
                )
            elif step.method == "hybrid":
                current = self._semantic_matches(
                    index,
                    _step_query(query, step.terms),
                    None,
                    scope="global",
                )
            else:
                raise ValueError(f"Unsupported docxindex route method: {step.method}")
            current = _add_route_source(current, step.method)
            step_groups.append(current)
            _route_event(
                "step",
                step=step_index,
                method=step.method,
                regions=step.regions,
                match_count=len(current),
                node_ids=[item.get("node_id") for item in current],
            )

        fallback_used = _should_use_fallback(
            route_plan,
            fallback_enabled=route.fallback_enabled,
        )
        fallback_matches = (
            self._semantic_matches(index, query, None, scope="global")
            if fallback_used
            else []
        )
        if fallback_matches:
            step_groups.append(_add_route_source(fallback_matches, "fallback"))
        _route_event(
            "fallback",
            requested=route_plan.fallback,
            used=fallback_used,
            match_count=len(fallback_matches),
        )

        has_scan = any(step.method == "scan" for step in route_plan.steps)
        selected_node_limit = config.semantic.max_selected_nodes
        limit = None if has_scan else max(selected_node_limit, sum(bool(group) for group in step_groups))
        ranked_matches = _interleave_groups(step_groups, limit=limit)
        if complete_title_matches:
            ranked_matches = _deduplicate_matches(complete_title_matches + ranked_matches)

        content_budget = (
            config.scan_batch_tokens
            if has_scan
            else config.output_tokens
        )
        context = build_content_context(index, ranked_matches, content_budget)
        content_nodes = list(context["content_context"])
        content_parts = 1
        if has_scan:
            next_part = context["pagination"]["next_part"]
            while next_part is not None:
                page = build_content_context(index, ranked_matches, content_budget, part=next_part)
                content_nodes.extend(page["content_context"])
                content_parts += 1
                next_part = page["pagination"]["next_part"]
        source_counts: dict[str, int] = {}
        for match in ranked_matches:
            for source in match.get("sources") or []:
                source_counts[source] = source_counts.get(source, 0) + 1
        _route_event(
            "result",
            candidate_count=len(ranked_matches),
            source_counts=source_counts,
            content_parts=content_parts,
            has_more=False if has_scan else context["pagination"]["has_more"],
        )
        content = _format_anchor_context(index, content_nodes)
        if deterministic_notices:
            content = "\n".join(deterministic_notices) + "\n\n" + content
        return content

    def _semantic_matches(
        self,
        index: dict[str, Any],
        query: str,
        allowed_node_ids: set[str] | None,
        *,
        scope: str,
    ) -> list[dict[str, Any]]:
        config = self.retrieval_config
        structure_matches = llm_query(
            index,
            [query],
            self.llm_client,
            cache_dir=None,
            input_tokens=config.input_tokens,
            concurrency=config.concurrency.llm,
            allowed_node_ids=allowed_node_ids,
        )[: config.semantic.structure_candidate_limit]
        vector_matches: list[dict[str, Any]] = []
        max_selected_nodes = config.semantic.max_selected_nodes
        if not config.vector.enabled:
            vector_trigger_reason = "disabled"
        elif self._vector_index is None:
            vector_trigger_reason = "index_unavailable"
        elif len(structure_matches) >= max_selected_nodes:
            vector_trigger_reason = "structure_sufficient"
        else:
            vector_trigger_reason = "structure_below_limit"
            vector_matches = vector_search(
                index,
                self._vector_index,
                query,
                self.embedding_client,
                top_k=config.vector.probe_limit,
                score_threshold=config.vector.score_threshold,
                input_tokens=config.input_tokens,
                concurrency=config.concurrency.embedding,
                allowed_node_ids=allowed_node_ids,
            )
        matches, vector_duplicate_count, vector_added_count = _merge_semantic_matches(
            structure_matches,
            vector_matches,
            max_unique_nodes=max_selected_nodes if vector_matches else None,
        )
        candidate_count_before_rerank = len(matches)
        # Vector supplementation and deduplication must finish before this check.
        rerank_triggered = config.rerank.enabled and len(matches) > 1
        if rerank_triggered:
            matches = rerank_matches(
                index,
                query,
                matches,
                self.llm_client,
                config.input_tokens,
                cache_dir=None,
                concurrency=config.concurrency.reranker,
            )
        selected = matches[:max_selected_nodes]
        _route_event(
            "semantic_retrieval",
            scope=scope,
            structure_candidate_count=len(structure_matches),
            vector_enabled=config.vector.enabled,
            vector_triggered=vector_trigger_reason == "structure_below_limit",
            vector_trigger_reason=vector_trigger_reason,
            vector_probe_limit=config.vector.probe_limit,
            vector_probe_count=len(vector_matches),
            vector_duplicate_count=vector_duplicate_count,
            vector_added_count=vector_added_count,
            candidate_count_before_rerank=candidate_count_before_rerank,
            rerank_triggered=rerank_triggered,
            single_candidate_rerank_skipped=config.rerank.enabled and candidate_count_before_rerank == 1,
            selected_node_count=len(selected),
            selected_node_ids=[item.get("node_id") for item in selected],
        )
        return selected


def _should_use_fallback(
    plan: Any,
    *,
    fallback_enabled: bool,
) -> bool:
    """Honor explicit fallback only for deterministic routes that may safely widen scope."""
    methods = {step.method for step in plan.steps}
    scoped_or_complete = methods.intersection({"region", "scan", "hybrid"})
    return bool(fallback_enabled and plan.fallback and not scoped_or_complete)


def _region_node_ids(index: dict[str, Any], regions: list[str]) -> set[str]:
    if not regions:
        raise ValueError("region route requires at least one region")
    roots = {
        str((index.get("top_regions") or {}).get(region) or region)
        for region in regions
    }
    return {
        str(node.get("node_id"))
        for node in index.get("nodes") or []
        if node.get("node_id")
        and any(
            str(node.get("node_id")) == root or str(node.get("node_id")).startswith(f"{root}/")
            for root in roots
        )
    }


def _step_query(query: str, terms: list[str]) -> str:
    clean_terms = [term.strip() for term in terms if term.strip()]
    if not clean_terms:
        return query
    return f"{query}\n检索提示词：{'、'.join(clean_terms)}"


def _add_route_source(
    matches: list[dict[str, Any]],
    method: str,
) -> list[dict[str, Any]]:
    result = []
    for match in matches:
        item = dict(match)
        sources = set(item.get("sources") or [])
        sources.add(method)
        item["sources"] = sorted(sources)
        result.append(item)
    return result


def _interleave_groups(
    groups: list[list[dict[str, Any]]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    position = 0
    while True:
        added = False
        for group in groups:
            if position >= len(group):
                continue
            added = True
            item = group[position]
            node_id = str(item.get("node_id") or "")
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            result.append(item)
            if limit is not None and len(result) >= limit:
                return result
        if not added:
            return result
        position += 1


def _deduplicate_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        node_id = str(match.get("node_id") or "")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        result.append(match)
    return result


def _route_event(event: str, **payload: Any) -> None:
    append_model_event("docxindex_route", route_event=event, **payload)
    _terminal(
        "[DocxIndex][Route] "
        + json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True)
    )


def _merge_semantic_matches(
    structure_matches: list[dict[str, Any]],
    vector_matches: list[dict[str, Any]],
    *,
    max_unique_nodes: int | None,
) -> tuple[list[dict[str, Any]], int, int]:
    result: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for match in structure_matches:
        node_id = str(match.get("node_id") or "")
        if not node_id or node_id in by_id:
            continue
        item = dict(match)
        item["sources"] = ["structure"]
        item["structure_reason"] = match.get("reason")
        by_id[node_id] = item
        result.append(item)

    duplicate_count = 0
    added_count = 0
    for match in vector_matches:
        node_id = str(match.get("node_id") or "")
        if not node_id:
            continue
        existing = by_id.get(node_id)
        if existing is not None:
            duplicate_count += 1
            existing["sources"] = sorted({*(existing.get("sources") or []), "vector"})
            existing["vector_score"] = match.get("score")
            continue
        if max_unique_nodes is not None and len(result) >= max_unique_nodes:
            continue
        item = dict(match)
        item["sources"] = ["vector"]
        item["vector_score"] = match.get("score")
        by_id[node_id] = item
        result.append(item)
        added_count += 1
    return result, duplicate_count, added_count


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
