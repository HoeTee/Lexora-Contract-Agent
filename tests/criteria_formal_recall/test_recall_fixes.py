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
from agents.review_presentation import (
    build_review_presentation,
    prompt_issue_records,
    prompt_summary_records,
)
from agents.schemas import SubAgentOutput, SummaryOutput
from agents.summarizer import (
    SUMMARIZER_MAX_PROMPT_TOKENS,
    SummarizerAgent,
    conservative_message_token_upper_bound,
    normalize_summary_output,
)
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
                region_boundaries={
                    "body_content_end": {
                        "xml_anchor_type": "paragraph",
                        "xml_anchor_id": "path:body/p1",
                        "quoted_text": "合同正文",
                    }
                },
            )

            self.assertEqual(result, str(output_path))
            report = json.loads(annotation_path.read_text(encoding="utf-8"))
            self.assertEqual(report["stats"]["missing_text_fallback"], 1)
            self.assertEqual(report["skipped_count"], 0)
            self.assertEqual(report["events"][0]["status"], "anchored")
            self.assertEqual(report["events"][0]["match_strategy"], "body_content_end_missing_issue")

    def test_missing_body_boundary_does_not_fall_back_to_attachment_end(self) -> None:
        with TemporaryDirectory() as temp_dir:
            contract_path = Path(temp_dir) / "contract.docx"
            output_path = Path(temp_dir) / "reviewed.docx"
            annotation_path = Path(temp_dir) / "logs" / "annotation_events.json"
            document = Document()
            document.add_paragraph("合同正文")
            document.add_paragraph("附件中的最后一段")
            document.save(contract_path)

            DocxReportGenerator.generate_annotated_docx(
                str(contract_path),
                [{
                    "criterion_id": "TEST",
                    "criterion": "正文结尾应有空白声明",
                    "issues": [{
                        "issue_id": "TEST.1",
                        "risk_level": "low",
                        "issue_comment": "建议在正文结尾补充空白声明。",
                        "check_point": "正文结尾",
                        "anchors": [],
                    }],
                }],
                str(output_path),
                annotation_events_path=str(annotation_path),
            )

            report = json.loads(annotation_path.read_text(encoding="utf-8"))
            self.assertEqual(report["skipped_count"], 1)
            self.assertEqual(report["events"][0]["status"], "unmatched")
            self.assertEqual(report["events"][0]["match_strategy"], "body_content_end_unavailable")

    def test_summary_comment_includes_complete_summary_only_issues(self) -> None:
        text = DocxReportGenerator._build_summary_comment_text({
            "overall_comment": "总体概述。",
            "priority_comments": [],
            "opening_notices": ["注意：补充固定提示语。"],
            "global_issues": ["缺失第六、九、十四、十五条。"],
        })

        self.assertIn("合同首部需补充的提示：", text)
        self.assertIn("注意：补充固定提示语。", text)
        self.assertIn("全局审查问题：", text)
        self.assertIn("缺失第六、九、十四、十五条。", text)


class PromptAuthorityTests(unittest.TestCase):
    def test_explicit_criteria_are_authoritative_for_review_and_reflection(self) -> None:
        prompt_path = Path(__file__).resolve().parents[2] / "agents" / "prompts" / "cn_prompts.yaml"

        prompts = load_prompts(prompt_path)

        self.assertIn("优先级高于本提示词中的通用风险筛选规则", prompts["SUB_AGENT_BASE_PROMPT"])
        self.assertIn("不得质疑标准本身的必要性", prompts["REFLECTOR_SYSTEM_PROMPT"])
        self.assertIn("指定提示语", prompts["SUB_AGENT_BASE_PROMPT"])
        self.assertIn("固定文字完整性", prompts["REFLECTOR_SYSTEM_PROMPT"])
        self.assertNotIn("output_scope 枚举", prompts["SUMMARIZER_SYSTEM_PROMPT"])
        self.assertIn("不得输出 issue_decisions", prompts["SUMMARIZER_SYSTEM_PROMPT"])
        self.assertIn("不得使用“断号”", prompts["SUMMARIZER_SYSTEM_PROMPT"])
        self.assertIn("付款计划数量必须按", prompts["SUB_AGENT_BASE_PROMPT"])
        self.assertIn("付款节点计数", prompts["REFLECTOR_SYSTEM_PROMPT"])

    def test_runtime_prompt_makes_deterministic_absence_authoritative(self) -> None:
        source = inspect.getsource(OrchestratorAgent.execute_single_criterion)

        self.assertIn("确定性计数和范围说明", source)
        self.assertIn("必须输出 issues_found，不得输出 compliant", source)


