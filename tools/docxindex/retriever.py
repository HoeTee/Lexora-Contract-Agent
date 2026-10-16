from __future__ import annotations

import asyncio
import json
import sys
import time
from config import MODEL_SEMAPHORE_CONFIGS
from tools.docxindex.reranker import RerankerClient
from pathlib import Path
from typing import Any

from loggers.call_log import append_route_event, call_stage, reset_call_stage, set_call_stage
from routing.schemas import RoutePlan
from tools.docxindex import build_document_index
from tools.docxindex.detection.patterns import ATTACHMENT_RE, NAMED_ATTACHMENT_RE
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
    section_root_ids,
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
        reranker_model: str | None = None,
        reranker_api_key: str | None = None,
        reranker_base_url: str | None = None,
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
        )
        self.embedding_client = EmbeddingClient(
            self.embedding_settings,
        )
        self.reranker_client = RerankerClient.from_sources(self.config_path, model=reranker_model,
            api_key=reranker_api_key, base_url=reranker_base_url)
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
                        "origin": "docxindex",
                        "failure_stage": getattr(exc, "failure_stage", "indexing"),
                        "http_status": getattr(exc, "http_status", getattr(exc, "status_code", None)),
                        "error_type": getattr(exc, "error_type", type(exc).__name__),
                        "detail": str(exc),
                        "call_id": getattr(exc, "call_id", None),
                        "call_path": getattr(exc, "call_path", None),
                    }
                },
                ensure_ascii=False,
            )

    def artifact_snapshot(self) -> dict[str, Any]:
        """Return serializable diagnostic artifacts for the current in-memory index."""
        if self._index is None:
            raise RuntimeError("docxindex_build_index must be called before exporting artifacts")
        nodes = self._index.get("nodes") or []
        attachments = [
            node for node in nodes
            if str(node.get("node_id") or "").startswith("attachments/")
        ]
        titles = [
            {
                "node_id": node.get("node_id"),
                "node_type": node.get("node_type"),
                "title": node.get("title"),
                "start_index": node.get("start_index"),
                "start_anchor": node.get("start_anchor"),
            }
            for node in nodes
            if str(node.get("title") or "").strip()
        ]
        snapshot: dict[str, Any] = {
            "document_index.json": self._index,
            "structure_tree.json": {"structure_tree": self._index.get("structure_tree") or []},
            "content_store.json": {"nodes": nodes},
            "anchor_store.json": {"anchors": self._index.get("anchor_map") or {}},
            "table_store.json": {"tables": self._index.get("table_map") or {}},
            "titles.json": {"titles": titles},
            "attachments.json": {"attachments": attachments},
        }
        if self._vector_index is not None:
            snapshot["vector_index.json"] = self._vector_index.model_dump(mode="json")
        return snapshot

    async def search(
        self,
        query: str,
        route_override: dict | None = None,
        criterion_id: str | None = None,
        return_metadata: bool = False,
    ) -> str:
        """Retrieve review-ready, DOCX-anchor-aware text for one query."""
        started_at = time.monotonic()
        _terminal(f"[DocxIndex] Searching temporary index: query_chars={len(query)}")
        try:
            result = await asyncio.to_thread(
                self._search_sync, query, route_override, criterion_id, return_metadata
            )
            _terminal(
                "[DocxIndex] Search complete: "
                f"result_chars={len(result)}, elapsed={round(time.monotonic() - started_at, 2)}s"
            )
            return result
        except Exception as exc:
            _terminal(f"[DocxIndex] Search failed: {type(exc).__name__}: {exc}")
            http_status = getattr(exc, "http_status", getattr(exc, "status_code", None))
            error_type = getattr(exc, "error_type", type(exc).__name__)
            if return_metadata:
                return json.dumps({"error": {
                    "origin": "docxindex", "failure_stage": getattr(exc, "failure_stage", "retrieval"),
                    "error_type": error_type, "exception_type": type(exc).__name__,
                    "http_status": http_status, "detail": str(exc),
                    "call_id": getattr(exc, "call_id", None),
                    "call_path": getattr(exc, "call_path", None),
                }}, ensure_ascii=False)
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
            llm_concurrency=MODEL_SEMAPHORE_CONFIGS["llm"]["max_concurrent_requests"],
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
                concurrency=MODEL_SEMAPHORE_CONFIGS["embedding"]["max_concurrent_requests"],
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

    def _search_sync(
        self,
        query: str,
        route_override: dict | None = None,
        criterion_id: str | None = None,
        return_metadata: bool = False,
    ) -> str:
        if self._index is None:
            raise RuntimeError("docxindex_build_index must be called before docxindex_search")

        index = self._index
        config = self.retrieval_config
        route = self.route_config
        if route_override:
            route_plan = RoutePlan.model_validate(route_override)
            route_source = "extracted"
        else:
            route_plan = plan_route(
                index,
                query,
                self.llm_client,
                route,
                cache_dir=None,
                criterion_text=query,
            )
            route_source = "llm"
        _route_event(
            "plan",
            plan=route_plan.model_dump(mode="json"),
            criterion_id=criterion_id,
            route_source=route_source,
        )
        step_groups: list[list[dict[str, Any]]] = []
        deterministic_notices: list[str] = []
        for step_index, step in enumerate(route_plan.steps, 1):
            stage_token = set_call_stage(f"route.{step_index}.{step.method}")
            try:
                if step.method == "title":
                    current = title_matches(index, step)
                elif step.method == "rule":
                    current = rule_matches(index, step)
                    if not current:
                        terms = "、".join(step.terms)
                        deterministic_notices.append(
                            "确定性全文精确检索（已覆盖全部合同原文）："
                            f"以下检索词的出现次数均为0：{terms}。"
                            "若当前审查标准以词语缺失为触发条件，应直接据此判断。"
                        )
                elif step.method == "region":
                    root_ids = _region_root_ids(index, step)
                    allowed_node_ids = _scope_node_ids(index, root_ids)
                    scope = step.regions[0] if step.regions else "section"
                    if step.coverage == "complete":
                        current = _complete_scope_matches(index, root_ids)
                    else:
                        current = self._semantic_matches(
                            index,
                            _step_query(query, step.terms),
                            allowed_node_ids,
                            scope=scope,
                        )
                elif step.method == "scan":
                    allowed_node_ids = None
                    if step.regions:
                        root_ids = _region_root_ids(index, step)
                        allowed_node_ids = _scope_node_ids(index, root_ids)
                    current = scan_matches(
                        index,
                        query,
                        self.llm_client,
                        config.scan_batch_tokens,
                        MODEL_SEMAPHORE_CONFIGS["llm"]["max_concurrent_requests"],
                        allowed_node_ids=allowed_node_ids,
                        candidate_instruction=step.candidate_instruction,
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
                    anchor_terms=step.anchor_terms,
                    coverage=step.coverage,
                    match_count=len(current),
                    node_ids=[item.get("node_id") for item in current],
                )
            finally:
                reset_call_stage(stage_token)

        fallback_used = _should_use_fallback(
            route_plan,
            fallback_enabled=route.fallback_enabled,
        )
        with call_stage("fallback"):
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
        has_complete = any(
            step.method == "region" and step.coverage == "complete"
            for step in route_plan.steps
        )
        selected_node_limit = config.semantic.max_selected_nodes
        ranked_matches = _deduplicate_matches([item for group in step_groups for item in group])
        candidate_count_before_rerank = len(ranked_matches)
        full_coverage = has_scan or has_complete or any(
            step.method == "title" and step.all_titles for step in route_plan.steps
        )
        rerank_triggered = config.rerank.enabled and len(ranked_matches) > 1 and not full_coverage
        stage_token = set_call_stage("route.result")
        try:
            if rerank_triggered:
                ranked_matches = rerank_matches(
                    index, query, ranked_matches, self.reranker_client,
                    config.input_tokens, cache_dir=None,
                    max_selected_nodes=selected_node_limit,
                )
            if not full_coverage:
                ranked_matches = ranked_matches[:selected_node_limit]
        finally:
            reset_call_stage(stage_token)

        content_budget = (
            config.scan_batch_tokens
            if has_scan
            else config.output_tokens
        )
        context = build_content_context(index, ranked_matches, content_budget)
        content_pages = [list(context["content_context"])]
        if has_scan:
            next_part = context["pagination"]["next_part"]
            while next_part is not None:
                page = build_content_context(index, ranked_matches, content_budget, part=next_part)
                content_pages[0].extend(page["content_context"])
                next_part = page["pagination"]["next_part"]
        elif return_metadata:
            next_part = context["pagination"]["next_part"]
            while next_part is not None:
                page = build_content_context(index, ranked_matches, content_budget, part=next_part)
                content_pages.append(list(page["content_context"]))
                next_part = page["pagination"]["next_part"]
        source_counts: dict[str, int] = {}
        for match in ranked_matches:
            for source in match.get("sources") or []:
                source_counts[source] = source_counts.get(source, 0) + 1
        _route_event(
            "result",
            candidate_count_before_rerank=candidate_count_before_rerank,
            rerank_enabled=config.rerank.enabled,
            rerank_triggered=rerank_triggered,
            selected_node_count=len(ranked_matches),
            selected_node_ids=[item.get("node_id") for item in ranked_matches],
            source_counts=source_counts,
            content_parts=len(content_pages),
            has_more=False if has_scan or return_metadata else context["pagination"]["has_more"],
        )
        contents = [_format_anchor_context(index, nodes) for nodes in content_pages]
        if deterministic_notices:
            contents[0] = "\n".join(deterministic_notices) + "\n\n" + contents[0]
        if return_metadata:
            return json.dumps(
                {
                    "contexts": contents,
                    "content_parts": len(contents),
                    "complete": True,
                },
                ensure_ascii=False,
            )
        return contents[0]

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
            allowed_node_ids=allowed_node_ids,
        )[: config.semantic.structure_candidate_limit]
        vector_matches: list[dict[str, Any]] = []
        max_selected_nodes = config.semantic.max_selected_nodes
        vector_triggered = config.vector.enabled and self._vector_index is not None and len(structure_matches) < max_selected_nodes
        if vector_triggered:
            vector_matches = vector_search(
                index,
                self._vector_index,
                query,
                self.embedding_client,
                top_k=config.vector.probe_limit,
                score_threshold=config.vector.score_threshold,
                input_tokens=config.input_tokens,
                allowed_node_ids=allowed_node_ids,
            )
        matches, _, vector_added_count = _merge_semantic_matches(
            structure_matches,
            vector_matches,
            max_unique_nodes=max_selected_nodes if vector_matches else None,
        )
        candidate_count_before_rerank = len(matches)
        _route_event(
            "semantic_retrieval",
            scope=scope,
            structure_candidate_count=len(structure_matches),
            vector_enabled=config.vector.enabled,
            vector_triggered=vector_triggered,
            vector_added_count=vector_added_count,
            candidate_count_before_rerank=candidate_count_before_rerank,
        )
        return matches


