from __future__ import annotations

import unittest

from agents.orchestrator import OrchestratorAgent
from routing.schemas import ReviewExecutionPlan
from tools.docxindex.retriever import _format_anchor_context


def _anchor(index: int, text: str, owner: str, kind: str = "p") -> dict:
    return {
        "body_child_index": index,
        "type": kind,
        "text": text,
        "raw_text": text,
        "node_id": owner,
    }


def _section(node_id: str, start: int, end: int) -> dict:
    return {
        "node_id": node_id,
        "node_type": "attachment_section",
        "start_anchor": f"p_{start}",
        "end_anchor": f"p_{end}",
        "start_index": start,
        "end_index": end,
    }


def _index() -> dict:
    return {
        "nodes": [
            _section("attachments/att_001", 10, 13),
            {
                "node_id": "attachments/att_001/title",
                "parent_id": "attachments/att_001",
                "node_type": "visual_title",
                "start_anchor": "p_11",
                "title": "生成的标题不能覆盖原文",
            },
            _section("attachments/att_002", 20, 23),
            {
                "node_id": "attachments/att_002/title",
                "parent_id": "attachments/att_002",
                "node_type": "attachment_heading",
                "start_anchor": "p_21",
                "confidence_evidence": ["number_family:unnumbered", "llm_attachment_hierarchy"],
            },
        ],
        "anchor_map": {
            "p_1": _anchor(1, "设备配置清单详见附件1", "body/section"),
            "p_5": _anchor(5, "1. 附件1《设备清单》", "attachments/parent"),
            "p_10": _anchor(10, "附件1", "attachments/att_001"),
            "p_11": _anchor(11, "设备配置清单", "attachments/att_001/title"),
            "tbl_12": _anchor(12, "型号与数量表格", "attachments/att_001/title/table", "tbl"),
            "p_13": _anchor(13, "产品规格", "attachments/att_001/title"),
            "p_20": _anchor(20, "附件2", "attachments/att_002"),
            "p_21": _anchor(21, "售后服务（保修）承诺书", "attachments/att_002/title"),
            "p_22": _anchor(22, "设备配置清单详见附件1；本承诺提供三年保修。", "attachments/att_002/title"),
            "p_23": _anchor(23, "承诺人盖章", "attachments/att_002/title"),
        },
    }


def _evidence(start: int, end: int, owner: str = "attachments") -> dict:
    return {"node_id": owner, "start_index": start, "end_index": end}


