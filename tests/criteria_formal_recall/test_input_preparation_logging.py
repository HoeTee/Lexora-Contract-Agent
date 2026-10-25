"""Unsendable slices retain counts without acquiring or contacting a model."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from endpoints.runtime.errors import ModelInputTooLong
from loggers.call_log import logical_call, reset_call_context, set_call_context, summarize_model_attempts
from tools.docxindex.model_input import embedding_chunks
from tools.docxindex.reranker import RerankerClient
from tools.docxindex.retrieval.rerank import rerank_matches
from tools.docxindex.vector.index import build_vector_index


class InputPreparationLoggingTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "logs"
        for name, value in (("LOGGING_ENABLED", True), ("LOGGING_DETAILED", False)):
            patcher = patch(f"loggers.call_log.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        token = set_call_context(self.root / "events.log", operation_id="op-preparation", criterion_id="H12")
        self.addCleanup(reset_call_context, token)
        for attribute, target in (
            ("admission", "endpoints.runtime.model_calls.model_semaphore"),
            ("retries", "endpoints.runtime.model_calls._retry_delay"),
            ("sdk", "endpoints.runtime.embedding_client.AsyncOpenAI"),
            ("http", "endpoints.runtime.reranker_client.httpx.AsyncClient.post"),
        ):
            patcher = patch(target)
            setattr(self, attribute, patcher.start())
            self.addCleanup(patcher.stop)

    def assert_local_rejection(self, error, kind, limit):
        records = list((self.root / "logical_calls").glob("*.json"))
        self.assertEqual(len(records), 1)
        logical = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(logical["status"], "failed")
        self.assertEqual(logical["retry_count"], 0)
        self.assertEqual(logical.get("send_count", 0), 0)
        self.assertEqual(len(logical["attempts"]), 1)
        attempt = logical["attempts"][0]
        self.assertIs(attempt["sent"], False)
        self.assertEqual(attempt["failure_stage"], "input_validation")
        self.assertEqual(attempt["model_type"], kind)
        self.assertGreater(attempt["local_input_tokens"], 0)
        self.assertGreater(attempt["input_tokens"], limit)
        self.assertEqual(attempt["max_input_tokens"], limit)
        self.assertIn("tokenizer_id", attempt["tokenizer"])
        self.assertEqual(error.model_type, kind)
        self.assertEqual(error.model, logical["model"])
        self.assertEqual(error.limit, limit)
        self.assertEqual(error.input_tokens, max(attempt["input_token_counts"]))
        self.assertEqual(error.call_path, str(records[0]))
        self.assertIs(error.sent, False)
        self.assertFalse((self.root / "calls").exists())
        summary = summarize_model_attempts(self.root / "logical_calls")
        self.assertEqual((summary["input_rejections"], summary["sent_attempts"], summary["total"]), (1, 0, 0))
        for dependency in (self.admission, self.retries, self.sdk, self.http):
            dependency.assert_not_called()
        return attempt

    def test_reranker_query_overflow_has_compact_rejection_and_preserves_node(self):
        index = {"nodes": [{"node_id": "A", "text": "完整原文与节点编号必须保留。"}]}
        original = deepcopy(index)
        client = RerankerClient(model="bge-reranker-v2-m3", max_input_tokens=20)
        with patch.object(client, "rank") as rank:
            with self.assertRaises(ModelInputTooLong) as captured:
                rerank_matches(index, "查询内容。" * 100, [{"node_id": "A"}], client)
        rank.assert_not_called()
        self.assertEqual(index, original)
        attempt = self.assert_local_rejection(captured.exception, "reranker", 20)
        self.assertEqual(attempt["input_preparation"]["node_id"], "A")
        self.assertGreater(attempt["input_preparation"]["fixed_input_tokens"], 20)

    def test_embedding_special_token_overhead_has_compact_rejection_before_batch(self):
        index = {"nodes": [{"node_id": "A", "node_type": "paragraph", "text": "完整合同。"}]}
        original = deepcopy(index)
        client = SimpleNamespace(settings=SimpleNamespace(model="bge-m3", max_input_tokens=1,
                                                          chunk_overlap_tokens=0), embed_batch=Mock())
        with self.assertRaises(ModelInputTooLong) as captured:
            build_vector_index(index, client, concurrency=1)
        client.embed_batch.assert_not_called()
        self.assertEqual(index, original)
        attempt = self.assert_local_rejection(captured.exception, "embedding", 1)
        self.assertEqual(attempt["input_preparation"]["fixed_input_tokens"], 2)

    def test_embedding_unsplittable_character_reuses_logical_call_without_send(self):
        text = "合同"
        with logical_call("docxindex.embedding", "bge-m3") as logical:
            with self.assertRaisesRegex(ModelInputTooLong, "next source character") as captured:
                embedding_chunks(text, "bge-m3", 2, 0)
            self.assertEqual(logical.budget.sends, 0)
            self.assertEqual(len(logical.attempt_records), 1)
        self.assertEqual(text, "合同")
        self.assert_local_rejection(captured.exception, "embedding", 2)
