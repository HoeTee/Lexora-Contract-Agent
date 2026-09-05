"""Keep structure-query instructions aligned with the compact payload."""

import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from tools.docxindex.retrieval.config import RetrievalConfig, VectorRetrievalConfig
from tools.docxindex.retrieval.llm_query import QUERY_PROMPT_VERSION, _compact_structure, _query_prompt


class StructureFieldContractTests(unittest.TestCase):
    def test_prompt_only_describes_fields_sent_to_model(self):
        compact = _compact_structure([{
            "node_id": "body/payment", "title": "付款", "node_type": "section",
            "summary": "付款期限", "token_estimate": 100,
            "key_items": ["付款期限"], "start_index": 1, "end_index": 2,
        }])
        prompt = _query_prompt("付款期限", json.dumps(compact, ensure_ascii=False))
        self.assertEqual(set(compact[0]), {"node_id", "title", "node_type", "summary", "token_estimate"})
        for omitted in ("key_items", "start_index", "end_index"):
            self.assertNotIn(omitted, prompt)
        self.assertEqual(QUERY_PROMPT_VERSION, "docx_query_v4")

    def test_unused_switch_is_removed_not_silently_accepted(self):
        self.assertNotIn("auto_build", VectorRetrievalConfig.model_fields)
        with self.assertRaises(ValidationError):
            VectorRetrievalConfig.model_validate({"auto_build": False})
        self.assertFalse(VectorRetrievalConfig(enabled=False).enabled)

    def test_example_config_loads_without_removed_switch(self):
        root = Path(__file__).resolve().parents[2]
        config = RetrievalConfig.from_sources(root / "config.example.yaml")
        self.assertNotIn("auto_build", config.vector.model_dump())


if __name__ == "__main__":
    unittest.main()