def _should_use_fallback(
    plan: Any,
    *,
    fallback_enabled: bool,
) -> bool:
    """Honor explicit fallback only for deterministic routes that may safely widen scope."""
    methods = {step.method for step in plan.steps}
    scoped_or_complete = methods.intersection({"region", "scan", "hybrid"})
    return bool(fallback_enabled and plan.fallback and not scoped_or_complete)


def _region_root_ids(index: dict[str, Any], step: Any) -> list[str]:
    if step.anchor_terms:
        roots = section_root_ids(index, step)
        if not roots:
            return []
        return roots
    return [
        str((index.get("top_regions") or {}).get(region) or region)
        for region in step.regions
    ]


def _scope_node_ids(index: dict[str, Any], roots: list[str]) -> set[str]:
    return {
        str(node.get("node_id"))
        for node in index.get("nodes") or []
        if node.get("node_id")
        and any(
            str(node.get("node_id")) == root or str(node.get("node_id")).startswith(f"{root}/")
            for root in roots
        )
    }


def _region_node_ids(index: dict[str, Any], regions: list[str]) -> set[str]:
    """Return all node IDs below fixed regions (compatibility helper)."""
    roots = [str((index.get("top_regions") or {}).get(region) or region) for region in regions]
    return _scope_node_ids(index, roots)


