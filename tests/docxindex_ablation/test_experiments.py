"""Network-free safety checks for the experiment interventions."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import zipfile

from pydantic import ValidationError

from tools.docxindex import retriever as retrieval
from tools.docxindex.llm.schemas import RerankResponse
from tools.docxindex.retrieval import content
from tests.docxindex_ablation.run import anchor_texts
from tests.docxindex_ablation.judge import new_ai_comments, validated_schema
from tests.docxindex_ablation.variants import (
    IndexedFormatter,
    ORIGINAL_RERANK,
    RERANK_MODULE,
    field_removal,
    skip_single_rerank,
    treatment,
)


def fixture():
    return {
        "nodes": [
            {"node_id": "body", "text": "甲乙", "title": "正文", "start_index": 1, "end_index": 2},
            {"node_id": "body/a", "text": "甲", "title": "甲", "start_index": 1, "end_index": 1},
        ],
        "anchor_map": {
            "late": {"body_child_index": 2, "text": "乙", "type": "p"},
            "missing": {"body_child_index": None, "text": "不输出"},
            "early": {"body_child_index": 1, "raw_text": "甲", "type": "p"},
            "duplicate": {"body_child_index": 1, "text": "不重复"},
        },
    }


class ExperimentTests(unittest.TestCase):
    def test_summary_is_visible_evidence_but_old_and_orphan_comments_are_not(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        with TemporaryDirectory() as temporary:
            paths = [Path(temporary) / name for name in ("before.docx", "after.docx")]
            for path, ids in zip(paths, (["1"], ["1", "2", "3"]), strict=True):
                body = "".join(
                    f'<w:p><w:commentRangeStart w:id="{key}"/><w:r><w:t>原文{key}</w:t></w:r><w:commentRangeEnd w:id="{key}"/></w:p>'
                    for key in ids
                )
                definitions = "".join(
                    f'<w:comment w:id="{key}" w:author="{author}"><w:p><w:r><w:t>批注{key}</w:t></w:r></w:p></w:comment>'
                    for key, author in [
                        ("1", "AI 条款审查"),
                        ("2", "AI 审查总结"),
                        ("3", "AI 条款审查"),
                        ("4", "AI 条款审查"),
                    ]
                )
                with zipfile.ZipFile(path, "w") as package:
                    package.writestr(
                        "word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
                    )
                    package.writestr("word/comments.xml", f'<w:comments xmlns:w="{ns}">{definitions}</w:comments>')
            visible = new_ai_comments(paths[1], paths[0])
            self.assertEqual([r["comment_id"] for r in visible], ["2", "3"])
            self.assertEqual(visible[0]["anchored_text"], "原文2")

    def test_judge_rejects_missing_ids_and_unsupported_recall(self):
        schema = validated_schema({"1": {"text": "批注"}})
        value = {
            "recall": [
                {"criterion_id": i, "expected_case": "valid", "recalled": False, "comment_ids": [], "reason": "reason"}
                for i in range(1, 19)
            ],
            "comments": [{"comment_id": "1", "criterion_ids": [1], "verdict": "correct", "reason": "reason"}],
        }
        schema.model_validate(value)
        value["recall"][0]["recalled"] = True
        with self.assertRaises(ValidationError):
            schema.model_validate(value)
        value["recall"][0]["comment_ids"] = ["missing"]
        with self.assertRaises(ValidationError):
            schema.model_validate(value)
        value["recall"][0]["comment_ids"] = ["1"]
        schema.model_validate(value)
        value["recall"][-1]["criterion_id"] = 1
        with self.assertRaises(ValidationError):
            schema.model_validate(value)

    def test_indexed_formatter_preserves_raw_text_order_and_overlap(self):
        idx = fixture()
        for nodes in (idx["nodes"], list(reversed(idx["nodes"])), [], [{"start_index": None}]):
            self.assertEqual(retrieval._format_anchor_context(idx, nodes), IndexedFormatter(idx)(idx, nodes))
        parsed = anchor_texts(IndexedFormatter(idx)(idx, idx["nodes"]))
        self.assertEqual(parsed, {"path:body/p1": "甲", "path:body/p2": "乙"})

    def test_singleton_skip_preserves_rendered_evidence(self):
        idx = fixture()
        candidates = [{"node_id": "body/a", "sources": ["structure"]}]
        for response in ({"nodes": []}, {"nodes": [{"node_id": "body/a", "score": 0.9, "reason": "match"}]}):
            client = SimpleNamespace(
                settings=SimpleNamespace(model="fake"),
                complete_model=lambda *args, response=response: RerankResponse.model_validate(response),
            )
            before = ORIGINAL_RERANK(idx, "query", candidates, client, 1000, None)
            after = skip_single_rerank(idx, "query", candidates, client, 1000, None)
            self.assertEqual(
                content.build_content_context(idx, before, 1000), content.build_content_context(idx, after, 1000)
            )

    def test_treatments_restore_original_functions(self):
        idx = fixture()
        original = content.get_node
        with treatment("E05", idx):
            self.assertEqual(content.get_node(idx, "body"), idx["nodes"][0])
            with self.assertRaises(KeyError):
                content.get_node(idx, "absent")
        self.assertIs(content.get_node, original)

    def test_cache_and_local_limiter_preserve_real_reranker(self):
        idx = fixture()
        candidates = [{"node_id": "body/a"}]
        response = RerankResponse.model_validate({"nodes": [{"node_id": "body/a", "score": 0.5}]})
        client = SimpleNamespace(settings=SimpleNamespace(model="fake"), complete_model=lambda *a: response)
        baseline = ORIGINAL_RERANK(idx, "q", candidates, client, 1000, None)
        for name in ("E07", "E08"):
            with treatment(name, idx):
                self.assertEqual(ORIGINAL_RERANK(idx, "q", candidates, client, 1000, None), baseline)

    def test_field_removal_preserves_admission_and_other_fields(self):
        idx = fixture()
        candidates = [{"node_id": "body/a", "sources": ["structure"]}]
        original = RERANK_MODULE._fit_candidates(idx, candidates, 1000)
        for field in ("node_type", "token_estimate", "sources"):
            with field_removal(field):
                changed = RERANK_MODULE._fit_candidates(idx, candidates, 1000)
            expected = deepcopy(original)
            for row in expected:
                row.pop(field)
            self.assertEqual(changed, expected)

    def test_vector_duplicate_changes_prompt_even_without_new_nodes(self):
        structure = [{"node_id": "body/a"}]
        before, _, _ = retrieval._merge_semantic_matches(structure, [], max_unique_nodes=10)
        after, duplicates, added = retrieval._merge_semantic_matches(
            structure, [{"node_id": "body/a", "score": 0.8}], max_unique_nodes=10
        )
        self.assertEqual((duplicates, added), (1, 0))
        self.assertNotEqual(json.dumps(before), json.dumps(after))


if __name__ == "__main__":
    unittest.main()
