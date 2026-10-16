"""Exercise the prompt contracts that previously failed in real H01 requests."""

import json
from pathlib import Path
from string import Template
import tempfile
import unittest

from pydantic import ValidationError

from agents.json_utils import _validation_feedback, chat_until_valid_json
from agents.prompts import PROMPTS_PATH, get_prompt, load_prompts, render_prompt
from agents.schemas import ReflectorOutput, StrategyExtractionOutput, SubAgentOutput, SummaryOutput


CONTRACTS = (
    ("PLANNER_STRATEGY_EXPECTED_JSON", StrategyExtractionOutput),
    ("SUB_AGENT_EXPECTED_JSON", SubAgentOutput),
    ("REFLECTOR_EXPECTED_JSON", ReflectorOutput),
    ("SUMMARY_EXPECTED_JSON", SummaryOutput),
)


class PromptCatalogTests(unittest.TestCase):
    def test_all_output_examples_match_the_runtime_schemas(self):
        for name, schema in CONTRACTS:
            with self.subTest(name=name):
                schema.model_validate(json.loads(get_prompt(name)))

    def test_catalog_templates_preserve_literal_json_and_inserted_dollars(self):
        initial = '合同原文 ${not_a_template}，金额 $100，JSON {"x": 1}'
        expected = get_prompt("SUB_AGENT_EXPECTED_JSON")
        rendered = render_prompt("JSON_INITIAL_REQUEST_PROMPT", initial_prompt=initial, expected_format=expected)
        self.assertIn(initial, rendered)
        self.assertIn(expected, rendered)
        self.assertEqual(PROMPTS_PATH, Path(__file__).resolve().parents[2] / "agents" / "prompts.yaml")

    def test_unknown_prompt_and_missing_template_argument_fail_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown agent prompt"):
            get_prompt("UNKNOWN_PROMPT")
        with self.assertRaisesRegex(RuntimeError, "JSON_INITIAL_REQUEST_PROMPT"):
            render_prompt("JSON_INITIAL_REQUEST_PROMPT", initial_prompt="review")

    def test_catalog_rejects_non_string_extra_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.yaml"
            path.write_text(PROMPTS_PATH.read_text(encoding="utf-8") + "\nBAD_PROMPT: null\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "BAD_PROMPT"):
                load_prompts(path)

    def test_every_catalog_template_has_valid_placeholders(self):
        for name, prompt in load_prompts().items():
            with self.subTest(name=name):
                self.assertTrue(Template(prompt).is_valid())

    def test_h01_repeated_field_errors_produce_short_repair_feedback(self):
        output = {
            "status": "compliant", "issues": [],
            "coverage_decisions": [
                {"anchor_id": f"path:body/p{i}", "decision": "compliant", "reason": "已完整填写"}
                for i in range(29)
            ],
        }
        with self.assertRaises(ValidationError) as caught:
            SubAgentOutput.model_validate(output)
        self.assertEqual(len(caught.exception.errors()), 87)
        feedback = _validation_feedback(caught.exception)
        self.assertEqual(len(feedback.splitlines()), 3)
        self.assertLess(len(feedback), 1000)
        self.assertIn("coverage_decisions[].xml_anchor_type", feedback)
        self.assertIn("coverage_decisions[].xml_anchor_id", feedback)
        self.assertIn("29", feedback)
        self.assertNotIn("https://errors.pydantic.dev", feedback)
        self.assertNotIn("path:body/p28", feedback)


class FirstRequestContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_agent_receives_its_output_contract_on_the_first_send(self):
        class CapturingAgent:
            async def chat(self, prompt):
                self.prompt = prompt
                return self.response

        for name, schema in CONTRACTS:
            with self.subTest(name=name):
                agent = CapturingAgent()
                agent.response = get_prompt(name)
                output, _ = await chat_until_valid_json(agent, "本次实际任务", schema, agent.response)
                self.assertIn("本次实际任务", agent.prompt)
                self.assertIn(agent.response, agent.prompt)
                self.assertEqual(output, schema.model_validate(json.loads(agent.response)).model_dump())

    async def test_h01_repair_keeps_the_contract_and_omits_repeated_error_objects(self):
        valid = {"status": "compliant", "issues": [], "coverage_decisions": []}
        invalid = dict(valid, coverage_decisions=[
            {"anchor_id": f"path:body/p{i}", "decision": "compliant", "reason": "已填写"}
            for i in range(29)
        ])

        class CapturingAgent:
            def __init__(self):
                self.prompts = []

            async def chat(self, prompt):
                self.prompts.append(prompt)
                return json.dumps(invalid if len(self.prompts) == 1 else valid)

        agent = CapturingAgent()
        expected = get_prompt("SUB_AGENT_EXPECTED_JSON")
        result, _ = await chat_until_valid_json(agent, "H01", SubAgentOutput, expected)
        self.assertEqual(result["status"], "compliant")
        self.assertEqual(len(agent.prompts), 2)
        self.assertIn(expected, agent.prompts[1])
        self.assertLess(len(agent.prompts[1]) - len(expected), 1500)


if __name__ == "__main__":
    unittest.main()
