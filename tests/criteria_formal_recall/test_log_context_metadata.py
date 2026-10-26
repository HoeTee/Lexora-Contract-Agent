"""Criterion recovery metadata crosses MCP without changing model tool inputs."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from agents.orchestrator import OrchestratorAgent
from loggers.call_log import (
    current_call_context,
    criterion_call_context,
    reset_call_context,
    set_call_context,
)
from mcp_service.client.client import MinimalMCPClient


class CriterionLogMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_criteria_retain_attempts_and_restore_parent_context(self):
        agent = OrchestratorAgent(api_events_path="task/logs/events.log")
        observed = []

        async def execute(criterion):
            await asyncio.sleep(0)
            context = current_call_context()
            observed.append((criterion["id"], context))
            if context["criterion_attempt"] == 1:
                raise RuntimeError("transient")
            return {"criterion_id": criterion["id"], "status": "COMPLIANT"}

        agent.execute_single_criterion = execute
        token = set_call_context("parent/logs/events.log", criterion_id="parent")
        try:
            await asyncio.gather(*(
                agent._execute_criterion_with_recovery({"id": cid}, max_attempts=2)
                for cid in ("C01", "C02")
            ))
            self.assertEqual(current_call_context()["criterion_id"], "parent")
        finally:
            reset_call_context(token)
        self.assertEqual(len(observed), 4)
        for cid, context in observed:
            self.assertEqual(context["criterion_id"], cid)
            self.assertEqual(context["events_path"], "task/logs/events.log")
        self.assertEqual(sorted(item[1]["criterion_attempt"] for item in observed), [1, 1, 2, 2])

    async def test_mcp_injects_runtime_attempt_without_mutating_caller_arguments(self):
        client = object.__new__(MinimalMCPClient)
        client.session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(content=[])))
        client._import_operation_records = Mock()
        arguments = {"query": "contract text", "api_events_path": "task/logs/events.log",
                     "criterion_id": "C01"}
        original = deepcopy(arguments)
        with criterion_call_context("C01", 3, events_path=arguments["api_events_path"]):
            await client.call_tool("contract_search", arguments)
        sent_name, sent_arguments = client.session.call_tool.await_args.args
        self.assertEqual(sent_name, "contract_search")
        self.assertEqual(sent_arguments["criterion_attempt"], 3)
        self.assertTrue(sent_arguments["log_operation_id"])
        self.assertEqual(arguments, original)

    async def test_runtime_attempt_is_hidden_from_model_tool_schema(self):
        schema = {
            "type": "object", "properties": {
                "query": {"type": "string"},
                "criterion_attempt": {"type": ["integer", "null"], "default": None},
            }, "required": ["query"],
        }
        original = deepcopy(schema)
        client = object.__new__(MinimalMCPClient)
        client.session = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[
            SimpleNamespace(name="contract_search", description="Search", inputSchema=schema),
        ])))
        tools = await client.list_tools_openai()
        self.assertEqual(tools[0]["function"]["parameters"], {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
        })
        self.assertEqual(schema, original)


if __name__ == "__main__":
    unittest.main()
