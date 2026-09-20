from __future__ import annotations

from tools.docxindex.schema import BodyItem

from .heading_profiles import CompiledHeadingProfile
from .patterns import (
    ATTACHMENT_PARENT_RE,
    ATTACHMENT_RE,
    LEVEL2_RE,
    MAIN_SECTION_RE,
    NAMED_ATTACHMENT_RE,
    SIGNATURE_LINE_RE,
    SIGNATURE_TABLE_RE,
    TAIL_MARKER_RE,
)

def find_first_body_start(items: list[BodyItem], profile: CompiledHeadingProfile | None = None) -> int:
    if profile is not None:
        for index, item in enumerate(items):
            if item.kind == "p" and profile.level_for(item.text) == 1:
                return index
    for index, item in enumerate(items):
        if item.kind == "p" and MAIN_SECTION_RE.match(item.text):
            return index
    cn_headings = [index for index, item in enumerate(items) if item.kind == "p" and LEVEL2_RE.match(item.text)]
    return cn_headings[0] if len(cn_headings) >= 2 else 0


def find_attachment_parent(items: list[BodyItem], body_start: int) -> int | None:
    for index in range(body_start, len(items)):
        item = items[index]
        if item.kind == "p" and ATTACHMENT_PARENT_RE.match(item.text):
            return index
    return None


def find_standalone_attachment_start(items: list[BodyItem], body_start: int) -> int | None:
    """Find a real attachment heading when the contract has no formal attachment parent."""
    for index in range(body_start, len(items)):
        item = items[index]
        if item.kind != "p" or item.effective_outline is None or not 0 <= item.effective_outline <= 8:
            continue
        text = item.text.strip()
        if ATTACHMENT_RE.match(text) or NAMED_ATTACHMENT_RE.match(text):
            return index
    return None


def find_tail_start(items: list[BodyItem], body_start: int, body_end: int) -> int | None:
    for index in range(body_end - 1, body_start - 1, -1):
        if items[index].kind == "p" and TAIL_MARKER_RE.search(items[index].text):
            return index
    search_start = max(body_start, body_end - max(20, (body_end - body_start) // 3))
    signature_items = [
        index
        for index in range(search_start, body_end)
        if (
            items[index].kind == "p" and SIGNATURE_LINE_RE.search(items[index].text.strip())
        ) or (
            items[index].kind == "tbl" and SIGNATURE_TABLE_RE.search(items[index].text)
        )
    ]
    if signature_items:
        return signature_items[0]
    return None
