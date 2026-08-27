"""Compatibility imports for the canonical DOCX table mapping implementation."""

from tools.docxindex.table_mapping.markdown_map import (
    CellMapping,
    MarkdownSource,
    MarkdownSpan,
    SourceResolution,
    TableMarkdownMapping,
    cell_source_refs,
    render_table_element_markdown,
    render_table_markdown,
    resolve_agent_evidence,
)

__all__ = [
    "CellMapping",
    "MarkdownSource",
    "MarkdownSpan",
    "SourceResolution",
    "TableMarkdownMapping",
    "cell_source_refs",
    "render_table_element_markdown",
    "render_table_markdown",
    "resolve_agent_evidence",
]
