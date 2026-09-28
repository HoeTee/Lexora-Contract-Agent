from __future__ import annotations

import re
import json
from pathlib import Path
from threading import Lock
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from loggers.call_log import reset_call_context, set_call_context
from endpoints.review.task_store import docxindex_state_dir
from tools.docxindex.llm.schemas import RouteStep, ScanResponse
from tools.docxindex.indexing.config import IndexingConfig
from tools.docxindex.detection.regions import find_tail_start
from tools.docxindex.retrieval.config import RetrievalConfig
from tools.docxindex.schema import BodyItem
from tools.docxindex.retrieval.llm_query import _compact_structure
from tools.docxindex.retrieval.scan import scan_matches
from tools.docxindex.retrieval.rule import rule_matches
from tools.docxindex.retrieval.title import title_matches
from tools.docxindex.retriever import (
    DocxIndexRetriever,
    _complete_scope_matches,
    _deduplicate_matches,
    _interleave_groups,
    _region_node_ids,
    _region_root_ids,
    _route_event,
)


class ScopedRegionTests(unittest.TestCase):
    def test_docxindex_artifacts_follow_api_and_web_task_partition(self) -> None:
        api_path = docxindex_state_dir("api", "20260910-120000-a001")
        web_path = docxindex_state_dir("web:tenant-001", "20260910-120001-a002")

        self.assertEqual(api_path.parts[-4:], ("api", "20260910-120000-a001", "state", "docxindex"))
        self.assertEqual(web_path.parts[-5:], ("web", "tenant-001", "20260910-120001-a002", "state", "docxindex"))

    def test_tail_starts_at_earliest_signature_evidence(self) -> None:
        def item(index: int, kind: str, text: str) -> BodyItem:
            return BodyItem(
                kind=kind,
                anchor=f"p_{index:04d}",
                body_child_index=index,
                text=text,
                xml_path=f"/w:document/w:body/*[{index + 1}]",
            )

        items = [item(index, "p", "正文") for index in range(25)]
        items[20] = item(20, "p", "三、甲方：浙江农商联合银行股份有限公司（盖章）")
        items[22] = item(22, "tbl", "甲方（盖章） 乙方（盖章）")

        self.assertEqual(find_tail_start(items, 0, len(items)), 20)

    def test_online_index_enrichment_is_enabled_by_default(self) -> None:
        enrichment = IndexingConfig().enrichment

        self.assertTrue(enrichment.llm_expand)
        self.assertTrue(enrichment.llm_summary)
        self.assertTrue(enrichment.llm_attachment_hierarchy)

    def test_region_ids_include_only_selected_region_descendants(self) -> None:
        index = {
            "top_regions": {"body": "body", "attachments": "attachments"},
            "nodes": [
                {"node_id": "body"},
                {"node_id": "body/payment"},
                {"node_id": "attachments"},
                {"node_id": "attachments/schedule"},
            ],
        }

        self.assertEqual(_region_node_ids(index, ["body"]), {"body", "body/payment"})

    def test_compact_structure_keeps_path_to_allowed_descendant(self) -> None:
        structure = [{
            "node_id": "body",
            "title": "正文",
            "nodes": [
                {"node_id": "body/payment", "title": "付款"},
                {"node_id": "body/dispute", "title": "争议"},
            ],
        }]

        compact = _compact_structure(structure, allowed_node_ids={"body/payment"})

        self.assertEqual(compact[0]["node_id"], "body")
        self.assertEqual([node["node_id"] for node in compact[0]["nodes"]], ["body/payment"])

    def test_interleave_preserves_first_result_from_each_region_step(self) -> None:
        groups = [
            [{"node_id": "body/a"}, {"node_id": "body/b"}],
            [{"node_id": "attachments/a"}, {"node_id": "attachments/b"}],
        ]

        matches = _interleave_groups(groups, limit=2)

        self.assertEqual([item["node_id"] for item in matches], ["body/a", "attachments/a"])

    def test_region_heading_locator_selects_section_root(self) -> None:
        index = {
            "nodes": [
                {"node_id": "body/payment", "node_type": "section", "title": "合同金额及支付方式"},
                {"node_id": "body/payment/detail", "node_type": "section", "title": "付款条件"},
            ]
        }
        step = RouteStep(method="region", anchor_terms=["支付方式"], coverage="complete")

        self.assertEqual(_region_root_ids(index, step), ["body/payment"])

    def test_title_route_returns_only_first_level_business_titles(self) -> None:
        index = {
            "nodes": [
                {"node_id": "body/sec", "title": "支付方式", "node_type": "section", "start_index": 1},
                {"node_id": "body/sec/detail", "title": "付款条件", "node_type": "section", "start_index": 2},
            ],
            "structure_tree": [{
                "node_id": "body",
                "title": "正文",
                "node_type": "body",
                "nodes": [{
                    "node_id": "body/sec",
                    "title": "支付方式",
                    "node_type": "section",
                    "nodes": [{
                        "node_id": "body/sec/detail",
                        "title": "付款条件",
                        "node_type": "section",
                        "nodes": [],
                    }],
                }],
            }],
        }

        matches = title_matches(index, RouteStep(method="title", all_titles=True))

        self.assertEqual([item["node_id"] for item in matches], ["body/sec"])

    def test_attachment_title_route_returns_attachment_labels_and_names(self) -> None:
        index = {
            "nodes": [
                {"node_id": "attachments/att_001", "title": "附件1", "node_type": "attachment_section", "start_index": 20},
                {"node_id": "attachments/att_001/visual_title_001", "title": "设备配置清单", "node_type": "visual_title", "start_index": 21},
                {"node_id": "attachments/att_001/heading_002", "title": "一、设备明细", "node_type": "heading", "start_index": 22},
            ],
            "structure_tree": [{
                "node_id": "attachments", "title": "附件", "node_type": "attachments", "nodes": [{
                    "node_id": "attachments/att_001", "title": "附件1", "node_type": "attachment_section", "nodes": [
                        {"node_id": "attachments/att_001/visual_title_001", "title": "设备配置清单", "node_type": "visual_title", "nodes": []},
                        {"node_id": "attachments/att_001/heading_002", "title": "一、设备明细", "node_type": "heading", "nodes": []},
                    ],
                }],
            }],
        }

        matches = title_matches(index, RouteStep(method="title", regions=["attachments"], all_titles=True))

        self.assertEqual(
            [item["node_id"] for item in matches],
            ["attachments/att_001", "attachments/att_001/visual_title_001"],
        )

    def test_complete_scope_keeps_the_root_full_text_range(self) -> None:
        index = {"nodes": [{
            "node_id": "body/payment",
            "title": "支付方式",
            "start_index": 10,
            "end_index": 30,
            "start_anchor": "p_0010",
            "end_anchor": "p_0030",
        }]}

        matches = _complete_scope_matches(index, ["body/payment"])

        self.assertEqual(len(matches), 1)
        self.assertEqual((matches[0]["start_index"], matches[0]["end_index"]), (10, 30))

    def test_complete_empty_region_expands_nonempty_attachment_children(self) -> None:
        index = {"nodes": [
            {"node_id": "attachments", "title": "附件", "text": "", "nodes": ["attachments/att_001"]},
            {
                "node_id": "attachments/att_001",
                "title": "信息化建设保密协议",
                "text": "附件2 信息化建设保密协议",
                "start_index": 20,
                "end_index": 30,
            },
        ]}

        matches = _complete_scope_matches(index, ["attachments"])

        self.assertEqual([item["node_id"] for item in matches], ["attachments/att_001"])
        self.assertEqual(matches[0]["title"], "信息化建设保密协议")

    def test_duplicate_title_does_not_replace_wider_section_range(self) -> None:
        title = {
            "node_id": "body/payment",
            "start_index": 10,
            "end_index": 10,
            "text_override": "支付方式",
            "sources": ["title"],
        }
        section = {
            "node_id": "body/payment",
            "start_index": 10,
            "end_index": 30,
            "sources": ["region"],
        }

        matches = _deduplicate_matches([title, section])

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["end_index"], 30)
        self.assertNotIn("text_override", matches[0])
        self.assertEqual(matches[0]["sources"], ["region", "title"])

    def test_complete_region_metadata_returns_every_token_page(self) -> None:
        retriever = DocxIndexRetriever.__new__(DocxIndexRetriever)
        retriever._index = {
            "top_regions": {"frontmatter": "frontmatter"},
            "nodes": [{
                "node_id": "frontmatter",
                "title": "合同首部",
                "node_type": "frontmatter",
                "text": "甲" * 1200 + "乙" * 1200,
                "start_index": 1,
                "end_index": 2,
                "start_anchor": "p_0001",
                "end_anchor": "p_0002",
                "token_estimate": 1200,
            }],
            "anchor_map": {
                "p_0001": {"body_child_index": 1, "text": "甲" * 1200, "raw_text": "甲" * 1200},
                "p_0002": {"body_child_index": 2, "text": "乙" * 1200, "raw_text": "乙" * 1200},
            },
        }
        retriever._vector_index = None
        retriever.retrieval_config = RetrievalConfig(output_tokens=500)
        retriever.route_config = SimpleNamespace(fallback_enabled=True)

        result = json.loads(retriever._search_sync(
            "检查主体",
            {"steps": [{"method": "region", "regions": ["frontmatter"], "coverage": "complete"}]},
            "C10",
            True,
        ))

        self.assertGreater(result["content_parts"], 1)
        self.assertEqual(len(result["contexts"]), result["content_parts"])
        self.assertIn("甲", result["contexts"][0])
        self.assertIn("乙", result["contexts"][-1])

    def test_rule_prioritizes_anchor_matching_more_planned_terms(self) -> None:
        index = {
            "nodes": [{"node_id": "body/amount", "title": "金额"}],
            "anchor_map": {
                "p_0001": {
                    "node_id": "body/amount",
                    "body_child_index": 1,
                    "text": "合同金额待填写",
                },
                "p_0002": {
                    "node_id": "body/amount",
                    "body_child_index": 2,
                    "text": "合同金额小写88元，合同金额大写人民币捌拾玖元",
                },
            },
        }
        step = RouteStep(method="rule", terms=["合同金额", "大写", "人民币"])

        matches = rule_matches(index, step)

        self.assertEqual(matches[0]["start_anchor"], "p_0002")
        self.assertEqual(matches[0]["rule_match_count"], 3)

    def test_route_event_is_persisted_without_contract_text(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.log"
            token = set_call_context(path, operation_id="test-operation", criterion_id="H12")
            try:
                _route_event("step", method="region", regions=["body"], node_ids=["body/a"])
            finally:
                reset_call_context(token)
            files = list((Path(temp_dir) / "route_events").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0].name, r"^H12__step__\d{8}T\d{9}Z__[0-9a-f]{32}\.json$")
            content = files[0].read_text(encoding="utf-8")

        self.assertIn('"event": "step"', content)
        self.assertIn('"criterion_id": "H12"', content)
        self.assertIn('"regions": [', content)


