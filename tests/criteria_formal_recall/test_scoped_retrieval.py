from __future__ import annotations

import re
from pathlib import Path
from threading import Lock
from tempfile import TemporaryDirectory
import unittest

from loggers.api_event_logger import _LOGGERS
from loggers.model_event_context import reset_model_event_path, set_model_event_path
from tools.docxindex.llm.schemas import RouteStep, ScanResponse
from tools.docxindex.indexing.config import IndexingConfig
from tools.docxindex.retrieval.llm_query import _compact_structure
from tools.docxindex.retrieval.scan import scan_matches
from tools.docxindex.retrieval.rule import rule_matches
from tools.docxindex.retriever import _interleave_groups, _region_node_ids, _route_event


class ScopedRegionTests(unittest.TestCase):
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
            token = set_model_event_path(path)
            try:
                _route_event("step", method="region", regions=["body"], node_ids=["body/a"])
            finally:
                reset_model_event_path(token)
            content = path.read_text(encoding="utf-8")
            logger = _LOGGERS.pop(str(path.resolve()))
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()

        self.assertIn("docxindex_route", content)
        self.assertIn("route_event=step", content)
        self.assertIn('regions=["body"]', content)


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


class _ScanClient:
    def __init__(self) -> None:
        self.call_count = 0
        self._lock = Lock()

    def complete_model(self, prompt: str, schema: type[ScanResponse]) -> ScanResponse:
        anchor_id = re.search(r'"anchor_id": "([^"]+)"', prompt).group(1)  # type: ignore[union-attr]
        with self._lock:
            self.call_count += 1
        return schema.model_validate({
            "findings": [{"anchor_id": anchor_id, "reason": "测试发现"}],
        })


if __name__ == "__main__":
    unittest.main()
