"""
DOCX cleaner - Remove comments and reject tracked revisions.

Usage:
    from tools.document.file_cleaner import DocxCleaner, clean_docx

    cleaner = DocxCleaner("合同.docx")
    clean_path = cleaner.clean()  # 返回临时文件路径
    # 使用完毕后删除: os.unlink(clean_path)
"""
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Union


class DocxCleaner:
    """清理 DOCX 文件中的修订和批注，返回临时文件路径"""

    WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    KNOWN_IGNORABLE_NS = {
        "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
        "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
        "w16se": "http://schemas.microsoft.com/office/word/2015/wordml/symex",
        "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
        "w16": "http://schemas.microsoft.com/office/word/2018/wordml",
        "w16cex": "http://schemas.microsoft.com/office/word/2018/wordml/cex",
        "w16sdtdh": "http://schemas.microsoft.com/office/word/2020/wordml/sdtdatahash",
        "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    }

    COMMENT_PART_RE = re.compile(r"^word/comments[^/]*\.xml$")
    PEOPLE_PART_RE = re.compile(r"^word/people\.xml$")
    WORD_PART_RE = re.compile(
        r"^word/(document|settings|footnotes|endnotes|header\d+|footer\d+)\.xml$"
    )

    DROP_MARKUP_TAGS = {
        "commentRangeStart", "commentRangeEnd", "commentReference",
        "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
        "customXmlInsRangeStart", "customXmlInsRangeEnd",
        "customXmlDelRangeStart", "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
    }

    DROP_PROPERTY_CHANGE_TAGS = {
        "numberingChange", "cellIns", "cellDel", "cellMerge",
    }

    DROP_SETTINGS_TAGS = {
        "trackRevisions", "doNotTrackMoves", "doNotTrackFormatting", "revisionView",
    }

    ACCEPT_UNWRAP_TAGS = {"ins", "moveTo"}
    ACCEPT_DROP_TAGS = {"del", "moveFrom"}

    PROPERTY_CHANGE_TAGS = {
        "pPrChange": "pPr", "rPrChange": "rPr", "tblPrChange": "tblPr",
        "tblPrExChange": "tblPrEx", "tblGridChange": "tblGrid",
        "tcPrChange": "tcPr", "trPrChange": "trPr", "sectPrChange": "sectPr",
    }

    def __init__(self, input_path: Union[str, Path]):
        """
        初始化清理器

        Args:
            input_path: 输入的 DOCX 文件路径
        """
        self.input_path = Path(input_path)

    def clean(self) -> str:
        """
        执行清理

        Returns:
            清理后的临时文件路径（使用完毕后需手动删除）
        """
        if not self.input_path.exists():
            raise FileNotFoundError(f"输入文件不存在: {self.input_path}")

        # 创建临时文件
        tmp = tempfile.NamedTemporaryFile(delete=True, suffix=".docx")
        tmp.close()

        try:
            with zipfile.ZipFile(self.input_path) as source:
                names = source.namelist()
                removed_parts = self._get_comment_parts(names)
                removed_basenames = {posixpath.basename(n) for n in removed_parts}

                with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as target:
                    for item in source.infolist():
                        if item.filename in removed_parts:
                            continue

                        data = source.read(item.filename)

                        if self.WORD_PART_RE.match(item.filename):
                            data = self._clean_word_xml(
                                data,
                                is_settings=(item.filename == "word/settings.xml"),
                            )
                        elif item.filename == "[Content_Types].xml":
                            data = self._clean_content_types_xml(data, removed_parts)
                        elif item.filename.endswith(".rels"):
                            data = self._clean_relationships_xml(data, removed_basenames)

                        target.writestr(item, data)

        except zipfile.BadZipFile as exc:
            raise ValueError(f"无效的 DOCX 文件: {self.input_path}") from exc

        return tmp.name

    # ==================== 内部方法 ====================

    def _get_comment_parts(self, names):
        return [n for n in names if self.COMMENT_PART_RE.match(n) or self.PEOPLE_PART_RE.match(n)]

    def _local_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[1] if "}" in tag else tag

    def _unwrap_child(self, parent, index: int) -> int:
        child = parent[index]
        grandchildren = list(child)
        parent.remove(child)
        for offset, g in enumerate(grandchildren):
            parent.insert(offset + index, g)
        return len(grandchildren)

    def _clean_word_element(self, element, *, is_settings: bool = False) -> None:

        index = 0
        while index < len(element):
            child = element[index]
            self._clean_word_element(child, is_settings=is_settings)
            tag_name = self._local_name(child.tag)

            if tag_name in self.PROPERTY_CHANGE_TAGS:
                element.remove(child)
                continue

            if tag_name in self.DROP_MARKUP_TAGS or tag_name in self.DROP_PROPERTY_CHANGE_TAGS:
                element.remove(child)
                continue

            if is_settings and tag_name in self.DROP_SETTINGS_TAGS:
                element.remove(child)
                continue

            if tag_name in self.ACCEPT_DROP_TAGS:
                element.remove(child)
                continue

            if tag_name in self.ACCEPT_UNWRAP_TAGS:
                inserted = self._unwrap_child(element, index)
                index += inserted
                continue

            index += 1

    def _clean_word_xml(self, data: bytes, *, is_settings: bool = False) -> bytes:
        from lxml import etree as ET

        root = ET.fromstring(data)
        self._clean_word_element(root, is_settings=is_settings)
        self._repair_mc_ignorable(root)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _repair_mc_ignorable(self, root) -> None:
        """Keep mc:Ignorable aligned with prefixes declared after XML cleanup."""
        attr_name = f"{{{self.MC_NS}}}Ignorable"
        for element in root.iter():
            value = element.attrib.get(attr_name)
            if not value:
                continue

            uri_to_prefix = {
                uri: prefix
                for prefix, uri in element.nsmap.items()
                if prefix and uri
            }
            repaired = []
            seen = set()
            for old_prefix in value.split():
                uri = element.nsmap.get(old_prefix) or self.KNOWN_IGNORABLE_NS.get(old_prefix)
                new_prefix = uri_to_prefix.get(uri)
                if new_prefix and new_prefix not in seen:
                    repaired.append(new_prefix)
                    seen.add(new_prefix)

            if repaired:
                element.attrib[attr_name] = " ".join(repaired)
            else:
                element.attrib.pop(attr_name, None)

    def _clean_content_types_xml(self, data: bytes, removed_parts: set) -> bytes:
        from xml.etree import ElementTree as ET

        root = ET.fromstring(data)
        for child in list(root):
            if self._local_name(child.tag) != "Override":
                continue
            part_name = child.attrib.get("PartName", "").lstrip("/")
            if part_name in removed_parts:
                root.remove(child)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _clean_relationships_xml(self, data: bytes, removed_basenames: set) -> bytes:
        from xml.etree import ElementTree as ET

        root = ET.fromstring(data)
        for child in list(root):
            if self._local_name(child.tag) != "Relationship":
                continue
            target = child.attrib.get("Target", "")
            basename = posixpath.basename(target)
            if basename in removed_basenames:
                root.remove(child)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ==================== 便捷函数 ====================

def clean_docx(input_path: Union[str, Path]) -> str:
    """
    便捷函数：清理 DOCX 文件

    Args:
        input_path: 输入的 DOCX 文件路径

    Returns:
        清理后的临时文件路径（使用完毕后需手动删除）
    """
    return DocxCleaner(input_path).clean()