def _complete_scope_matches(index: dict[str, Any], roots: list[str]) -> list[dict[str, Any]]:
    nodes = {str(node.get("node_id") or ""): node for node in index.get("nodes") or []}
    result: list[dict[str, Any]] = []
    for root_id in roots:
        root = nodes.get(root_id)
        if not root:
            continue
        selected = [root]
        if not str(root.get("text") or "").strip():
            descendants = [
                node
                for node_id, node in nodes.items()
                if node_id.startswith(f"{root_id}/") and str(node.get("text") or "").strip()
            ]
            descendant_ids = {str(node.get("node_id") or "") for node in descendants}
            expanded = [
                node
                for node in descendants
                if not any(
                    ancestor != str(node.get("node_id") or "")
                    and str(node.get("node_id") or "").startswith(f"{ancestor}/")
                    for ancestor in descendant_ids
                )
            ]
            if expanded:
                selected = expanded
        for node in selected:
            result.append(
                {
                    "node_id": node.get("node_id"),
                    "title": node.get("title"),
                    "summary": node.get("summary"),
                    "start_index": node.get("start_index"),
                    "end_index": node.get("end_index"),
                    "start_anchor": node.get("start_anchor"),
                    "end_anchor": node.get("end_anchor"),
                    "token_estimate": node.get("token_estimate"),
                    "sources": ["region_complete"],
                }
            )
    return result


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


