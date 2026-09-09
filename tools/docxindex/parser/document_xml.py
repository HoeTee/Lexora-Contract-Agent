from __future__ import annotations

from contextlib import suppress

from pathlib import Path

from lxml import etree
from tools.docxindex.table_mapping import render_table_element_markdown

from tools.docxindex.schema import BodyItem

from .numbering_xml import NumberingResolver, apply_numbering_prefix
from .package import DocxPackage
from .styles_xml import load_styles, style_outline
from .xml_utils import NS, attr_value, has_ancestor_within, text_of, w_tag, xml_path, xpath_one


def table_text(element: etree._Element) -> str:
    rows = []
    for row in element.xpath(".//w:tr", namespaces=NS):
        cells = [text_of(cell) for cell in row.xpath("./w:tc", namespaces=NS)]
        if any(cells):
            rows.append("\t".join(cells))
    return "\n".join(rows).strip() or "[TABLE]"


def paragraph_item(
    element: etree._Element,
    styles: dict[str, dict[str, str | None]],
    numbering: NumberingResolver,
    anchor: str,
    body_child_index: int,
) -> BodyItem:
    ppr = xpath_one(element, "./w:pPr")
    direct_outline = None
    style_id = None
    alignment = None
    num_id = None
    num_ilvl = None
    if ppr is not None:
        outline = attr_value(xpath_one(ppr, "./w:outlineLvl"))
        if outline is not None:
            try:
                direct_outline = int(outline)
            except ValueError:
                direct_outline = None
        style_id = attr_value(xpath_one(ppr, "./w:pStyle"))
        alignment = attr_value(xpath_one(ppr, "./w:jc"))
        num_id = attr_value(xpath_one(ppr, "./w:numPr/w:numId"))
        ilvl = attr_value(xpath_one(ppr, "./w:numPr/w:ilvl"))
        if ilvl is not None:
            try:
                num_ilvl = int(ilvl)
            except ValueError:
                num_ilvl = None

    total_chars = 0
    bold_chars = 0
    sizes: list[int] = []
    for run in element.xpath(".//w:r", namespaces=NS):
        if has_ancestor_within(run, element, {"del", "moveFrom"}):
            continue
        run_text = text_of(run)
        if not run_text:
            continue
        total_chars += len(run_text)
        rpr = xpath_one(run, "./w:rPr")
        if rpr is None:
            continue
        if xpath_one(rpr, "./w:b") is not None:
            bold_chars += len(run_text)
        size = attr_value(xpath_one(rpr, "./w:sz"))
        if size:
            with suppress(ValueError):
                sizes.append(int(size))

    raw_text = text_of(element)
    numbering_prefix = numbering.next_prefix(num_id, num_ilvl)
    display_text = apply_numbering_prefix(raw_text, numbering_prefix)

    return BodyItem(
        kind="p",
        anchor=anchor,
        body_child_index=body_child_index,
        text=display_text,
        raw_text=raw_text,
        xml_path=xml_path(element),
        direct_outline=direct_outline,
        style_outline=style_outline(style_id, styles),
        style_id=style_id,
        style_name=styles.get(style_id, {}).get("name") if style_id else None,
        num_id=num_id,
        num_ilvl=num_ilvl,
        numbering_prefix=numbering_prefix,
        alignment=alignment,
        bold_fraction=bold_chars / total_chars if total_chars else 0.0,
        max_font_size=max(sizes) if sizes else None,
        page_breaks=len(element.xpath(".//w:lastRenderedPageBreak", namespaces=NS)),
    )


def read_docx_items(path: Path) -> list[BodyItem]:
    package = DocxPackage(path)
    document = package.read_xml("word/document.xml")
    styles = load_styles(package.try_read_xml("word/styles.xml"))
    numbering = NumberingResolver(package.try_read_xml("word/numbering.xml"))
    body = xpath_one(document, ".//w:body")
    if body is None:
        return []

    items: list[BodyItem] = []
    paragraph_index = 0
    table_index = 0
    page = 1
    has_page_break = False
    for body_child_index, child in enumerate(list(body), start=1):
        if child.tag == w_tag("p"):
            paragraph_index += 1
            item = paragraph_item(child, styles, numbering, f"p_{paragraph_index:04d}", body_child_index)
        elif child.tag == w_tag("tbl"):
            table_index += 1
            mapping = render_table_element_markdown(child, f"body/tbl{body_child_index}")
            item = BodyItem(
                kind="tbl",
                anchor=f"tbl_{table_index:04d}",
                body_child_index=body_child_index,
                text=mapping.markdown,
                raw_text=table_text(child),
                xml_path=xml_path(child),
                page_breaks=len(child.xpath(".//w:lastRenderedPageBreak", namespaces=NS)),
                table_mapping=mapping.to_dict(),
            )
        else:
            continue
        item.page_start = page
        if item.page_breaks:
            has_page_break = True
            page += item.page_breaks
        item.page_end = page
        items.append(item)

    if not has_page_break:
        for item in items:
            item.page_start = None
            item.page_end = None
    return items
