"""Native-tokenizer boundaries, complete source coverage and node-level retrieval contracts."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

import httpx

from tests.criteria_formal_recall._model_test_support import reranker_transport
from tools.docxindex.model_input import TOKENIZER_ROOT, ModelInputTooLong, embedding_chunks, model_tokenizer
from tools.docxindex.reranker import RerankerClient
from tools.docxindex.retrieval.rerank import _candidate_batches, rerank_matches
from tools.docxindex.vector.index import build_vector_index
from tools.docxindex.vector.schema import VectorIndex, VectorItem, VectorMetadata
from tools.docxindex.vector.search import vector_search


class ChunkedModelInputTests(unittest.TestCase):
    def test_all_six_tokenizers_are_versioned_local_native_resources(self):
        manifest = json.loads((TOKENIZER_ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["models"]), 6)
        for model in manifest["models"]:
            with self.subTest(model=model):
                tokenizer = model_tokenizer(model)
                self.assertTrue(tokenizer.native)
                self.assertIn(manifest["models"][model]["revision"], tokenizer.identity)

    def test_embedding_chunks_preserve_unicode_and_cover_every_source_character(self):
        text = "甲方收到货物后应支付运输费用。🚚\n" * 500 + "最后一段独有证据NEEDLE"
        for model in ("bge-m3", "qwen3-embedding"):
            for overlap in (0, 32):
                with self.subTest(model=model, overlap=overlap):
                    chunks = embedding_chunks(text, model, 1024, overlap)
                    self.assertGreater(len(chunks), 1)
                    self.assertEqual(chunks[0].start, 0)
                    self.assertEqual(chunks[-1].end, len(text))
                    for chunk in chunks:
                        self.assertEqual(text[chunk.start:chunk.end], chunk.text)
                        self.assertLessEqual(model_tokenizer(model).count_embedding(chunk.text), 1024)
                    for previous, current in zip(chunks, chunks[1:], strict=False):
                        self.assertGreater(current.start, previous.start)
                        self.assertLessEqual(current.start, previous.end)
                        if overlap == 0:
                            self.assertEqual(current.start, previous.end)
                        else:
                            self.assertLessEqual(model_tokenizer(model).count_text(text[current.start:previous.end]), overlap)

    def test_only_explicit_configuration_controls_the_input_limit(self):
        self.assertEqual(model_tokenizer("bge-m3").effective_limit(40000), 40000)
        self.assertEqual(model_tokenizer("qwen3-reranker").effective_limit(40000), 40000)
        self.assertEqual(model_tokenizer("bge-m3").effective_limit(1024), 1024)
        self.assertIsNone(model_tokenizer("bge-m3").effective_limit(None))

    def test_build_keeps_original_node_and_embeds_its_last_piece(self):
        node = {"node_id": "N001", "node_type": "paragraph", "title": "运输", "summary": "费用承担",
                "text": "正文内容与付款条件。\n" * 600 + "NEEDLE最终运输条款"}
        index = {"nodes": [node]}
        original = deepcopy(index)
        requests = []
        def embed_batch(texts):
            requests.extend(texts)
            return [[1.0, 0.0] if "NEEDLE" in text else [0.0, 1.0] for text in texts]
        client = SimpleNamespace(settings=SimpleNamespace(model="bge-m3", max_input_tokens=1024, chunk_overlap_tokens=32),
                                 embed_batch=embed_batch)
        vector = build_vector_index(index, client, concurrency=1)
        self.assertEqual(index, original)
        self.assertGreater(len(vector.items), 1)
        self.assertEqual({item.node_id for item in vector.items}, {"N001"})
        self.assertEqual(len({item.vector_id for item in vector.items}), len(vector.items))
        self.assertTrue(any("NEEDLE" in text for text in requests))
        self.assertEqual([item.text for item in vector.items], requests)
        self.assertEqual(vector.format_version, 2)

    def test_max_piece_score_is_merged_before_unique_node_limit(self):
        metadata = VectorMetadata(title="", node_type="paragraph")
        def item(vector_id, node_id, vector):
            return VectorItem(vector_id=vector_id, node_id=node_id, text_hash="hash", text="piece",
                              embedding=vector, metadata=metadata)
        vector = VectorIndex(embedding_model="test", items=[item("A1", "A", [0, 1]),
            item("A2", "A", [1, 0]), item("A3", "A", [0.99, 0.01]), item("B1", "B", [0.8, 0.2])])
        index = {"nodes": [{"node_id": "A", "text": "complete A"}, {"node_id": "B", "text": "complete B"}]}
        client = SimpleNamespace(embed=lambda texts: [[1.0, 0.0]])
        ranked = vector_search(index, vector, "query", client, top_k=2)
        self.assertEqual([row["node_id"] for row in ranked], ["A", "B"])
        self.assertEqual(ranked[0]["score"], 1.0)
        self.assertEqual(ranked[0]["matched_vector_id"], "A2")
        scoped = vector_search(index, vector, "query", client, top_k=2, allowed_node_ids={"B"})
        self.assertEqual([row["node_id"] for row in scoped], ["B"])

    def test_rerank_1k_scores_all_pieces_and_preserves_original_nodes(self):
        nodes = [{"node_id": "A", "text": "付款和运输约定。\n" * 1800 + "NEEDLE最高相关运输条款"},
                 {"node_id": "B", "text": "普通候选"}]
        candidates = [{"node_id": "A", "sources": ["vector"]}, {"node_id": "B", "sources": ["structure"]}]
        original = deepcopy(nodes)
        client = RerankerClient(model="bge-reranker-v2-m3", api_key="fake-key", base_url="https://model.test/v1/rerank",
                                max_input_tokens=1024)
        requests = []
        counter = model_tokenizer(client.model)
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(set(payload), {"model", "query", "documents", "top_n"})
            self.assertEqual(request.headers["X-API-Key"], "fake-key")
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(payload["top_n"], len(payload["documents"]))
            self.assertTrue(all(counter.count_rerank(payload["query"], doc) <= 1024 for doc in payload["documents"]))
            requests.extend(payload["documents"])
            return httpx.Response(200, json={"results": [{"index": i, "relevance_score": 0.99 if "NEEDLE" in doc else 0.1}
                                                       for i, doc in enumerate(payload["documents"])]})
        with reranker_transport(handler):
            ranked = rerank_matches({"nodes": nodes}, "运输费用由谁承担？", candidates, client, 1)
        self.assertEqual(nodes, original)
        self.assertEqual([row["node_id"] for row in ranked], ["A", "B"])
        self.assertEqual(ranked[0]["rerank_score"], 0.99)
        self.assertEqual(ranked[0]["sources"], ["vector"])
        self.assertGreater(len(requests), 10)
        views = _candidate_batches({"nodes": nodes}, "运输费用由谁承担？", candidates, client=client)
        # The prepared documents are contiguous source slices, not duplicated windows.
        combined = "".join(piece["_document"] for batch in views for piece in batch if piece["node_id"] == "A")
        self.assertEqual(json.loads(combined)["text"], nodes[0]["text"])

    def test_rerank_honors_empty_text_override_and_duplicate_content(self):
        nodes = [{"node_id": "A", "text": "must not leak"}, {"node_id": "B", "text": "same"},
                 {"node_id": "C", "text": "same"}]
        candidates = [{"node_id": "A", "text_override": ""}, {"node_id": "B"}, {"node_id": "C"}]
        seen = []
        client = SimpleNamespace(model="fake", max_input_tokens=1024, rank=lambda query, docs:
                                 (seen.extend(docs) or [(i, 1.0) for i in range(len(docs))]))
        result = rerank_matches({"nodes": nodes}, "q", candidates, client)
        self.assertNotIn("must not leak", "".join(seen))
        self.assertEqual([row["node_id"] for row in result], ["A", "B", "C"])

    def test_query_overflow_is_explicit_before_http(self):
        client = RerankerClient(model="bge-reranker-v2-m3", max_input_tokens=20)
        with self.assertRaises(ModelInputTooLong):
            rerank_matches({"nodes": []}, "查询内容。" * 100, [{"node_id": "A"}], client)

    def test_http_400_retains_gateway_error_body(self):
        client = RerankerClient(model="bge-reranker-v2-m3", base_url="https://model.test/rerank")
        def handler(request):
            return httpx.Response(400, json={"error": "context_length_exceeded", "limit": 1024})
        with reranker_transport(handler), self.assertRaisesRegex(httpx.HTTPStatusError, "context_length_exceeded"):
            client.rank("query", ["document"])


if __name__ == "__main__":
    unittest.main()
