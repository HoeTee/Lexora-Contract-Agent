from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from loggers.call_log import map_with_context
from pathlib import Path

from tools.docxindex.llm import LLMClient
from tools.docxindex.schema import BodyItem, DocumentNode

from .attachment_hierarchy import infer_hierarchy_children
from .splitter import split_long_leaf
from .token_budget import PARAGRAPH_CHUNK_TARGET_TOKENS, PARAGRAPH_SPLIT_THRESHOLD_TOKENS


def expand_large_leaves(
    roots: list[DocumentNode],
    items: list[BodyItem],
    client: LLMClient,
    cache_dir: Path | None,
    concurrency: int = 10,
    split_threshold_tokens: int = PARAGRAPH_SPLIT_THRESHOLD_TOKENS,
    chunk_target_tokens: int = PARAGRAPH_CHUNK_TARGET_TOKENS,
    input_max_tokens: int = 20000,
    batch_target_tokens: int = 16000,
    batch_overlap_tokens: int = 800,
    max_levels: int = 6,
) -> None:
    """Expand oversized body leaves through the shared local-hierarchy inference pipeline."""
    pending: list[tuple[DocumentNode, int]] = []
    for root in roots:
        _collect_large_leaves(root, 0, split_threshold_tokens, pending)

    def infer(entry: tuple[DocumentNode, int]) -> list[DocumentNode] | None:
        node, _ = entry
        return infer_hierarchy_children(
            node,
            items,
            client,
            cache_dir,
            inside_attachment=False,
            input_max_tokens=input_max_tokens,
            batch_target_tokens=batch_target_tokens,
            batch_overlap_tokens=batch_overlap_tokens,
            max_levels=max_levels,
            split_long_nodes=False,
            paragraph_split_threshold_tokens=split_threshold_tokens,
            paragraph_chunk_target_tokens=chunk_target_tokens,
        )

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        while pending:
            current = pending
            pending = []
            infer_entries: list[tuple[DocumentNode, int]] = []
            for node, depth in current:
                if depth >= max_levels:
                    _fallback_chunk(node, items, split_threshold_tokens, chunk_target_tokens)
                else:
                    infer_entries.append((node, depth))

            results = map_with_context(executor, infer, infer_entries)
            for (node, depth), children in zip(infer_entries, results, strict=True):
                if not children:
                    _fallback_chunk(node, items, split_threshold_tokens, chunk_target_tokens)
                    continue
                node.children = children
                for child in children:
                    _collect_large_leaves(child, depth + 1, split_threshold_tokens, pending)


def _collect_large_leaves(
    node: DocumentNode,
    depth: int,
    split_threshold_tokens: int,
    result: list[tuple[DocumentNode, int]],
) -> None:
    if node.node_type in {"table", "table_chunk", "attachment_heading", "attachment_section", "attachment_parent"}:
        return
    if node.children:
        for child in node.children:
            _collect_large_leaves(child, depth, split_threshold_tokens, result)
        return
    if node.source_start is None or node.source_end is None:
        return
    if node.token_estimate > split_threshold_tokens:
        result.append((node, depth))


def _fallback_chunk(
    node: DocumentNode,
    items: list[BodyItem],
    split_threshold_tokens: int,
    chunk_target_tokens: int,
) -> None:
    if node.source_start is None or node.source_end is None:
        return
    split_long_leaf(
        node,
        items,
        node.source_start,
        node.source_end,
        split_threshold_tokens,
        chunk_target_tokens,
    )
