from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from loggers.call_log import map_with_context
from pathlib import Path

from tools.docxindex.detection import attachment_starts, heading_level, heading_score, is_plain_label, is_visual_title
from tools.docxindex.llm import LLMClient
from tools.docxindex.schema import BodyItem, DocumentNode

from .attachment_hierarchy import infer_attachment_children
from .node_factory import make_node
from .splitter import split_long_leaf
from .tables import attach_table_nodes
from .token_budget import PARAGRAPH_CHUNK_TARGET_TOKENS, PARAGRAPH_SPLIT_THRESHOLD_TOKENS


def build_attachment_children(
    items: list[BodyItem],
    start: int,
    end: int,
    parent_id: str,
    split_long_nodes: bool = True,
    paragraph_split_threshold_tokens: int = PARAGRAPH_SPLIT_THRESHOLD_TOKENS,
    paragraph_chunk_target_tokens: int = PARAGRAPH_CHUNK_TARGET_TOKENS,
) -> list[DocumentNode]:
    nodes: list[DocumentNode] = []
    candidate_positions = []
    for position, index in enumerate(range(start, end)):
        item = items[index]
        if heading_level(item) is not None:
            candidate_positions.append((index, "heading", heading_level(item)))
        elif is_visual_title(item, items[index + 1 : end], position):
            candidate_positions.append((index, "visual_title", None))
        elif is_plain_label(item):
            candidate_positions.append((index, "plain_label", None))

    for pos, (index, kind, level) in enumerate(candidate_positions):
        next_index = candidate_positions[pos + 1][0] if pos + 1 < len(candidate_positions) else end
        title = items[index].text
        node_type = kind
        score, evidence = heading_score(items[index], level, inside_attachment=True)
        evidence.append(kind)
        node = make_node(
            f"{parent_id}/{kind}_{pos + 1:03d}",
            node_type,
            title,
            items,
            index,
            next_index,
            level,
            parent_id,
            score,
            evidence,
        )
        if split_long_nodes:
            split_long_leaf(
                node,
                items,
                index,
                next_index,
                paragraph_split_threshold_tokens,
                paragraph_chunk_target_tokens,
            )
        nodes.append(node)
    return nodes


def build_attachments(
    items: list[BodyItem],
    start: int,
    end: int,
    parent_id: str,
    split_long_nodes: bool = True,
    paragraph_split_threshold_tokens: int = PARAGRAPH_SPLIT_THRESHOLD_TOKENS,
    paragraph_chunk_target_tokens: int = PARAGRAPH_CHUNK_TARGET_TOKENS,
    table_split_threshold_tokens: int = 20000,
    table_chunk_target_tokens: int = 18000,
    llm_client: LLMClient | None = None,
    cache_dir: Path | None = None,
    hierarchy_input_max_tokens: int = 20000,
    hierarchy_batch_target_tokens: int = 16000,
    hierarchy_batch_overlap_tokens: int = 800,
    hierarchy_retry_count: int = 3,
    attachment_max_levels: int = 6,
    llm_concurrency: int = 10,
) -> list[DocumentNode]:
    starts = attachment_starts(items, start, end)
    attachment_ranges: list[tuple[DocumentNode, int, int]] = []
    for pos, attach_start in enumerate(starts):
        attach_end = starts[pos + 1] if pos + 1 < len(starts) else end
        item = items[attach_start]
        score, evidence = heading_score(item, None, inside_attachment=True)
        evidence.append("attachment_section")
        node = make_node(
            f"attachments/att_{pos + 1:03d}",
            "attachment_section",
            item.text,
            items,
            attach_start,
            attach_end,
            None,
            parent_id,
            score,
            evidence,
        )
        attachment_ranges.append((node, attach_start, attach_end))

    inferred_results: list[list[DocumentNode] | None] = [None] * len(attachment_ranges)
    if llm_client is not None and attachment_ranges:
        def infer(entry: tuple[DocumentNode, int, int]) -> list[DocumentNode] | None:
            node, _, _ = entry
            return infer_attachment_children(
                node,
                items,
                llm_client,
                cache_dir,
                input_max_tokens=hierarchy_input_max_tokens,
                batch_target_tokens=hierarchy_batch_target_tokens,
                batch_overlap_tokens=hierarchy_batch_overlap_tokens,
                max_levels=attachment_max_levels,
                retry_count=hierarchy_retry_count,
                split_long_nodes=split_long_nodes,
                paragraph_split_threshold_tokens=paragraph_split_threshold_tokens,
                paragraph_chunk_target_tokens=paragraph_chunk_target_tokens,
            )

        with ThreadPoolExecutor(max_workers=max(1, llm_concurrency)) as executor:
            inferred_results = map_with_context(executor, infer, attachment_ranges)

    nodes: list[DocumentNode] = []
    for (node, attach_start, attach_end), inferred_children in zip(attachment_ranges, inferred_results, strict=True):
        if inferred_children is not None:
            node.children = inferred_children
            node.confidence_evidence.append("llm_attachment_hierarchy_applied")
        else:
            node.children = build_attachment_children(
                items,
                attach_start + 1,
                attach_end,
                node.node_id,
                split_long_nodes,
                paragraph_split_threshold_tokens,
                paragraph_chunk_target_tokens,
            )
            if llm_client is not None:
                node.confidence_evidence.append("llm_attachment_hierarchy_fallback")
        attach_table_nodes(
            node,
            items,
            attach_start + 1,
            attach_end,
            split_threshold_tokens=table_split_threshold_tokens,
            chunk_target_tokens=table_chunk_target_tokens,
        )
        nodes.append(node)
    return nodes
