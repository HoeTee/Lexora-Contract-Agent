from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.docxindex.detection import (
    find_attachment_parent,
    find_first_body_start,
    find_standalone_attachment_start,
    find_tail_start,
)
from tools.docxindex.detection.heading_profiles import (
    CompiledHeadingProfile,
    default_heading_profiles,
    select_heading_profile,
)
from tools.docxindex.llm import LLMClient, LLMSettings
from tools.docxindex.parser import read_docx_items
from tools.docxindex.schema import BodyItem, DocumentIndex, DocumentNode

from .anchors import build_anchor_map
from .attachments import build_attachments
from .hierarchy import build_hierarchy_for_range, flatten_nodes
from .llm_expand import expand_large_leaves
from .llm_summary import summarize_nodes
from .node_factory import make_node
from .splitter import split_long_leaves
from .tables import attach_table_nodes, collect_table_map
from .token_budget import STRUCTURE_INLINE_BUDGET_TOKENS, STRUCTURE_PAGED_BUDGET_TOKENS


@dataclass(frozen=True)
class IndexBuildOptions:
    llm_expand: bool
    llm_summary: bool
    llm_settings: LLMSettings | None
    cache_dir: Path | None
    timer: Any | None
    llm_concurrency: int
    paragraph_split_threshold_tokens: int
    paragraph_chunk_target_tokens: int
    table_split_threshold_tokens: int
    table_chunk_target_tokens: int
    heading_profiles: tuple[CompiledHeadingProfile, ...] | None
    llm_attachment_hierarchy: bool
    hierarchy_input_max_tokens: int
    hierarchy_batch_target_tokens: int
    hierarchy_batch_overlap_tokens: int
    hierarchy_max_levels: int
    hierarchy_retry_count: int
    attachment_max_levels: int
    summary_batch_max_nodes: int
    summary_batch_max_tokens: int


@dataclass(frozen=True)
class DetectedRegions:
    heading_profile: CompiledHeadingProfile
    profile_match: Any
    body_start: int
    body_end: int
    body_content_end: int
    tail_start: int | None
    attachment_parent: int | None
    attachment_region_start: int | None


def run_build_pipeline(docx_path: Path, options: IndexBuildOptions) -> DocumentIndex:
    with _stage(options.timer, "build.read_docx_items"):
        items = read_docx_items(docx_path)
    if not items:
        raise ValueError(f"No readable word/document.xml body found: {docx_path}")

    with _stage(options.timer, "build.detect_regions"):
        regions = _detect_regions(items, options.heading_profiles)

    llm_client, llm_settings = _initialize_llm(options)
    roots = _build_deterministic_roots(items, regions, llm_client, options)
    _apply_llm_enrichment(roots, items, llm_client, options)

    with _stage(options.timer, "build.finalize_index"):
        return _finalize_index(docx_path, items, roots, regions, llm_settings, options)


def _detect_regions(
    items: list[BodyItem],
    heading_profiles: tuple[CompiledHeadingProfile, ...] | None,
) -> DetectedRegions:
    compiled_profiles = heading_profiles or default_heading_profiles()
    provisional_attachment_parent = find_attachment_parent(items, 0)
    provisional_attachment_start = provisional_attachment_parent
    if provisional_attachment_start is None:
        provisional_attachment_start = find_standalone_attachment_start(items, 0)
    profile_scan_end = provisional_attachment_start if provisional_attachment_start is not None else len(items)
    profile_match = select_heading_profile(items[:profile_scan_end], compiled_profiles)
    heading_profile = profile_match.profile
    body_start = find_first_body_start(items, heading_profile)
    attachment_parent = find_attachment_parent(items, body_start)
    standalone_attachment_start = None
    if attachment_parent is None:
        standalone_attachment_start = find_standalone_attachment_start(items, body_start)
    attachment_region_start = attachment_parent if attachment_parent is not None else standalone_attachment_start
    body_end = attachment_region_start if attachment_region_start is not None else len(items)
    tail_start = find_tail_start(items, body_start, body_end)
    body_content_end = tail_start if tail_start is not None else body_end
    return DetectedRegions(
        heading_profile=heading_profile,
        profile_match=profile_match,
        body_start=body_start,
        body_end=body_end,
        body_content_end=body_content_end,
        tail_start=tail_start,
        attachment_parent=attachment_parent,
        attachment_region_start=attachment_region_start,
    )


