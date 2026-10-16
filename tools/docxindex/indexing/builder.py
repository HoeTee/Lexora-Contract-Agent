from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.docxindex.detection.heading_profiles import CompiledHeadingProfile
from tools.docxindex.llm import LLMSettings
from tools.docxindex.schema import DocumentIndex

from .build_pipeline import IndexBuildOptions, run_build_pipeline
from .tables import TABLE_CHUNK_TARGET_TOKENS, TABLE_SPLIT_THRESHOLD_TOKENS
from .token_budget import PARAGRAPH_CHUNK_TARGET_TOKENS, PARAGRAPH_SPLIT_THRESHOLD_TOKENS


def build_document_index(
    docx_path: Path,
    llm_expand: bool = False,
    llm_summary: bool = False,
    llm_settings: LLMSettings | None = None,
    cache_dir: Path | None = None,
    timer: Any | None = None,
    llm_concurrency: int = 10,
    paragraph_split_threshold_tokens: int = PARAGRAPH_SPLIT_THRESHOLD_TOKENS,
    paragraph_chunk_target_tokens: int = PARAGRAPH_CHUNK_TARGET_TOKENS,
    table_split_threshold_tokens: int = TABLE_SPLIT_THRESHOLD_TOKENS,
    table_chunk_target_tokens: int = TABLE_CHUNK_TARGET_TOKENS,
    heading_profiles: tuple[CompiledHeadingProfile, ...] | None = None,
    llm_attachment_hierarchy: bool = False,
    hierarchy_input_max_tokens: int = 20000,
    hierarchy_batch_target_tokens: int = 16000,
    hierarchy_batch_overlap_tokens: int = 800,
    hierarchy_max_levels: int = 6,
    attachment_max_levels: int = 6,
    summary_batch_max_nodes: int = 10,
    summary_batch_max_tokens: int = 20000,
) -> DocumentIndex:
    """Build a source-addressable DOCX index using deterministic and optional LLM stages."""
    options = IndexBuildOptions(
        llm_expand=llm_expand,
        llm_summary=llm_summary,
        llm_settings=llm_settings,
        cache_dir=cache_dir,
        timer=timer,
        llm_concurrency=llm_concurrency,
        paragraph_split_threshold_tokens=paragraph_split_threshold_tokens,
        paragraph_chunk_target_tokens=paragraph_chunk_target_tokens,
        table_split_threshold_tokens=table_split_threshold_tokens,
        table_chunk_target_tokens=table_chunk_target_tokens,
        heading_profiles=heading_profiles,
        llm_attachment_hierarchy=llm_attachment_hierarchy,
        hierarchy_input_max_tokens=hierarchy_input_max_tokens,
        hierarchy_batch_target_tokens=hierarchy_batch_target_tokens,
        hierarchy_batch_overlap_tokens=hierarchy_batch_overlap_tokens,
        hierarchy_max_levels=hierarchy_max_levels,
        attachment_max_levels=attachment_max_levels,
        summary_batch_max_nodes=summary_batch_max_nodes,
        summary_batch_max_tokens=summary_batch_max_tokens,
    )
    return run_build_pipeline(Path(docx_path), options)
