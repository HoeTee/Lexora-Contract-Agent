from __future__ import annotations

import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docx import Document
from docx.oxml import OxmlElement

from agents.orchestrator import OrchestratorAgent
from agents.prompts.cn_prompts import load_prompts
from agents.schemas import SubAgentOutput
from tests.criteria_formal_recall.prepare_cases import select_contract_title_group
from tools.document.docx_anchor_index import build_docx_anchor_nodes
from tools.document.reporting.docx_report import DocxReportGenerator
from tools.docxindex.llm.schemas import RouteResponse
from tools.docxindex.retriever import _should_use_fallback


class AnchorEvidenceValidationTests(unittest.TestCase):
    def test_wrong_model_anchor_id_is_repaired_from_unique_retrieval_evidence(self) -> None:
        context = (
            "xml_anchor_type: paragraph\nxml_anchor_id: path:body/p8\n内容：\n合同编号：A\n\n---\n\n"
            "xml_anchor_type: paragraph\nxml_anchor_id: path:body/p15\n内容：\n甲方：浙江农村商业联合银行股份有限公司"
        )
        output = {
            "issues": [{
                "anchors": [{
                    "xml_anchor_type": "paragraph",
                    "xml_anchor_id": "path:body/p8",
                    "quoted_text": "甲方：浙江农村商业联合银行股份有限公司",
                    "comment_text": "测试",
                }],
            }],
        }

        stats = OrchestratorAgent()._validate_and_repair_anchors(output, context)

        self.assertEqual(stats, {"valid": 0, "repaired": 1, "dropped": 0})
        self.assertEqual(output["issues"][0]["anchors"][0]["xml_anchor_id"], "path:body/p15")

    def test_unprovable_model_anchor_is_removed_before_docx_write(self) -> None:
        context = "xml_anchor_type: paragraph\nxml_anchor_id: path:body/p1\n内容：\n合同正文"
        output = {
            "issues": [{
                "anchors": [{
                    "xml_anchor_type": "paragraph",
                    "xml_anchor_id": "path:body/p1",
                    "quoted_text": "模型编造的原文",
                    "comment_text": "测试",
                }],
            }],
        }

        stats = OrchestratorAgent()._validate_and_repair_anchors(output, context)

        self.assertEqual(stats, {"valid": 0, "repaired": 0, "dropped": 1})
        self.assertEqual(output["issues"][0]["anchors"], [])


class DocxAnchorRegressionTests(unittest.TestCase):
    def test_anchor_paths_keep_real_xml_child_indexes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            contract_path = Path(temp_dir) / "contract.docx"
            document = Document()
            first = document.add_paragraph("第一段")
            document.add_paragraph("目标条款")
            first._p.addnext(OxmlElement("w:sdt"))
            document.save(contract_path)

            nodes = build_docx_anchor_nodes(str(contract_path))
            target = next(node for node in nodes if node.text == "目标条款")
            self.assertEqual(target.anchor_id, "path:body/p3")

            anchor = DocxReportGenerator._find_text_range_anchor_in_xml_anchor(
                Document(contract_path),
                "paragraph",
                target.anchor_id,
                "目标条款",
            )
            self.assertIsNotNone(anchor)
            self.assertEqual(anchor.path, "body/p3")

    def test_anchor_matching_tolerates_spacing_variants_within_same_anchor(self) -> None:
        with TemporaryDirectory() as temp_dir:
            contract_path = Path(temp_dir) / "contract.docx"
            document = Document()
            document.add_paragraph("甲方应在十\u00a0个工作日内付款")
            document.save(contract_path)

            anchor = DocxReportGenerator._find_text_range_anchor_in_xml_anchor(
                Document(contract_path),
                "paragraph",
                "path:body/p1",
                "甲方应在十 个工作日内付款",
            )
            self.assertIsNotNone(anchor)
            self.assertEqual(anchor.match.matched_text, "甲方应在十\u00a0个工作日内付款")

    def test_anchorless_issue_is_written_and_diagnosed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            contract_path = Path(temp_dir) / "contract.docx"
            output_path = Path(temp_dir) / "reviewed.docx"
            annotation_path = Path(temp_dir) / "logs" / "annotation_events.json"
            document = Document()
            document.add_paragraph("合同正文")
            document.add_paragraph("（以下无正文）")
            document.save(contract_path)

            result = DocxReportGenerator.generate_annotated_docx(
                str(contract_path),
                [{
                    "criterion_id": "TEST",
                    "criterion": "正文最后应有空白声明",
                    "issues": [{
                        "issue_id": "TEST.1",
                        "risk_level": "low",
                        "issue_comment": "建议在正文最后补充空白声明。",
                        "check_point": "正文最后",
                        "anchors": [],
                    }],
                }],
                str(output_path),
                annotation_events_path=str(annotation_path),
            )

            self.assertEqual(result, str(output_path))
            report = json.loads(annotation_path.read_text(encoding="utf-8"))
            self.assertEqual(report["stats"]["missing_text_fallback"], 1)
            self.assertEqual(report["skipped_count"], 0)
            self.assertEqual(report["events"][0]["status"], "anchored")
            self.assertEqual(report["events"][0]["match_strategy"], "document_end_missing_issue")