def _initialize_llm(options: IndexBuildOptions) -> tuple[LLMClient | None, LLMSettings | None]:
    llm_settings = options.llm_settings
    if not (options.llm_expand or options.llm_summary or options.llm_attachment_hierarchy):
        return None, llm_settings
    with _stage(options.timer, "build.init_llm_client"):
        if llm_settings is None:
            llm_settings = LLMSettings.from_sources()
        return LLMClient(llm_settings, max_concurrency=options.llm_concurrency), llm_settings


def _build_deterministic_roots(
    items: list[BodyItem],
    regions: DetectedRegions,
    llm_client: LLMClient | None,
    options: IndexBuildOptions,
) -> list[DocumentNode]:
    split_long_nodes = not options.llm_expand
    roots: list[DocumentNode] = []
    with _stage(options.timer, "build.build_body_hierarchy"):
        frontmatter = make_node(
            "frontmatter", "frontmatter", "合同首部", items, 0, max(regions.body_start, 1), 0, None
        )
        body = make_node(
            "body",
            "body",
            "正文",
            items,
            regions.body_start,
            max(regions.body_content_end, regions.body_start + 1),
            0,
            None,
        )
        body.children = build_hierarchy_for_range(
            items,
            regions.body_start,
            regions.body_content_end,
            "body",
            "body",
            regions.heading_profile,
            split_long_nodes=split_long_nodes,
            paragraph_split_threshold_tokens=options.paragraph_split_threshold_tokens,
            paragraph_chunk_target_tokens=options.paragraph_chunk_target_tokens,
        )
        if regions.body_start > 0:
            _attach_tables(frontmatter, items, 0, regions.body_start, options)
        _attach_tables(body, items, regions.body_start, regions.body_content_end, options)
        roots.extend([frontmatter, body])

    with _stage(options.timer, "build.build_tail"):
        if regions.tail_start is not None and regions.tail_start < regions.body_end:
            tail = make_node(
                "tail", "tail", "合同末尾", items, regions.tail_start, regions.body_end, 0, None
            )
            _attach_tables(tail, items, regions.tail_start, regions.body_end, options)
            roots.append(tail)
        else:
            roots.append(
                DocumentNode(
                    node_id="tail",
                    node_type="tail",
                    title="合同末尾",
                    start_anchor="",
                    end_anchor="",
                    level=0,
                )
            )

    with _stage(options.timer, "build.build_attachments"):
        roots.append(_build_attachment_root(items, regions, llm_client, split_long_nodes, options))
    return roots


def _attach_tables(
    node: DocumentNode,
    items: list[BodyItem],
    start: int,
    end: int,
    options: IndexBuildOptions,
) -> None:
    attach_table_nodes(
        node,
        items,
        start,
        end,
        split_threshold_tokens=options.table_split_threshold_tokens,
        chunk_target_tokens=options.table_chunk_target_tokens,
    )


def _build_attachment_root(
    items: list[BodyItem],
    regions: DetectedRegions,
    llm_client: LLMClient | None,
    split_long_nodes: bool,
    options: IndexBuildOptions,
) -> DocumentNode:
    attachments = DocumentNode(
        node_id="attachments", node_type="attachments", title="附件", start_anchor="", end_anchor="", level=0
    )
    if regions.attachment_region_start is None:
        return attachments
    parent_title = items[regions.attachment_parent].text if regions.attachment_parent is not None else "附件"
    child_start = (
        regions.attachment_parent + 1
        if regions.attachment_parent is not None
        else regions.attachment_region_start
    )
    parent = make_node(
        "attachments/parent",
        "attachment_parent",
        parent_title,
        items,
        regions.attachment_region_start,
        len(items),
        1,
        "attachments",
    )
    parent.children = build_attachments(
        items,
        child_start,
        len(items),
        parent.node_id,
        split_long_nodes=split_long_nodes,
        paragraph_split_threshold_tokens=options.paragraph_split_threshold_tokens,
        paragraph_chunk_target_tokens=options.paragraph_chunk_target_tokens,
        table_split_threshold_tokens=options.table_split_threshold_tokens,
        table_chunk_target_tokens=options.table_chunk_target_tokens,
        llm_client=llm_client if options.llm_attachment_hierarchy else None,
        cache_dir=options.cache_dir,
        hierarchy_input_max_tokens=options.hierarchy_input_max_tokens,
        hierarchy_batch_target_tokens=options.hierarchy_batch_target_tokens,
        hierarchy_batch_overlap_tokens=options.hierarchy_batch_overlap_tokens,
        hierarchy_retry_count=options.hierarchy_retry_count,
        attachment_max_levels=options.attachment_max_levels,
        llm_concurrency=options.llm_concurrency,
    )
    attachments.children.append(parent)
    return attachments


