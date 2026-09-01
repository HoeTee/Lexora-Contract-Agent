from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from tools.docxindex.retrieval.config import RetrievalConfig, _migrate_legacy_value
from tools.docxindex.retrieval.route import RouteConfig
from tools.docxindex.retriever import DocxIndexRetriever, _merge_semantic_matches
from tools.docxindex.schema.index import DocumentIndex
from tools.docxindex.schema.nodes import DocumentNode


def _candidate(index: int, *, score: float | None = None) -> dict[str, object]:
    item: dict[str, object] = {
        "node_id": f"body/sec_{index:03d}",
        "title": f"第{index}条",
        "reason": "结构命中",
    }
    if score is not None:
        item["score"] = score
    return item


class SemanticSupplementTests(unittest.TestCase):
    def _retriever(self) -> DocxIndexRetriever:
        retriever = DocxIndexRetriever.__new__(DocxIndexRetriever)
        retriever.retrieval_config = RetrievalConfig.model_validate(
            {
                "vector": {"enabled": True, "score_threshold": 0.45, "probe_limit": 20},
                "semantic": {"structure_candidate_limit": 12, "max_selected_nodes": 10},
                "rerank": {"enabled": False},
            }
        )
        retriever.llm_client = object()
        retriever.embedding_client = object()
        retriever._vector_index = object()
        return retriever

    def test_vector_is_not_called_when_structure_has_ten_nodes(self) -> None:
        retriever = self._retriever()
        structure = [_candidate(index) for index in range(10)]
        with (
            patch("tools.docxindex.retriever.llm_query", return_value=structure),
            patch("tools.docxindex.retriever.vector_search") as vector_search,
            patch("tools.docxindex.retriever._route_event"),
        ):
            result = retriever._semantic_matches({"nodes": []}, "金额", None, scope="global")

        self.assertEqual(len(result), 10)
        vector_search.assert_not_called()

    def test_vector_only_fills_missing_unique_nodes(self) -> None:
        retriever = self._retriever()
        structure = [_candidate(index) for index in range(8)]
        vector = [
            _candidate(0, score=0.99),
            _candidate(8, score=0.90),
            _candidate(9, score=0.80),
            _candidate(10, score=0.70),
        ]
        with (
            patch("tools.docxindex.retriever.llm_query", return_value=structure),
            patch("tools.docxindex.retriever.vector_search", return_value=vector) as vector_search,
            patch("tools.docxindex.retriever._route_event"),
        ):
            result = retriever._semantic_matches({"nodes": []}, "金额", None, scope="global")

        self.assertEqual([item["node_id"] for item in result[-2:]], ["body/sec_008", "body/sec_009"])
        self.assertEqual(len(result), 10)
        self.assertEqual(result[0]["sources"], ["structure", "vector"])
        self.assertEqual(vector_search.call_args.kwargs["top_k"], 20)
        self.assertEqual(vector_search.call_args.kwargs["score_threshold"], 0.45)

    def test_merge_reports_duplicate_and_added_counts(self) -> None:
        merged, duplicates, added = _merge_semantic_matches(
            [_candidate(1), _candidate(2)],
            [_candidate(2, score=0.9), _candidate(3, score=0.8)],
            max_unique_nodes=3,
        )

        self.assertEqual([item["node_id"] for item in merged], ["body/sec_001", "body/sec_002", "body/sec_003"])
        self.assertEqual(duplicates, 1)
        self.assertEqual(added, 1)


class RetrievalSchemaTests(unittest.TestCase):
    def test_project_config_uses_one_vector_threshold_and_ten_selected_nodes(self) -> None:
        config = RetrievalConfig.from_sources(Path(__file__).resolve().parents[2] / "config.yaml")

        self.assertEqual(config.vector.score_threshold, 0.45)
        self.assertEqual(config.vector.probe_limit, 20)
        self.assertEqual(config.semantic.structure_candidate_limit, 12)
        self.assertEqual(config.semantic.max_selected_nodes, 10)

    def test_legacy_retrieval_setting_migrates_but_conflict_is_rejected(self) -> None:
        target: dict[str, object] = {}
        _migrate_legacy_value({"max_nodes": 5}, "max_nodes", target, "max_selected_nodes")
        self.assertEqual(target, {"max_selected_nodes": 5})

        with self.assertRaisesRegex(ValueError, "conflicting retrieval settings"):
            _migrate_legacy_value(
                {"max_nodes": 5},
                "max_nodes",
                {"max_selected_nodes": 10},
                "max_selected_nodes",
            )

    def test_route_config_no_longer_owns_retrieval_limits(self) -> None:
        self.assertEqual(
            set(RouteConfig.model_fields),
            {"criteria", "expected_criteria_count", "fallback_enabled", "methods"},
        )

    def test_storage_view_only_serializes_nodes_child_ids(self) -> None:
        child = DocumentNode(
            node_id="body/sec_001",
            parent_id="body",
            node_type="section",
            title="合同金额",
            start_anchor="p_0001",
            end_anchor="p_0001",
        )
        root = DocumentNode(
            node_id="body",
            node_type="body",
            title="正文",
            start_anchor="p_0001",
            end_anchor="p_0001",
            children=[child],
        )

        stored = root.storage_view()

        self.assertEqual(stored["nodes"], ["body/sec_001"])
        self.assertNotIn("children", stored)
        self.assertEqual(DocumentIndex.model_fields["schema_version"].default, "docx-index-v5")
        self.assertNotIn("root_nodes", DocumentIndex.model_fields)


if __name__ == "__main__":
    unittest.main()
