"""Shared tokenizer selection and complete-message input-budget contracts."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace

from endpoints.runtime.errors import ModelInputTooLong
from endpoints.runtime.model_tokens import (
    count_model_input, count_model_output, model_tokenizer, prepare_chat_messages,
)
from tools.docxindex.model_input import embedding_chunks


class ModelTokenMemoryTests(TestCase):
    def test_explicit_aliases_allow_multiple_names_and_casefold(self):
        mapping = {"bge-m3": ("Company-Embedding", "Other-Embedding"), "cl100k_base": ("other",)}
        with patch("config.TOKENIZER_MODEL_NAMES", mapping):
            self.assertEqual(model_tokenizer(" Company-Embedding ").slug, "bge-m3")
            self.assertEqual(model_tokenizer("OTHER-EMBEDDING").slug, "bge-m3")
            # No built-in model-name alias bypasses the supplied deployment map.
            fallback = model_tokenizer("bge-m3")
            self.assertEqual(fallback.slug, "cl100k_base")
            self.assertTrue(fallback.metadata["estimated"])
            self.assertEqual(fallback.metadata["tokenizer_match"], "other")

    def test_fallback_table_is_loaded_without_tiktoken_download(self):
        from endpoints.runtime.model_tokens import _fallback_encoder
        _fallback_encoder.cache_clear()
        with patch("tiktoken.get_encoding", side_effect=AssertionError("must remain offline")):
            self.assertGreater(model_tokenizer("Unknown-Model").count_text("合同"), 0)

    def test_complete_chat_count_includes_tools_and_does_not_mutate_arguments(self):
        messages = [{"role": "system", "content": "审查合同"}, {"role": "user", "content": "检查付款"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "t1", "type": "function", "function": {"name": "find", "arguments": '{"q":"付款"}'}}
                    ]}, {"role": "tool", "tool_call_id": "t1", "content": "支付条款"}]
        original = deepcopy(messages)
        request = {"messages": messages, "extra_body": {"enable_thinking": False}}
        without = count_model_input("llm", "qwen3.6-35b-a3b", request)
        with_tools = count_model_input("llm", "qwen3.6-35b-a3b", {**request, "tools": [
            {"type": "function", "function": {"name": "find", "description": "检索条款", "parameters": {
                "type": "object", "properties": {"q": {"type": "string"}}}}}
        ]})
        self.assertGreater(with_tools.input_tokens, without.input_tokens)
        self.assertEqual(original, messages)
        self.assertFalse(with_tools.metadata["estimated"])

    def test_deepseek_0731_uses_packaged_message_encoder(self):
        model = "DeepSeek-V4-Flash-0731"
        plain = {"messages": [{"role": "system", "content": "审查"}, {"role": "user", "content": "合同"}],
                 "extra_body": {"enable_thinking": False}}
        result = count_model_input("llm", model, plain)
        self.assertGreater(result.input_tokens, 0)
        self.assertEqual(result.metadata["tokenizer_id"], "deepseek-v4-flash-0731")
        self.assertFalse(result.metadata["estimated"])
        tools = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
        self.assertGreater(count_model_input("llm", model, {**plain, "tools": tools}).input_tokens, result.input_tokens)

    def test_batch_inputs_are_counted_individually_with_special_tokens(self):
        embedded = count_model_input("embedding", "bge-m3", {"input": ["合同", "付款"]})
        tokenizer = model_tokenizer("bge-m3")
        self.assertEqual(embedded.input_token_counts, [tokenizer.count_embedding("合同"), tokenizer.count_embedding("付款")])
        self.assertEqual(embedded.input_tokens, sum(embedded.input_token_counts))
        reranked = count_model_input("reranker", "bge-reranker-v2-m3", {
            "input": {"query": "付款", "documents": ["合同", {"text": "运输"}]}})
        self.assertEqual(len(reranked.input_token_counts), 2)
        self.assertTrue(all(value > 0 for value in reranked.input_token_counts))

    def test_output_count_retains_partial_reasoning_and_tools(self):
        partial = {0: {"message": {"content": "结论", "reasoning_content": "推理",
            "tool_calls": [{"function": {"name": "search", "arguments": '{"q":"合'}}]}}}
        self.assertEqual(count_model_output("qwen3.6-35b-a3b", partial),
                         model_tokenizer("qwen3.6-35b-a3b").count_text('结论推理search{"q":"合'))

    def test_legacy_completion_counts_raw_prompt_without_chat_wrapping(self):
        model = "qwen3.6-35b-a3b"
        count = count_model_input("llm", model, {"prompt": "审查运输条款"})
        self.assertEqual(count.input_token_counts, [model_tokenizer(model).count_text("审查运输条款")])

    def test_old_messages_removed_as_whole_tool_transactions(self):
        messages = [{"role": "system", "content": "保留系统"}, {"role": "user", "content": "原始任务"},
                    {"role": "assistant", "content": "旧答案" * 100},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "a", "function": {"name": "search", "arguments": "{}"}},
                        {"id": "b", "function": {"name": "search", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "a", "content": "旧工具结果" * 100},
                    {"role": "tool", "tool_call_id": "b", "content": "第二个结果" * 100},
                    {"role": "assistant", "content": "较新答案"},
                    {"role": "user", "content": "最新请求"}]
        original = deepcopy(messages)
        required = [messages[0], messages[1], messages[6], messages[7]]
        limit = model_tokenizer("qwen3.6-35b-a3b").count_chat(required)
        prepared, metadata = prepare_chat_messages(messages, model="qwen3.6-35b-a3b", tools=[], max_input_tokens=limit)
        self.assertEqual(messages, original)
        self.assertEqual(prepared, required)
        self.assertIn([3, 4, 5], metadata["removed_message_groups"])
        self.assertLessEqual(metadata["input_tokens_after"], limit)

    def test_latest_tool_transaction_is_retained(self):
        messages = [{"role": "system", "content": "系统"}, {"role": "user", "content": "任务"},
                    {"role": "assistant", "content": "旧消息" * 100},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "a", "function": {"name": "search", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "a", "content": "当前证据"}]
        limit = model_tokenizer("qwen3.6-35b-a3b").count_chat([*messages[:2], *messages[3:]])
        prepared, _ = prepare_chat_messages(messages, model="qwen3.6-35b-a3b", tools=[], max_input_tokens=limit)
        self.assertEqual(prepared, [*messages[:2], *messages[3:]])

    def test_required_input_overflow_has_counts_and_is_not_retryable(self):
        messages = [{"role": "system", "content": "不可删除"}, {"role": "user", "content": "任务" * 100}]
        with self.assertRaises(ModelInputTooLong) as captured:
            prepare_chat_messages(messages, model="qwen3.6-35b-a3b", tools=[], max_input_tokens=5)
        self.assertFalse(captured.exception.sent)
        self.assertFalse(captured.exception.retryable)
        self.assertGreater(captured.exception.input_tokens, captured.exception.limit)

    def test_null_limit_keeps_all_history_and_native_capacity_does_not_clamp(self):
        messages = [{"role": "user", "content": "任务"}]
        prepared, metadata = prepare_chat_messages(messages, model="qwen3.6-35b-a3b", tools=[], max_input_tokens=None)
        self.assertEqual(prepared, messages)
        self.assertEqual(metadata["removed_message_groups"], [])
        text = "合同条款" * 5000
        chunks = embedding_chunks(text, "bge-m3", None, 32)
        self.assertEqual([chunk.text for chunk in chunks], [text])
        self.assertEqual(model_tokenizer("bge-m3").effective_limit(40000), 40000)

    def test_large_overlap_warns_and_still_covers_source(self):
        text = "合同运输条款。" * 30
        with self.assertLogs("tools.docxindex.model_input", level="WARNING"):
            chunks = embedding_chunks(text, "bge-m3", 12, 100)
        self.assertEqual(chunks[0].start, 0)
        self.assertEqual(chunks[-1].end, len(text))
        for previous, current in zip(chunks, chunks[1:]):
            self.assertGreater(current.start, previous.start)
            self.assertLessEqual(current.start, previous.end)

    def test_agent_usage_counts_failed_attempt_once_and_preserves_unknown_output(self):
        from agents.base_agent import Agent
        agent = Agent.__new__(Agent)
        failed = SimpleNamespace(call_id="first", payload={
            "sent": True, "input_tokens": 50, "output_tokens": None,
            "input_token_source": "local", "output_completeness": "unknown"})
        success = SimpleNamespace(call_id="second", payload={
            "sent": True, "input_tokens": 70, "output_tokens": 10,
            "input_token_source": "service", "output_token_source": "service",
            "output_completeness": "complete"})
        unsent = SimpleNamespace(call_id="blocked", payload={"sent": False, "input_tokens": 9000})
        agent._update_token_usage([failed, success, unsent])
        agent._update_token_usage([failed, success])
        self.assertEqual(agent.token_usage["prompt_tokens"], 120)
        self.assertIsNone(agent.token_usage["completion_tokens"])
        self.assertEqual(agent.token_usage["known_completion_tokens"], 10)
        self.assertEqual(agent.token_usage["total_tokens"], 130)
        self.assertTrue(agent.token_usage["total_is_lower_bound"])
        self.assertEqual(agent.token_usage["sent_attempt_count"], 2)