class FullScanTests(unittest.TestCase):
    def test_scan_calls_llm_for_every_batch_and_returns_anchor_findings(self) -> None:
        owner = {
            "node_id": "body/sec",
            "title": "正文",
            "start_index": 1,
            "end_index": 2,
            "start_anchor": "p_0001",
            "end_anchor": "p_0002",
        }
        index = {
            "nodes": [owner],
            "anchor_map": {
                "p_0001": {"node_id": "body/sec", "body_child_index": 1, "raw_text": "甲" * 20},
                "p_0002": {"node_id": "body/sec", "body_child_index": 2, "raw_text": "乙" * 20},
            },
        }
        client = _ScanClient()

        matches = scan_matches(index, "检查错别字", client, batch_tokens=1, concurrency=2)

        self.assertEqual(client.call_count, 2)
        self.assertEqual({item["start_anchor"] for item in matches}, {"p_0001", "p_0002"})
        self.assertTrue(all(item["text_override"] for item in matches))

    def test_scan_can_be_limited_to_body_nodes_with_candidate_instruction(self) -> None:
        body_owner = {"node_id": "body/sec", "title": "正文"}
        attachment_owner = {"node_id": "attachments/att_001", "title": "附件一"}
        index = {
            "nodes": [body_owner, attachment_owner],
            "anchor_map": {
                "p_0001": {
                    "node_id": "body/sec",
                    "body_child_index": 1,
                    "raw_text": "详见附件一《产品清单》",
                },
                "p_0002": {
                    "node_id": "attachments/att_001",
                    "body_child_index": 2,
                    "raw_text": "附件一：服务清单",
                },
            },
        }
        client = _ScanClient()

        matches = scan_matches(
            index,
            "检查附件名称",
            client,
            allowed_node_ids={"body/sec"},
            candidate_instruction="提取正文附件引用",
        )

        self.assertEqual([item["start_anchor"] for item in matches], ["p_0001"])
        self.assertIn("候选提取任务", client.prompts[0])
        self.assertIn("提取正文附件引用", client.prompts[0])


    def test_scan_retries_only_the_failed_batch(self) -> None:
        owner = {"node_id": "body/sec", "title": "正文"}
        index = {
            "nodes": [owner],
            "anchor_map": {
                "p_0001": {"node_id": "body/sec", "body_child_index": 1, "raw_text": "甲" * 20},
                "p_0002": {"node_id": "body/sec", "body_child_index": 2, "raw_text": "乙" * 20},
            },
        }
        client = _RetryingScanClient()

        matches = scan_matches(index, "检查错别字", client, batch_tokens=1, concurrency=1)

        self.assertEqual(client.calls_by_anchor["p_0001"], 2)
        self.assertEqual(client.calls_by_anchor["p_0002"], 1)
        self.assertEqual({item["start_anchor"] for item in matches}, {"p_0001", "p_0002"})