def _deduplicate_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for match in matches:
        node_id = str(match.get("node_id") or "")
        if not node_id:
            continue
        existing = by_id.get(node_id)
        if existing is None:
            item = dict(match)
            by_id[node_id] = item
            result.append(item)
            continue
        _merge_duplicate_match(existing, match)
    return result


def _merge_duplicate_match(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    sources = sorted({*(existing.get("sources") or []), *(incoming.get("sources") or [])})
    existing_start = existing.get("start_index")
    existing_end = existing.get("end_index")
    incoming_start = incoming.get("start_index")
    incoming_end = incoming.get("end_index")
    existing_span = -1 if existing_start is None or existing_end is None else existing_end - existing_start
    incoming_span = -1 if incoming_start is None or incoming_end is None else incoming_end - incoming_start
    if incoming_span > existing_span or ("text_override" in existing and "text_override" not in incoming):
        existing.clear()
        existing.update(incoming)
    existing["sources"] = sources


def _route_event(event: str, **payload: Any) -> None:
    token = set_call_stage("route.result") if event == "result" else None
    try:
        append_route_event(event, payload)
    finally:
        if token is not None:
            reset_call_stage(token)
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
    attachment_contexts = _attachment_contexts(index, anchors)
    source_records = {
        f"path:body/{'tbl' if record.get('type') == 'tbl' else 'p'}{record['body_child_index']}": record
        for _, record in anchors
        if record.get("body_child_index") is not None
    }
    parts: list[str] = []
    seen: set[str] = set()
    described_attachments: set[str] = set()
    attachment_evidence_ids: dict[str, None] = {}
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
            # Do not expose a display ordinal beside the real paragraph/table ID.
            # LLM reviewers only need the stable XML anchor and may otherwise
            # confuse a result number such as 19 with path:body/p19.
            header = "## 合同检索证据"
            title = str(node.get("title") or "").strip()
            if title:
                header += f"\n检索片段标题：{title}"
            selection_reason = str(node.get("selection_reason") or "").strip()
            if selection_reason:
                header += f"\n检索候选判定理由：{selection_reason}"
            source_region = str(node.get("node_id") or "").split("/", 1)[0]
            owner_id = str(record.get("node_id") or node.get("node_id") or "")
            attachments = [
                context for context in attachment_contexts
                if context["start_index"] <= body_index <= context["end_index"]
            ]
            if len(attachments) == 1 and (
                not owner_id
                or owner_id == attachments[0]["attachment_context_id"]
                or owner_id.startswith(attachments[0]["attachment_context_id"] + "/")
            ):
                attachment = attachments[0]
                context_id = attachment["attachment_context_id"]
                header += f"\nattachment_context_id: {context_id}"
                if context_id not in described_attachments:
                    described_attachments.add(context_id)
                    for key in ("attachment_heading_xml_anchor_id", "attachment_title_xml_anchor_id"):
                        attachment_evidence_ids[attachment[key]] = None
                    for key, value in attachment.items():
                        if key not in {"attachment_context_id", "start_index", "end_index"}:
                            header += f"\n{key}: {value}"
            elif owner_id.startswith("attachments/") or source_region == "attachments":
                header += "\nattachment_context_id: unknown\nattachment_label: unknown\nattachment_title: unknown"
            if source_region:
                header += f"\nsource_region: {source_region}"
            header += f"\nxml_anchor_type: {anchor_type}\nxml_anchor_id: {anchor_id}"
            parts.append(f"{header}\n内容：\n{text}")
    # Title metadata can be cited by a reviewer. Include its source paragraphs in
    # the same evidence format as direct hits so the ordinary validator can check
    # the exact anchor and quote, without granting metadata IDs special trust.
    for anchor_id in attachment_evidence_ids:
        record = source_records.get(anchor_id)
        if anchor_id in seen or record is None:
            continue
        text = str(record.get("raw_text") or record.get("text") or "").strip()
        if not text:
            continue
        seen.add(anchor_id)
        anchor_type = "table" if record.get("type") == "tbl" else "paragraph"
        parts.append(
            "## 合同检索证据\nsource_region: attachments"
            f"\nxml_anchor_type: {anchor_type}\nxml_anchor_id: {anchor_id}\n内容：\n{text}"
        )
    return "\n\n---\n\n".join(parts) if parts else "未找到相关合同内容。"


def _attachment_contexts(
    index: dict[str, Any], anchors: list[tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Resolve attachment identity from section boundaries and source headings."""
    anchor_map = index.get("anchor_map") or {}
    all_nodes = index.get("nodes") or []
    contexts: list[dict[str, Any]] = []
    for section in all_nodes:
        if section.get("node_type") != "attachment_section":
            continue
        heading = anchor_map.get(section.get("start_anchor")) or {}
        end = anchor_map.get(section.get("end_anchor")) or {}
        start_index, end_index = heading.get("body_child_index"), end.get("body_child_index")
        if heading.get("type") != "p" or start_index is None or end_index is None:
            continue
        if start_index > end_index:
            continue
        heading_text = str(heading.get("raw_text") or heading.get("text") or "").strip()
        label_match = ATTACHMENT_RE.match(heading_text)
        if label_match:
            label = heading_text[:label_match.end(1)]
            inline_title = heading_text[label_match.end(1):].lstrip(" ：:、\t")
        elif NAMED_ATTACHMENT_RE.match(heading_text):
            label = "unknown"
            inline_title = heading_text.split("：" if "：" in heading_text else ":", 1)[1].strip()
        else:
            # A generated section name is not evidence of an original attachment label.
            continue
        title = inline_title or "unknown"
        title_index = start_index if inline_title else None
        if not inline_title:
            following = next((
                (anchor, record) for anchor, record in anchors
                if start_index < record.get("body_child_index", 0) <= end_index
                and str(record.get("raw_text") or record.get("text") or "").strip()
            ), None)
            if following is not None:
                title_anchor, title_record = following
                title_nodes = [
                    node for node in all_nodes
                    if node.get("parent_id") == section.get("node_id")
                    and node.get("start_anchor") == title_anchor
                    and (
                        node.get("node_type") == "visual_title"
                        or (
                            node.get("node_type") == "attachment_heading"
                            and "number_family:unnumbered" in (node.get("confidence_evidence") or [])
                        )
                    )
                ]
                if title_record.get("type") == "p" and title_nodes:
                    title = str(title_record.get("raw_text") or title_record.get("text") or "").strip()
                    title_index = title_record["body_child_index"]
        contexts.append({
            "attachment_context_id": str(section.get("node_id") or ""),
            "start_index": start_index,
            "end_index": end_index,
            "attachment_label": label,
            "attachment_heading_text": heading_text,
            "attachment_heading_xml_anchor_id": f"path:body/p{start_index}",
            "attachment_title": title,
            "attachment_title_xml_anchor_id": f"path:body/p{title_index}" if title_index is not None else "unknown",
        })
    return contexts


def _terminal(message: str) -> None:
    """Emit concise progress without writing a tool-owned log file."""
    print(message, file=sys.stderr, flush=True)
