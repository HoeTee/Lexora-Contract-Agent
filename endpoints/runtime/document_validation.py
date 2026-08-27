from __future__ import annotations

import re
import zipfile
from pathlib import Path

from docx import Document
from fastapi import HTTPException


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OLE_DOC_SIGNATURE = bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1")
REQUIRED_DOCX_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
}
CRITERIA_VALIDATION_ERROR = (
    "上传的审查要点文件内容不符合要求：未识别到足够的审查关键词或编号审查项。"
    "请上传包含具体编号审查要点的 DOCX 文件。"
)
CRITERIA_KEYWORDS = {
    "审查",
    "审核",
    "要点",
    "合同",
    "条款",
    "风险",
    "责任",
    "违约",
    "付款",
    "期限",
    "义务",
    "权利",
    "金额",
    "争议",
}
CRITERIA_NUMBERED_ITEM_RE = re.compile(
    r"(?m)^\s*(?:\d+[.、．]|[一二三四五六七八九十]+[、.．]|第[一二三四五六七八九十\d]+条)"
)


def validate_uploaded_docx(path: Path) -> None:
    with path.open("rb") as f:
        header = f.read(8)

    if header.startswith(OLE_DOC_SIGNATURE):
        raise HTTPException(
            status_code=400,
            detail=(
                "上传的文件是旧版 .doc/OLE 文档，不是真正的 .docx 文件。"
                "请先使用 Word 或 LibreOffice 转换为 .docx 后再上传。"
            ),
        )

    if not zipfile.is_zipfile(path):
        raise HTTPException(
            status_code=400,
            detail="上传的文件不是有效的 .docx 文件。",
        )

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=400,
            detail="上传的文件不是有效的 .docx 文件。",
        ) from None

    missing_parts = sorted(REQUIRED_DOCX_PARTS - names)
    if missing_parts:
        raise HTTPException(
            status_code=400,
            detail=(
                "上传的文件扩展名是 .docx，但内部结构"
                f"不是有效的 Word DOCX 结构。缺少内部文件：{missing_parts}"
            ),
        )


def validate_criteria_content(path: Path) -> None:
    try:
        doc = Document(path)
        criteria_text = "\n".join(
            paragraph.text.strip()
            for paragraph in doc.paragraphs
            if paragraph.text and paragraph.text.strip()
        )
    except Exception:
        raise HTTPException(status_code=400, detail="审查要点文件解析失败，请上传可正常读取的 DOCX 文件。") from None

    normalized_text = criteria_text.strip()
    keyword_hits = sum(1 for keyword in CRITERIA_KEYWORDS if keyword in normalized_text)
    numbered_items = CRITERIA_NUMBERED_ITEM_RE.findall(normalized_text)
    if len(normalized_text) < 100 or keyword_hits < 2 or len(numbered_items) < 2:
        raise HTTPException(status_code=400, detail=CRITERIA_VALIDATION_ERROR)