class _ScanClient:
    def __init__(self) -> None:
        self.call_count = 0
        self._lock = Lock()
        self.prompts: list[str] = []

    def complete_model(self, prompt: str, schema: type[ScanResponse]) -> ScanResponse:
        anchor_id = re.search(r'"anchor_id": "([^"]+)"', prompt).group(1)  # type: ignore[union-attr]
        with self._lock:
            self.call_count += 1
            self.prompts.append(prompt)
        return schema.model_validate({
            "findings": [{"anchor_id": anchor_id, "reason": "测试发现"}],
        })


class _RetryingScanClient:
    def __init__(self) -> None:
        self.calls_by_anchor: dict[str, int] = {}

    def complete_model(self, prompt: str, schema: type[ScanResponse]) -> ScanResponse:
        anchor_id = re.search(r'"anchor_id": "([^"]+)"', prompt).group(1)  # type: ignore[union-attr]
        self.calls_by_anchor[anchor_id] = self.calls_by_anchor.get(anchor_id, 0) + 1
        if anchor_id == "p_0001" and self.calls_by_anchor[anchor_id] == 1:
            raise TimeoutError("simulated scan timeout")
        return schema.model_validate({
            "findings": [{"anchor_id": anchor_id, "reason": "测试发现"}],
        })


if __name__ == "__main__":
    unittest.main()