def _apply_llm_enrichment(
    roots: list[DocumentNode],
    items: list[BodyItem],
    llm_client: LLMClient | None,
    options: IndexBuildOptions,
) -> None:
    if not (options.llm_expand or options.llm_summary):
        return
    if llm_client is None:
        raise RuntimeError("LLM client was not initialized")
    if options.llm_expand:
        with _stage(options.timer, "build.llm_expand"):
            expand_large_leaves(
                roots,
                items,
                llm_client,
                options.cache_dir,
                concurrency=options.llm_concurrency,
                split_threshold_tokens=options.paragraph_split_threshold_tokens,
                chunk_target_tokens=options.paragraph_chunk_target_tokens,
                input_max_tokens=options.hierarchy_input_max_tokens,
                batch_target_tokens=options.hierarchy_batch_target_tokens,
                batch_overlap_tokens=options.hierarchy_batch_overlap_tokens,
                max_levels=options.hierarchy_max_levels,
                retry_count=options.hierarchy_retry_count,
            )
        with _stage(options.timer, "build.split_long_leaves_after_llm_expand"):
            split_long_leaves(
                roots,
                items,
                options.paragraph_split_threshold_tokens,
                options.paragraph_chunk_target_tokens,
            )
    if options.llm_summary:
        with _stage(options.timer, "build.llm_summary"):
            stats = summarize_nodes(
                roots,
                llm_client,
                options.cache_dir,
                concurrency=options.llm_concurrency,
                batch_max_nodes=options.summary_batch_max_nodes,
                batch_max_tokens=options.summary_batch_max_tokens,
            )
            if options.timer is not None:
                options.timer.note("build.llm_summary.eligible_nodes", stats["eligible_nodes"])
                options.timer.note("build.llm_summary.batches", stats["batches"])
                options.timer.note("build.llm_summary.fallback_nodes", stats["fallback_nodes"])


def _finalize_index(
    docx_path: Path,
    items: list[BodyItem],
    roots: list[DocumentNode],
    regions: DetectedRegions,
    llm_settings: LLMSettings | None,
    options: IndexBuildOptions,
) -> DocumentIndex:
    _fill_key_items(roots)
    flat_nodes = flatten_nodes(roots)
    return DocumentIndex(
        source_file=str(docx_path),
        top_regions={
            "frontmatter": "frontmatter",
            "body": "body",
            "tail": "tail",
            "attachments": "attachments",
        },
        settings={
            "paragraph_split_threshold_tokens": options.paragraph_split_threshold_tokens,
            "paragraph_chunk_target_tokens": options.paragraph_chunk_target_tokens,
            "heading_profile": regions.heading_profile.name,
            "heading_profile_score": regions.profile_match.score,
            "heading_level_matches": regions.profile_match.level_counts,
            "heading_orphan_matches": regions.profile_match.orphan_count,
            "summary_tree_inline_budget_tokens": STRUCTURE_INLINE_BUDGET_TOKENS,
            "summary_tree_paged_budget_tokens": STRUCTURE_PAGED_BUDGET_TOKENS,
            "llm_expand_enabled": options.llm_expand,
            "llm_summary_enabled": options.llm_summary,
            "llm_attachment_hierarchy_enabled": options.llm_attachment_hierarchy,
            "hierarchy_input_max_tokens": options.hierarchy_input_max_tokens,
            "hierarchy_batch_target_tokens": options.hierarchy_batch_target_tokens,
            "hierarchy_batch_overlap_tokens": options.hierarchy_batch_overlap_tokens,
            "hierarchy_max_levels": options.hierarchy_max_levels,
            "hierarchy_retry_count": options.hierarchy_retry_count,
            "attachment_max_levels": options.attachment_max_levels,
            "llm_model": llm_settings.model if llm_settings else None,
            "llm_concurrency": options.llm_concurrency,
            "table_markdown_enabled": True,
            "table_split_threshold_tokens": options.table_split_threshold_tokens,
            "table_chunk_target_tokens": options.table_chunk_target_tokens,
        },
        nodes=[node.storage_view() for node in flat_nodes],
        structure_tree=[node.structure_view() for node in roots],
        anchor_map=build_anchor_map(items, flat_nodes),
        table_map=collect_table_map(items),
    )


def _fill_key_items(nodes: list[DocumentNode]) -> None:
    for node in nodes:
        if node.children:
            node.key_items = [child.title for child in node.children[:8] if child.title]
            _fill_key_items(node.children)


def _stage(timer: Any | None, name: str):
    if timer is None:
        return nullcontext()
    return timer.stage(name)
