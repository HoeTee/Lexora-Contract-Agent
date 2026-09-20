from __future__ import annotations

import unittest

from tools.docxindex.detection.heading_profiles import (
    compile_heading_profile,
    default_heading_profiles,
    select_heading_profile,
)
from tools.docxindex.detection.regions import find_first_body_start
from tools.docxindex.indexing.hierarchy import build_hierarchy_for_range
from tools.docxindex.schema import BodyItem


def _paragraph(index: int, text: str) -> BodyItem:
    return BodyItem(
        kind="p",
        anchor=f"p_{index:04d}",
        body_child_index=index,
        text=text,
        xml_path=f"/w:document/w:body/w:p[{index}]",
    )


class ChapterHeadingProfileTests(unittest.TestCase):
    def test_chapter_profile_splits_frontmatter_and_builds_numbered_hierarchy(self) -> None:
        items = [
            _paragraph(1, "采购框架合同"),
            _paragraph(2, "甲方：甲公司"),
            _paragraph(3, "乙方：乙公司"),
            _paragraph(4, "第一章、供货范围"),
            _paragraph(5, "1.1 产品清单"),
            _paragraph(6, "（1）显示器"),
            _paragraph(7, "第二章、支付条件"),
            _paragraph(8, "2.1 支付期限"),
        ]

        profile_match = select_heading_profile(items, default_heading_profiles())
        body_start = find_first_body_start(items, profile_match.profile)
        hierarchy = build_hierarchy_for_range(
            items,
            body_start,
            len(items),
            "body",
            "body",
            profile_match.profile,
        )

        self.assertEqual(profile_match.profile.name, "chapter_decimal")
        self.assertEqual(body_start, 3)
        self.assertEqual([node.title for node in hierarchy], ["第一章、供货范围", "第二章、支付条件"])
        self.assertEqual(hierarchy[0].children[0].title, "1.1 产品清单")
        self.assertEqual(hierarchy[0].children[0].children[0].title, "（1）显示器")

    def test_body_start_falls_back_to_main_section_when_profile_has_no_match(self) -> None:
        unrelated_profile = compile_heading_profile(
            "unrelated",
            ["一、合作背景"],
            ["（一）合作内容"],
            ["1. 服务要求"],
        )
        items = [
            _paragraph(1, "采购框架合同"),
            _paragraph(2, "第一章、供货范围"),
            _paragraph(3, "合同正文"),
        ]

        self.assertEqual(find_first_body_start(items, unrelated_profile), 1)


if __name__ == "__main__":
    unittest.main()
