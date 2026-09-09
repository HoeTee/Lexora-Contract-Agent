from __future__ import annotations

from dataclasses import dataclass

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml import etree

@dataclass(frozen=True)
class DocxAnchorNode:
    anchor_type: str
    anchor_id: str
    text: str
    path: str


def xml_local_name(element) -> str:
    return etree.QName(element).localname


def has_ancestor(element, local_names: set[str]) -> bool:
    parent = element.getparent()
    while parent is not None:
        if xml_local_name(parent) in local_names:
            return True
        parent = parent.getparent()
    return False


def iter_blocks(parent):
    """Yield Paragraph/Table blocks in document order for a document or table cell."""
    for _, block in iter_indexed_blocks(parent):
        yield block


def iter_indexed_blocks(parent):
    """Yield the real XML child index together with each paragraph/table block."""
    if isinstance(parent, DocxDocument):
        parent_element = parent.element.body
    elif isinstance(parent, _Cell):
        parent_element = parent._tc
    else:
        raise TypeError(f"Unsupported block parent: {type(parent)!r}")

    for child_index, child in enumerate(parent_element.iterchildren(), start=1):
        if isinstance(child, CT_P):
            yield child_index, Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield child_index, Table(child, parent)


def paragraph_visible_text(paragraph: Paragraph) -> str:
    """Extract review-visible paragraph text while ignoring deleted/moved-from text."""
    chars: list[str] = []
    for run_element in paragraph._element.iter():
        if xml_local_name(run_element) != "r":
            continue
        if has_ancestor(run_element, {"del", "moveFrom"}):
            continue
        for text_element in run_element.iter():
            if xml_local_name(text_element) != "t":
                continue
            if has_ancestor(text_element, {"del", "moveFrom"}):
                continue
            chars.append(text_element.text or "")
    return "".join(chars)


def paragraph_anchor_id(paragraph: Paragraph, path: str) -> str:
    return f"path:{path}"


def table_anchor_id(table: Table, path: str) -> str:
    return f"path:{path}"


def build_docx_anchor_nodes(docx_path: str) -> list[DocxAnchorNode]:
    """Build paragraph/table retrieval nodes with stable DOCX XML anchor metadata."""
    doc = Document(docx_path)
    nodes: list[DocxAnchorNode] = []
    seen_paragraphs: set[CT_P] = set()

    def walk(parent, path_prefix: str) -> None:
        for block_index, block in iter_indexed_blocks(parent):
            if isinstance(block, Paragraph):
                element = block._element
                if element in seen_paragraphs:
                    continue
                seen_paragraphs.add(element)
                path = f"{path_prefix}p{block_index}"
                text = paragraph_visible_text(block).strip()
                if text:
                    nodes.append(
                        DocxAnchorNode(
                            anchor_type="paragraph",
                            anchor_id=paragraph_anchor_id(block, path),
                            text=text,
                            path=path,
                        )
                    )
                continue

            table_path = f"{path_prefix}tbl{block_index}"
            table_parts: list[str] = []
            for row_index, row in enumerate(block.rows, start=1):
                for cell_index, cell in enumerate(row.cells, start=1):
                    cell_parts: list[str] = []
                    for paragraph in cell.paragraphs:
                        text = paragraph_visible_text(paragraph).strip()
                        if text:
                            cell_parts.append(text)
                    if cell_parts:
                        table_parts.append(" ".join(cell_parts))
                    walk(
                        cell,
                        path_prefix=f"{table_path}/r{row_index}c{cell_index}/",
                    )

            table_text = "\n".join(table_parts).strip()
            if table_text:
                nodes.append(
                    DocxAnchorNode(
                        anchor_type="table",
                        anchor_id=table_anchor_id(block, table_path),
                        text=table_text,
                        path=table_path,
                    )
                )

    walk(doc, "body/")
    return nodes
