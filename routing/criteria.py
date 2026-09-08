"""Deterministic parsing of numbered review criteria from DOCX files."""

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SECTION_RE = re.compile(r"^[一二三四五六七八九十]+\s*、")
NUMBER_RE = re.compile(r"^\s*(\d{1,2})\s*[.、)]\s*(.+)$", re.S)


def _ancestors(parents: dict, target: ET.Element) -> list[ET.Element]:
    result = []
    current = target
    while current in parents:
        current = parents[current]
        result.append(current)
    return result


def read_docx_paragraphs(docx_path: Path) -> list[str]:
    with zipfile.ZipFile(docx_path) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    parents = {child: parent for parent in root.iter() for child in parent}
    paragraphs: list[str] = []
    for paragraph in root.findall(f".//{{{W_NS}}}body/{{{W_NS}}}p"):
        text_parts = []
        for text_node in paragraph.iter(f"{{{W_NS}}}t"):
            if any(ancestor.tag == f"{{{W_NS}}}del" for ancestor in _ancestors(parents, text_node)):
                continue
            text_parts.append(text_node.text or "")
        text = "".join(text_parts).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def split_criteria_from_docx(docx_path: str | Path) -> list[dict]:
    """Return numbered criterion skeletons in document order."""
    path = Path(docx_path)
    paragraphs = read_docx_paragraphs(path)
    if not paragraphs:
        raise ValueError(f"criteria DOCX has no readable paragraphs: {path}")
    has_numbered = any(NUMBER_RE.match(text) for text in paragraphs)
    criteria: list[dict] = []
    section = ""
    for index, text in enumerate(paragraphs):
        match = NUMBER_RE.match(text)
        if match:
            criteria.append({
                "number": int(match.group(1)),
                "section": section,
                "criterion": match.group(2).strip(),
            })
            continue
        if index == 0:
            continue
        if SECTION_RE.match(text):
            section = SECTION_RE.sub("", text).strip()
            continue
        if not has_numbered and len(text) <= 10 and not text.endswith(("。", "；", ";", ":", "：")):
            section = text
            continue
        if has_numbered:
            if criteria:
                criteria[-1]["criterion"] += "\n" + text
            continue
        criteria.append({"number": None, "section": section, "criterion": text})
    if not criteria:
        raise ValueError(f"no criteria could be split from DOCX: {path}")
    for position, criterion in enumerate(criteria, 1):
        criterion["id"] = f"C{position}"
        criterion["section"] = criterion["section"] or "其他"
        if criterion["number"] is None:
            criterion["number"] = position
    return criteria
