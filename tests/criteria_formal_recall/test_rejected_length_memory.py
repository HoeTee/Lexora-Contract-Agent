from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import BaseModel

from agents.base_agent import Agent, Settings
from agents.json_utils import chat_until_valid_json
from endpoints.runtime.model_tokens import count_model_input, model_tokenizer
from loggers.call_log import reset_call_context, set_call_context
from tests.criteria_formal_recall._model_test_support import model_transport


class Answer(BaseModel):
    answer: str


def _response(content: str | None, finish_reason: str = "stop", *, tool_calls=None) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "test", "object": "chat.completion", "created": 0, "model": "fake",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


class RejectedLengthMemoryTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def harness(self, responses, *, tools=None, model="qwen3.6-35b-a3b", enable_thinking=None):
        sequence = iter(responses)
        requests, conversations = [], []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=next(sequence))

        with (
            TemporaryDirectory() as tmp,
            model_transport("agents.base_agent", handler),
            patch("endpoints.runtime.llm_client.LLM_STREAM", False),
            patch("loggers.call_log.LOGGING_DETAILED", True),
            patch("agents.base_agent.log_agent_step"),
            patch("agents.base_agent.log_conversation", side_effect=lambda _name, messages: conversations.append(deepcopy(messages))),
        ):
            logs = Path(tmp) / "logs"
            token = set_call_context(logs / "events.log", stage="agent")
            agent = Agent(
                name="TestAgent", system_prompt="Keep the original task.", max_input_tokens=None,
                settings=Settings(api_key="fake", model=model, enable_thinking=enable_thinking), tools=tools,
            )
            try:
                yield agent, requests, conversations, logs
            finally:
                reset_call_context(token)
                await agent.client.close()

    async def test_invalid_length_response_is_excluded_from_send_but_history_logs_and_counts_remain(self):
        invalid = '{"answer":"' + "unfinished " * 2000
        async with self.harness([_response(invalid, "length"), _response('{"answer":"ok"}')]) as (agent, requests, conversations, logs):
            result, _ = await chat_until_valid_json(agent, "Review this contract.", Answer, '{"answer":"text"}', max_attempts=2)

            self.assertEqual(result, {"answer": "ok"})
            self.assertEqual(len(requests), 2)
            self.assertNotIn(invalid, json.dumps(requests[1]["messages"]))
            self.assertEqual(agent.messages[2], {"role": "assistant", "content": invalid})
            self.assertEqual(conversations[-1][2]["content"], invalid)
            self.assertEqual(agent.last_input_preparation["excluded_messages"], [{
                "index": 2, "reason": "schema_rejected_length_response", "finish_reason": "length",
            }])
            self.assertEqual(agent.last_input_preparation["retained_message_indexes"], [0, 1, 3])
            actual_input = count_model_input("llm", agent.settings.model, requests[1]).input_tokens
            self.assertEqual(agent.last_input_preparation["input_tokens_after"], actual_input)

            records = [json.loads(path.read_text(encoding="utf-8")) for path in (logs / "calls").glob("*.json")]
            rejected = next(record for record in records if record["response"]["choices"][0]["message"]["content"] == invalid)
            self.assertEqual(rejected["failure_stage"], "output_validation")
            self.assertEqual(rejected["response"]["choices"][0]["finish_reason"], "length")
            self.assertGreater(rejected["output_tokens"], 0)
            repair = next(record for record in records if record["request"]["messages"] == requests[1]["messages"])
            self.assertEqual(repair["local_input_tokens"], actual_input)
            self.assertEqual(repair["input_preparation"]["excluded_messages"], agent.last_input_preparation["excluded_messages"])
            logical, = [json.loads(path.read_text(encoding="utf-8")) for path in (logs / "logical_calls").glob("*.json")]
            self.assertEqual([attempt["status"] for attempt in logical["attempts"]], ["failed", "succeeded"])
            self.assertEqual(agent.token_usage["sent_attempt_count"], 2)

    async def test_length_response_rejected_by_pydantic_is_also_excluded(self):
        invalid = '{"answer":{"unexpected":"object"}}'
        async with self.harness([_response(invalid, "length"), _response('{"answer":"ok"}')]) as (agent, requests, _conversations, _logs):
            await chat_until_valid_json(agent, "Review.", Answer, '{"answer":"text"}', max_attempts=2)
            self.assertFalse(any(message.get("content") == invalid for message in requests[1]["messages"]))
            self.assertEqual(agent.messages[2]["content"], invalid)
            self.assertEqual(agent.last_input_preparation["excluded_messages"][0]["index"], 2)

    async def test_native_and_fallback_count_the_final_copy_with_explicit_false_consistently(self):
        invalid = '{"answer":"unfinished'
        for model in ("qwen3.6-35b-a3b", "unlisted-provider-model"):
            with self.subTest(model=model):
                async with self.harness(
                    [_response(invalid, "length"), _response('{"answer":"ok"}')],
                    model=model, enable_thinking=False,
                ) as (agent, requests, _conversations, logs):
                    await chat_until_valid_json(agent, "Review.", Answer, '{"answer":"text"}', max_attempts=2)
                    records = [json.loads(path.read_text(encoding="utf-8")) for path in (logs / "calls").glob("*.json")]
                    repair = next(record for record in records if record["request"]["messages"] == requests[1]["messages"])

                    self.assertIs(repair["request"]["extra_body"]["enable_thinking"], False)
                    self.assertFalse(any(message.get("content") == invalid for message in repair["request"]["messages"]))
                    counted = count_model_input("llm", model, repair["request"]).input_tokens
                    self.assertEqual(repair["local_input_tokens"], counted)
                    self.assertEqual(agent.last_input_preparation["input_tokens_after"], counted)

    def test_fallback_ignores_none_options_and_preserves_false(self):
        tokenizer = model_tokenizer("unlisted-provider-model")
        messages = [{"role": "user", "content": "Review the contract."}]
        counted_false = tokenizer.count_chat(messages, enable_thinking=False)
        self.assertEqual(tokenizer.count_chat(
            messages, enable_thinking=False, reasoning_effort=None, response_format=None,
        ), counted_false)
        self.assertEqual(tokenizer.count_chat(
            messages, enable_thinking=None, reasoning_effort=None, response_format=None,
        ), tokenizer.count_chat(messages))
        self.assertGreater(counted_false, tokenizer.count_chat(messages))

    async def test_invalid_stop_response_stays_in_schema_repair_input(self):
        invalid = '{"answer":"unfinished'
        async with self.harness([_response(invalid), _response('{"answer":"ok"}')]) as (agent, requests, _conversations, _logs):
            await chat_until_valid_json(agent, "Review.", Answer, '{"answer":"text"}', max_attempts=2)
            self.assertTrue(any(message.get("content") == invalid for message in requests[1]["messages"]))
            self.assertEqual(agent.last_input_preparation["excluded_messages"], [])

    async def test_valid_length_response_is_accepted_and_remains_in_next_request(self):
        valid = '{"answer":"valid despite length"}'
        async with self.harness([_response(valid, "length"), _response('{"answer":"next"}')]) as (agent, requests, _conversations, _logs):
            result, _ = await chat_until_valid_json(agent, "Review.", Answer, '{"answer":"text"}', max_attempts=2)
            self.assertEqual(result, {"answer": "valid despite length"})
            await agent.chat("Review another item.")
            self.assertTrue(any(message.get("content") == valid for message in requests[1]["messages"]))
            self.assertEqual(agent.last_input_preparation["excluded_messages"], [])

    async def test_replacing_history_does_not_exclude_an_equal_message_or_reused_index(self):
        invalid = '{"answer":"unfinished'
        async with self.harness([_response(invalid, "length"), _response('{"answer":"ok"}')]) as (agent, requests, _conversations, _logs):
            await agent.chat("Old task.")
            rejected_message = agent.messages[2]
            agent.reject_schema_response(ValueError("Invalid JSON"))
            agent.messages = deepcopy(agent.messages)
            self.assertEqual(agent.messages[2], rejected_message)
            self.assertIsNot(agent.messages[2], rejected_message)

            await agent.chat("New task after a history reset.")

            self.assertTrue(any(message.get("content") == invalid for message in requests[1]["messages"]))
            self.assertEqual(agent.last_input_preparation["excluded_messages"], [])
            self.assertEqual(agent._rejected_length_responses, [])

    async def test_excluding_rejected_final_text_preserves_complete_tool_transaction(self):
        tool = {"id": "lookup-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        schema = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]
        invalid = '{"answer":"unfinished'
        async with self.harness([
            _response(None, "tool_calls", tool_calls=[tool]), _response(invalid, "length"), _response('{"answer":"ok"}'),
        ], tools=schema) as (agent, requests, _conversations, _logs):
            with patch.object(agent, "_execute_a_tool", AsyncMock(return_value="Original contract evidence.")):
                await chat_until_valid_json(agent, "Review.", Answer, '{"answer":"text"}', max_attempts=2)

            tool_messages = [message for message in requests[2]["messages"] if message.get("tool_calls") or message.get("role") == "tool"]
            self.assertEqual(len(tool_messages), 2)
            self.assertEqual(tool_messages[0]["tool_calls"][0]["id"], "lookup-1")
            self.assertEqual(tool_messages[1], {"role": "tool", "tool_call_id": "lookup-1", "content": "Original contract evidence."})
            self.assertFalse(any(message.get("content") == invalid for message in requests[2]["messages"]))
            self.assertEqual(agent.messages[4]["content"], invalid)
            self.assertEqual(agent.last_input_preparation["retained_message_indexes"], [0, 1, 2, 3, 5])
            self.assertEqual(agent.last_input_preparation["excluded_messages"][0]["index"], 4)


if __name__ == "__main__":
    unittest.main()