class SubAgentSchemaNormalizationTests(unittest.TestCase):
    def test_word_comments_preserve_complete_text_within_new_limits(self) -> None:
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

        self.assertEqual(len(output.issues[0].issue_comment), 180)
        self.assertEqual(len(output.issues[0].anchors[0].comment_text), 120)


class ReviewPresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.results = [
            {
                "criterion_id": "C12",
                "section": "文字",
                "criterion": "检查重复词",
                "status": "issues_found",
                "issues": [{
                    "issue_id": "C12.1",
                    "risk_level": "low",
                    "issue_comment": "存在重复用词。",
                    "anchors": [{
                        "xml_anchor_type": "paragraph",
                        "xml_anchor_id": "path:body/p1",
                        "quoted_text": "因乙方原因原因",
                        "comment_text": "删除重复文字。",
                    }],
                    "reasoning": "原因重复",
                    "criterion": "检查重复词",
                    "check_point": "重复词",
                }],
            },
            {
                "criterion_id": "C15",
                "section": "语法",
                "criterion": "检查语病",
                "status": "issues_found",
                "issues": [{
                    "issue_id": "C15.1",
                    "risk_level": "low",
                    "issue_comment": "存在语病。",
                    "anchors": [{
                        "xml_anchor_type": "paragraph",
                        "xml_anchor_id": "path:body/p1",
                        "quoted_text": "因乙方原因原因",
                        "comment_text": "修改语病。",
                    }],
                    "reasoning": "原因重复",
                    "criterion": "检查语病",
                    "check_point": "语病",
                }],
            },
        ]

    def test_prompt_records_expose_stable_issue_and_anchor_keys(self) -> None:
        records = prompt_issue_records(self.results)

        self.assertEqual(records[0]["issue_key"], "C12::C12.1")
        self.assertEqual(records[0]["anchors"][0]["anchor_key"], "C12::C12.1#anchor_1")
        self.assertNotIn("_issue", records[0])

    def test_summary_prompt_excludes_anchor_and_reasoning_fields(self) -> None:
        record = prompt_summary_records(self.results)[0]

        self.assertEqual(
            set(record),
            {"section", "criterion_id", "risk_level", "issue_comment", "check_point", "quality_status"},
        )

    def test_summary_schema_rejects_removed_issue_decisions_field(self) -> None:
        with self.assertRaises(ValueError):
            SummaryOutput.model_validate({
                "overall_comment": "总结",
                "priority_comments": [],
                "issue_decisions": [],
            })

    def test_cross_criterion_duplicates_render_as_one_annotation(self) -> None:
        self.results[0]["issues"][0]["anchors"][0]["comment_text"] = (
            "删除重复的“原因”，修改为“因乙方原因”。"
        )
        self.results[1]["issues"][0]["anchors"][0]["comment_text"] = (
            "删除重复的原因，改为“因乙方原因”。"
        )

        presentation = build_review_presentation(self.results)

        self.assertEqual(len(presentation.inline_results), 1)
        issue = presentation.inline_results[0]["issues"][0]
        self.assertEqual(issue["source_issue_keys"], ["C12::C12.1", "C15::C15.1"])
        self.assertEqual(len(issue["anchors"]), 1)

    def test_ambiguous_different_advice_is_not_merged_only_by_criterion(self) -> None:
        for result in self.results:
            result["issues"][0]["anchors"][0]["quoted_text"] = "乙方向甲方发贷。"
        self.results[0]["issues"][0]["anchors"][0]["comment_text"] = (
            "若意为交付货物请改为“发货”，若为无关内容请删除。"
        )
        self.results[1]["issues"][0]["anchors"][0]["comment_text"] = (
            "结合语境应改为“乙方向甲方发货”。"
        )

        presentation = build_review_presentation(self.results)

        self.assertEqual(len(presentation.inline_results), 2)

    def test_anchorless_issue_is_summary_only_and_still_represented(self) -> None:
        self.results[0]["issues"][0]["anchors"] = []

        presentation = build_review_presentation([self.results[0]])

        self.assertEqual(presentation.inline_results, [])
        self.assertEqual(presentation.global_issues, ["存在重复用词。"])
        self.assertEqual(presentation.opening_notices, [])
        self.assertEqual(len(presentation.summarizer_results), 1)

    def test_multi_anchor_numbering_issue_is_summary_only(self) -> None:
        anchors = [
            {
                "xml_anchor_type": "paragraph",
                "xml_anchor_id": f"path:body/p{index}",
                "quoted_text": f"第{index}条",
                "comment_text": "序号断号。",
            }
            for index in range(1, 4)
        ]
        results = [{
            "criterion_id": "ORDER",
            "criterion": "检查标题顺序是否杂乱",
            "status": "issues_found",
            "issues": [{
                "issue_id": "ORDER.1",
                "risk_level": "low",
                "issue_comment": "条款序号存在多处断号。",
                "anchors": anchors,
                "reasoning": "多处断号",
                "criterion": "检查标题顺序是否杂乱",
                "check_point": "条款序号",
            }],
        }]

        presentation = build_review_presentation(results)

        self.assertEqual(presentation.inline_results, [])
        self.assertEqual(
            presentation.global_issues,
            ["条款编号多处不连续。"],
        )

    def test_numbering_issue_is_summary_only_even_with_one_anchor(self) -> None:
        result = self.results[0]
        result["criterion"] = "检查标题顺序是否杂乱"
        issue = result["issues"][0]
        issue["issue_comment"] = "合同条款序号缺失第六条。"

        presentation = build_review_presentation([result])

        self.assertEqual(presentation.inline_results, [])
        self.assertEqual(
            presentation.global_issues,
            ["合同条款序号缺失第六条。"],
        )

    def test_missing_contract_framework_is_summary_only_with_two_anchors(self) -> None:
        result = self.results[0]
        result["criterion"] = "合同框架需要具备至少以下必备标题"
        issue = result["issues"][0]
        issue["issue_comment"] = "合同框架缺失多项必备标题：保密条款、争议解决。"
        issue["anchors"].append({
            "xml_anchor_type": "paragraph",
            "xml_anchor_id": "path:body/p2",
            "quoted_text": "第四条 甲方义务",
            "comment_text": "作为结构参照。",
        })

        presentation = build_review_presentation([result])

        self.assertEqual(presentation.inline_results, [])
        self.assertEqual(
            presentation.global_issues,
            ["合同框架缺失多项必备标题：保密条款、争议解决。"],
        )

    def test_reference_only_attachment_anchor_is_removed(self) -> None:
        issue = self.results[0]["issues"][0]
        issue["anchors"].append({
            "xml_anchor_type": "paragraph",
            "xml_anchor_id": "path:body/p2",
            "quoted_text": "附件一：服务清单",
            "comment_text": "此处为附件一实际标题，作为正文引用的修正基准。",
        })

        presentation = build_review_presentation([self.results[0]])
        anchors = presentation.inline_results[0]["issues"][0]["anchors"]

        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["xml_anchor_id"], "path:body/p1")

    def test_attachment_heading_reference_is_removed_for_alternate_wording(self) -> None:
        issue = self.results[0]["issues"][0]
        issue["anchors"].append({
            "xml_anchor_type": "paragraph",
            "xml_anchor_id": "path:body/p2",
            "quoted_text": "附件一：服务清单",
            "comment_text": "此处标题为服务清单，请与正文中的附件名称保持一致。",
        })

        presentation = build_review_presentation([self.results[0]])
        anchors = presentation.inline_results[0]["issues"][0]["anchors"]

        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["xml_anchor_id"], "path:body/p1")

    def test_keyword_triggered_fixed_instruction_is_routed_to_opening_summary(self) -> None:
        results = [{
            "criterion_id": "FIXED",
            "criterion": "如果标题出现采购，就直接在合同首部写明“注意：完整固定文字”。",
            "status": "issues_found",
            "issues": [{
                "issue_id": "FIXED.1",
                "risk_level": "low",
                "issue_comment": "缺少提示语。",
                "anchors": [{
                    "xml_anchor_type": "paragraph",
                    "xml_anchor_id": "path:body/p1",
                    "quoted_text": "采购合同",
                    "comment_text": "建议补充提示语。",
                }],
                "reasoning": "标准触发",
                "criterion": "如果标题出现采购，就直接在合同首部写明“注意：完整固定文字”。",
                "check_point": "直接写明“注意：当前检查点固定文字”",
            }],
        }]

        presentation = build_review_presentation(results)
        self.assertEqual(presentation.inline_results, [])
        self.assertEqual(
            presentation.opening_notices,
            ["注意：当前检查点固定文字"],
        )

    def test_missing_content_fixed_instruction_remains_inline(self) -> None:
        result = self.results[0]
        result["criterion"] = (
            "检查联系信息，如无则在合同首部写明“注意：请补充合同基本信息”。"
        )
        issue = result["issues"][0]
        issue["check_point"] = (
            "如无联系地址，在合同首部写明“注意：请补充合同基本信息”"
        )

        presentation = build_review_presentation([result])

        self.assertEqual(len(presentation.inline_results), 1)
        self.assertEqual(presentation.opening_notices, [])

    def test_duration_issue_is_routed_to_summary(self) -> None:
        self.results[0]["issues"][0]["check_point"] = "补充合同履行期限"

        presentation = build_review_presentation([self.results[0]])

        self.assertEqual(presentation.inline_results, [])
        self.assertEqual(presentation.global_issues, ["存在重复用词。"])

    def test_nested_prescribed_text_is_complete_in_summary(self) -> None:
        issue = self.results[0]["issues"][0]
        issue["anchors"] = []
        issue["check_point"] = (
            "如无上述内容，在正文结尾处写入“注意：补充“以下为合同签署栏”"
            "或“以下无正文””"
        )

        presentation = build_review_presentation([self.results[0]])

        self.assertEqual(
            presentation.global_issues,
            ["注意：补充“以下为合同签署栏”或“以下无正文”"],
        )