class PromptAuthorityTests(unittest.TestCase):
    def test_explicit_criteria_are_authoritative_for_review_and_reflection(self) -> None:
        prompt_path = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "cn_prompts.yaml"

        prompts = load_prompts(prompt_path)

        self.assertIn("优先级高于本提示词中的通用风险筛选规则", prompts["SUB_AGENT_BASE_PROMPT"])
        self.assertIn("不得质疑标准本身的必要性", prompts["REFLECTOR_SYSTEM_PROMPT"])

    def test_runtime_prompt_makes_deterministic_absence_authoritative(self) -> None:
        source = inspect.getsource(OrchestratorAgent.execute_single_criterion)

        self.assertIn("确定性计数和范围说明", source)
        self.assertIn("必须输出 issues_found，不得输出 compliant", source)


class SubAgentSchemaNormalizationTests(unittest.TestCase):
    def test_word_comment_lengths_are_deterministically_truncated(self) -> None:
        output = SubAgentOutput.model_validate({
            "status": "issues_found",
            "issues": [{
                "issue_id": "C11.1",
                "risk_level": "medium",
                "issue_comment": "问" * 180,
                "anchors": [{
                    "xml_anchor_type": "paragraph",
                    "xml_anchor_id": "path:body/p1",
                    "quoted_text": "合同原文",
                    "comment_text": "改" * 120,
                }],
                "reasoning": "正文与附件日期不一致",
                "criterion": "检查正文与附件一致性",
                "check_point": "日期必须一致",
            }],
        })

        self.assertEqual(len(output.issues[0].issue_comment), 160)
        self.assertEqual(len(output.issues[0].anchors[0].comment_text), 100)


class PreparedContractValidationTests(unittest.TestCase):
    def test_contract_title_selection_stays_in_early_frontmatter(self) -> None:
        values = [
            "甲方公司",
            "与",
            "乙方公司",
            "人行支付系统重构研发合同",
            "合同号：ABC",
            "正文中的合同总价",
            "合同金额及支付方式",
        ]

        selected = select_contract_title_group(
            values,
            tuple((index,) for index in range(len(values))),
            frontmatter_end=5,
        )

        self.assertEqual(selected, (3,))


class RetrievalFallbackTests(unittest.TestCase):
    def test_rule_uses_semantic_fallback_only_when_plan_requests_it(self) -> None:
        plan = RouteResponse.model_validate({
            "steps": [{"method": "rule", "terms": ["人民法院"]}],
            "fallback": True,
        })

        self.assertTrue(
            _should_use_fallback(plan, [{"node_id": "body/dispute"}], fallback_enabled=True)
        )

    def test_region_cannot_request_global_fallback(self) -> None:
        with self.assertRaises(ValueError):
            RouteResponse.model_validate({
                "steps": [{"method": "region", "regions": ["body"]}],
                "fallback": True,
            })

    def test_full_scan_does_not_duplicate_semantic_fallback(self) -> None:
        plan = RouteResponse.model_validate({
            "steps": [{"method": "scan"}],
            "fallback": False,
        })

        self.assertFalse(
            _should_use_fallback(plan, [{"node_id": "scan/batch_001"}], fallback_enabled=True)
        )


if __name__ == "__main__":
    unittest.main()
