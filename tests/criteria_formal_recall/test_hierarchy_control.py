"""Optional hierarchy enrichment must preserve deterministic fallback boundaries."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from endpoints.runtime.model_budget import ModelCallTimeout, ModelRetryLimitError
from loggers.call_log import reset_call_context, set_call_context
from tests.criteria_formal_recall._model_test_support import model_transport
from tools.docxindex.indexing.attachment_hierarchy import _infer_profile_attempts
from tools.docxindex.indexing.attachments import build_attachments
from tools.docxindex.indexing.llm_expand import expand_large_leaves
from tools.docxindex.indexing.node_factory import make_node
from tools.docxindex.llm.client import LLMClient, LLMClientError, LLMResponseValidationError, LLMSettings
from tools.docxindex.schema import BodyItem


class HierarchyFallbackTests(TestCase):
    @staticmethod
    def items(title):
        texts = [title, "第一条 付款", "付款内容" * 40, "第二条 交付", "交付内容" * 40]
        return [BodyItem(kind="p", anchor=f"p_{i + 1:04d}", body_child_index=i + 1,
                         xml_path=f"body/p{i + 1}", text=text,
                         direct_outline=0 if i in (1, 3) else None)
                for i, text in enumerate(texts)]

    def test_schema_or_structure_failure_uses_body_chunks_after_shared_attempts(self):
        for content in ("not JSON", '{"segments":[{"start_anchor":"missing-anchor"}]}'):
            with self.subTest(content=content), TemporaryDirectory() as directory:
                items = self.items("合同正文")
                root = make_node("body", "section", "合同正文", items, 0, len(items), None, None)
                original_text = root.text
                sends = []

                def handler(request, *, sends=sends, content=content):
                    sends.append(json.loads(request.content))
                    return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                        "model": "fake", "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 9, "completion_tokens": 2}})

                token = set_call_context(Path(directory) / "logs" / "events.log")
                try:
                    with model_transport("tools.docxindex.llm.client", handler):
                        expand_large_leaves([root], items, LLMClient(LLMSettings(model="fake")),
                                            Path(directory) / "cache", concurrency=1,
                                            split_threshold_tokens=30, chunk_target_tokens=20)
                finally:
                    reset_call_context(token)
                self.assertEqual(len(sends), 3)
                self.assertIn("JSON 必须符合以下 schema", sends[1]["messages"][0]["content"])
                self.assertTrue(root.children)
                self.assertEqual({child.node_type for child in root.children}, {"chunk"})
                self.assertEqual("\n".join(child.text for child in root.children), original_text)
                records = list((Path(directory) / "logs" / "logical_calls").glob("*.json"))
                self.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text(encoding="utf-8"))
                self.assertEqual((record["status"], record["retry_count"]), ("failed", 2))
                self.assertEqual(len(record["attempts"]), 3)
                self.assertEqual({attempt["failure_stage"] for attempt in record["attempts"]}, {"output_validation"})
                self.assertFalse((Path(directory) / "cache" / "llm_hierarchy.jsonl").exists())

    def test_output_failure_preserves_attachment_deterministic_children(self):
        items = self.items("附件一 产品清单")
        client = Mock(settings=LLMSettings(model="fake"))
        error = LLMResponseValidationError("invalid hierarchy", http_status=200,
                                           error_type="response_validation_error")
        error.failure_stage = "output_validation"
        client.complete_model.side_effect = error

        roots = build_attachments(items, 0, len(items), "attachments", split_long_nodes=False,
                                  llm_client=client, llm_concurrency=1)

        self.assertEqual(client.complete_model.call_count, 1)
        self.assertEqual(len(roots), 1)
        self.assertIn("llm_attachment_hierarchy_fallback", roots[0].confidence_evidence)
        self.assertEqual([child.title for child in roots[0].children], ["第一条 付款", "第二条 交付"])
        self.assertIn("付款内容" * 40, roots[0].children[0].text)
        self.assertIn("交付内容" * 40, roots[0].children[1].text)

    def test_network_budget_redis_and_send_stage_errors_do_not_become_fallback(self):
        send_validation = LLMResponseValidationError("bad request configuration", http_status=None,
                                                      error_type="ValueError")
        send_validation.failure_stage = "request"
        errors = [
            LLMClientError("network unavailable", http_status=None, error_type="APIConnectionError"),
            ModelCallTimeout("shared deadline expired"),
            ModelRetryLimitError("shared allowance exhausted"),
            RedisConnectionError("Redis unavailable"),
            send_validation,
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client = Mock()
                client.complete_model.side_effect = error
                logical = Mock()
                with self.assertRaises(type(error)) as caught:
                    _infer_profile_attempts("prompt", [], client, Mock(), "cache-key", 6, logical)
                self.assertIs(caught.exception, error)
                self.assertEqual(client.complete_model.call_count, 1)
                logical.failed.assert_not_called()