class SummarizerPromptBudgetTests(unittest.TestCase):
    def test_summary_output_replaces_internal_numbering_jargon(self) -> None:
        summary = normalize_summary_output({
            "overall_comment": "合同条款断号严重。",
            "priority_comments": ["修正序号断号。"],
        })

        self.assertEqual(summary["overall_comment"], "合同条款编号多处不连续。")
        self.assertEqual(summary["priority_comments"], ["修正编号不连续。"])

    def test_conservative_bound_accepts_small_prompt(self) -> None:
        bound = conservative_message_token_upper_bound([
            {"role": "system", "content": "总结合同"},
            {"role": "user", "content": "问题列表"},
        ])

        self.assertLess(bound, SUMMARIZER_MAX_PROMPT_TOKENS)

    def test_conservative_bound_rejects_prompt_above_hard_limit(self) -> None:
        messages = [
            {"role": "user", "content": "x" * SUMMARIZER_MAX_PROMPT_TOKENS},
        ]
        bound = conservative_message_token_upper_bound(messages)

        self.assertGreater(bound, SUMMARIZER_MAX_PROMPT_TOKENS)
        summarizer = SummarizerAgent.__new__(SummarizerAgent)
        summarizer.messages = messages
        summarizer.MAX_CONTEXT_TOKENS = 254_000
        summarizer.debug = False
        with self.assertRaisesRegex(ValueError, "90000-token safety limit"):
            summarizer._check_context_limits()


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
            _should_use_fallback(plan, fallback_enabled=True)
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
            _should_use_fallback(plan, fallback_enabled=True)
        )


if __name__ == "__main__":
    unittest.main()