class AttachmentContextTests(unittest.TestCase):
    def test_two_attachments_and_table_use_source_titles_and_real_xml_paths(self) -> None:
        result = _format_anchor_context(_index(), [_evidence(12, 12), _evidence(22, 22)])

        first, second = result.split("\n\n---\n\n")[:2]
        self.assertIn("xml_anchor_id: path:body/tbl12", first)
        self.assertIn("attachment_label: 附件1", first)
        self.assertIn("attachment_title: 设备配置清单", first)
        self.assertIn("attachment_heading_xml_anchor_id: path:body/p10", first)
        self.assertIn("attachment_title_xml_anchor_id: path:body/p11", first)
        self.assertNotIn("生成的标题", result)
        self.assertIn("attachment_label: 附件2", second)
        self.assertIn("attachment_title: 售后服务（保修）承诺书", second)
        self.assertIn("attachment_heading_xml_anchor_id: path:body/p20", second)
        self.assertIn("attachment_title_xml_anchor_id: path:body/p21", second)
        self.assertIn("xml_anchor_id: path:body/p22", second)

    def test_body_reference_and_attachment_directory_do_not_assign_ownership(self) -> None:
        result = _format_anchor_context(_index(), [
            _evidence(1, 1, "body/section"), _evidence(5, 5), _evidence(22, 22),
        ])

        body, directory, attachment = result.split("\n\n---\n\n")[:3]
        self.assertNotIn("attachment_context_id:", body)
        self.assertIn("attachment_context_id: unknown", directory)
        self.assertNotIn("attachment_label: 附件1", directory)
        self.assertIn("attachment_label: 附件2", attachment)
        self.assertNotIn("attachment_label: 附件1", attachment)

    def test_attachment_metadata_is_described_once_and_referenced_afterward(self) -> None:
        result = _format_anchor_context(_index(), [_evidence(22, 23)])

        self.assertEqual(result.count("attachment_context_id: attachments/att_002"), 2)
        self.assertEqual(result.count("attachment_title_xml_anchor_id: path:body/p21"), 1)
        self.assertNotIn("型号与数量表格", result)

    def test_attachment_metadata_preserves_downstream_evidence_parsing(self) -> None:
        result = _format_anchor_context(_index(), [_evidence(12, 12), _evidence(22, 22)])

        evidence = OrchestratorAgent._retrieval_anchor_evidence(result)

        self.assertEqual(len(evidence), 6)
        self.assertEqual(evidence[0], {
            "source_region": "attachments",
            "xml_anchor_type": "table",
            "xml_anchor_id": "path:body/tbl12",
            "content": "型号与数量表格",
        })
        self.assertEqual(evidence[1]["source_region"], "attachments")
        self.assertEqual(evidence[1]["xml_anchor_id"], "path:body/p22")
        self.assertEqual(evidence[1]["content"], "设备配置清单详见附件1；本承诺提供三年保修。")

    def test_attachment_label_hit_also_exposes_title_as_standard_evidence(self) -> None:
        result = _format_anchor_context(_index(), [_evidence(10, 10)])

        evidence = OrchestratorAgent._retrieval_anchor_evidence(result)

        self.assertEqual(evidence, [
            {
                "source_region": "attachments", "xml_anchor_type": "paragraph",
                "xml_anchor_id": "path:body/p10", "content": "附件1",
            },
            {
                "source_region": "attachments", "xml_anchor_type": "paragraph",
                "xml_anchor_id": "path:body/p11", "content": "设备配置清单",
            },
        ])

    def test_attachment_heading_and_title_direct_hits_are_not_duplicated(self) -> None:
        result = _format_anchor_context(_index(), [_evidence(10, 13), _evidence(11, 11)])

        evidence = OrchestratorAgent._retrieval_anchor_evidence(result)
        ids = [item["xml_anchor_id"] for item in evidence]

        self.assertEqual(ids.count("path:body/p10"), 1)
        self.assertEqual(ids.count("path:body/p11"), 1)
        self.assertEqual(ids.count("path:body/tbl12"), 1)

    def test_comparison_accepts_source_title_and_rejects_unknown_anchor(self) -> None:
        index = _index()
        index["anchor_map"].update({
            "p_41": _anchor(41, "设备配置清单详见附件1", "body/section"),
            "p_180": _anchor(180, "附件1", "attachments/att_001"),
            "p_181": _anchor(181, "设备配置清单", "attachments/att_001/title"),
            "p_182": _anchor(182, "产品明细", "attachments/att_001/title"),
        })
        index["nodes"] = [
            _section("attachments/att_001", 180, 182),
            {
                "node_id": "attachments/att_001/title", "parent_id": "attachments/att_001",
                "node_type": "visual_title", "start_anchor": "p_181",
            },
        ]
        context = _format_anchor_context(index, [
            _evidence(41, 41, "body/section"), _evidence(180, 180),
        ])
        execution = ReviewExecutionPlan(audit_mode={"comparison": True}, evidence_region="attachments")
        opinion = {
            "status": "compliant", "issues": [],
            "comparison_decisions": [{
                "edit_anchor_type": "paragraph", "edit_anchor_id": "path:body/p41",
                "evidence_anchor_type": "paragraph", "evidence_anchor_id": "path:body/p181",
                "decision": "matched", "reason": "附件标题与正文引用一致。",
            }],
        }

        self.assertEqual(OrchestratorAgent._execution_output_errors(opinion, execution, context), [])

        opinion["comparison_decisions"][0]["evidence_anchor_id"] = "path:body/p999"
        self.assertTrue(OrchestratorAgent._execution_output_errors(opinion, execution, context))

    def test_unstructured_or_conflicting_identity_stays_unknown(self) -> None:
        index = _index()
        index["anchor_map"]["p_20"]["raw_text"] = "正文提及附件2"
        result = _format_anchor_context(index, [_evidence(22, 22)])
        self.assertIn("attachment_context_id: unknown", result)
        self.assertNotIn("attachment_label: 附件2", result)

        index = _index()
        index["anchor_map"]["p_22"]["node_id"] = "attachments/att_001/title"
        result = _format_anchor_context(index, [_evidence(22, 22)])
        self.assertIn("attachment_context_id: unknown", result)

    def test_numbered_clause_is_not_used_as_attachment_title(self) -> None:
        index = _index()
        index["nodes"][3]["confidence_evidence"] = ["number_family:cn_comma"]
        index["anchor_map"]["p_21"]["raw_text"] = "一、保修义务"

        result = _format_anchor_context(index, [_evidence(22, 22)])

        self.assertIn("attachment_label: 附件2", result)
        self.assertIn("attachment_title: unknown", result)
        self.assertIn("attachment_title_xml_anchor_id: unknown", result)

    def test_inline_attachment_title_preserves_original_number(self) -> None:
        index = _index()
        index["anchor_map"]["p_20"]["raw_text"] = "附件十二：售后服务承诺书"

        result = _format_anchor_context(index, [_evidence(22, 22)])

        self.assertIn("attachment_context_id: attachments/att_002", result)
        self.assertIn("attachment_label: 附件十二", result)
        self.assertIn("attachment_title: 售后服务承诺书", result)
        self.assertIn("attachment_title_xml_anchor_id: path:body/p20", result)


if __name__ == "__main__":
    unittest.main()
