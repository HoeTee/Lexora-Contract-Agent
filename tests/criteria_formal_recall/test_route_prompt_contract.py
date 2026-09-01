from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from endpoints.runtime.errors import classify_model_call_error
from tools.docxindex.llm.client import LLMClient, LLMResponseValidationError
from tools.docxindex.llm.prompts import ATTACHMENT_HIERARCHY_PROMPT_VERSION, ROUTE_EXPECTED_JSON, route_prompt
from tools.docxindex.llm.schemas import RouteResponse
from tools.docxindex.retriever import DocxIndexRetriever
from tools.docxindex.retrieval.route import ROUTE_PROMPT_VERSION, RouteConfig, plan_route


METHODS = {
    "title": "检查标题。",
    "region": "检查区域。",
    "rule": "执行精确匹配。",
    "scan": "扫描全文。",
    "hybrid": "执行语义检索。",
}


class RoutePromptContractTests(unittest.TestCase):
    def test_expected_json_matches_route_response_schema(self) -> None:
        validated = RouteResponse.model_validate(json.loads(ROUTE_EXPECTED_JSON))

        self.assertEqual(validated.steps[0].method, "rule")
        self.assertFalse(validated.fallback)

    def test_initial_prompt_contains_complete_output_contract(self) -> None:
        prompt = route_prompt(
            "检查合同金额",
            "合同金额应明确",
            [{"title": "合同金额", "node_type": "section", "depth": 1, "region": "body"}],
            METHODS,
        )

        self.assertIn(ROUTE_EXPECTED_JSON, prompt)
        for field in ("steps", "method", "terms", "regions", "all_titles", "fallback"):
            self.assertIn(field, prompt)
        self.assertNotIn('"context"', ROUTE_EXPECTED_JSON)
        for method in METHODS:
            self.assertIn(method, prompt)
        self.assertIn("1 到 5 个路由步骤", prompt)
        self.assertIn("不要返回 Markdown", prompt)

    def test_plan_route_uses_contract_prompt_on_first_call(self) -> None:
        client = _RecordingClient()
        config = RouteConfig(criteria=Path("unused.docx"), methods=METHODS)

        result = plan_route(
            {"structure_tree": []},
            "检查合同金额",
            client,
            config,
            cache_dir=None,
            criterion_text="合同金额应明确",
        )

        self.assertEqual(result.steps[0].method, "rule")
        self.assertEqual(len(client.prompts), 1)
        self.assertIn(ROUTE_EXPECTED_JSON, client.prompts[0])
        self.assertEqual(ROUTE_PROMPT_VERSION, "docx_route_v7")

    def test_schema_rejects_removed_join_route_and_slots_field(self) -> None:
        with self.assertRaises(ValidationError):
            RouteResponse.model_validate({
                "steps": [{"method": "join", "slots": ["正文", "附件"]}],
                "fallback": False,
            })

    def test_invalid_first_output_gets_schema_guided_retry(self) -> None:
        client = LLMClient.__new__(LLMClient)
        responses = iter([
            '{"steps":[{"method":"unsupported"}],"fallback":false}',
            ROUTE_EXPECTED_JSON,
        ])
        prompts: list[str] = []

        def complete(prompt: str) -> str:
            prompts.append(prompt)
            return next(responses)

        client.complete = complete  # type: ignore[method-assign]
        result = client.complete_model("首次提示包含期望 JSON", RouteResponse, retries=1)

        self.assertEqual(result.steps[0].method, "rule")
        self.assertEqual(len(prompts), 2)
        self.assertIn("JSON 必须符合以下 schema", prompts[1])
        self.assertIn("unsupported", prompts[1])

    def test_schema_rejects_contract_fields_outside_expected_json(self) -> None:
        invalid = json.loads(ROUTE_EXPECTED_JSON)
        invalid["steps"][0]["reason"] = "不属于执行契约"

        with self.assertRaises(ValidationError):
            RouteResponse.model_validate(invalid)

    def test_schema_rejects_removed_context_field(self) -> None:
        invalid = json.loads(ROUTE_EXPECTED_JSON)
        invalid["steps"][0]["context"] = 1

        with self.assertRaises(ValidationError):
            RouteResponse.model_validate(invalid)

    def test_schema_rejects_fields_that_do_not_belong_to_method(self) -> None:
        invalid_plans = [
            {"steps": [{"method": "rule", "terms": ["金额"], "regions": ["body"]}]},
            {"steps": [{"method": "region", "regions": ["body", "attachments"]}]},
            {"steps": [{"method": "scan"}, {"method": "region", "regions": ["body"]}]},
            {"steps": [{"method": "scan"}, {"method": "hybrid"}]},
            {"steps": [{"method": "scan"}, {"method": "scan"}]},
            {"steps": [{"method": "hybrid"}], "fallback": True},
        ]

        for plan in invalid_plans:
            with self.subTest(plan=plan), self.assertRaises(ValidationError):
                RouteResponse.model_validate(plan)

    def test_schema_accepts_title_rule_and_scan_for_one_mixed_criterion(self) -> None:
        result = RouteResponse.model_validate(
            {
                "steps": [
                    {"method": "title", "all_titles": True},
                    {"method": "rule", "terms": ["分批支付", "人民币"]},
                    {"method": "scan"},
                ],
                "fallback": False,
            }
        )

        self.assertEqual([step.method for step in result.steps], ["title", "rule", "scan"])

    def test_validation_failure_preserves_successful_upstream_http_status(self) -> None:
        client = LLMClient.__new__(LLMClient)
        client.complete = lambda _prompt: '{"steps":[{"method":"scan"},{"method":"hybrid"}]}'  # type: ignore[method-assign]

        with self.assertRaises(LLMResponseValidationError) as raised:
            client.complete_model("route prompt", RouteResponse, retries=0)

        self.assertEqual(raised.exception.http_status, 200)
        self.assertEqual(raised.exception.error_type, "response_validation_error")
        self.assertIn("scan route may only be combined", str(raised.exception))

    def test_retrieval_error_fields_survive_model_error_classification(self) -> None:
        error = classify_model_call_error(
            "Error in contract DocxIndex search: http_status=200; "
            "error_type=response_validation_error; detail=invalid route response"
        )

        payload = error.as_task_error()
        self.assertEqual(payload["http_status"], 200)
        self.assertEqual(payload["error_type"], "response_validation_error")
        self.assertIn("invalid route response", payload["detail"])
        self.assertIn("响应格式校验失败", payload["message"])

    def test_index_build_error_preserves_http_status_and_error_type(self) -> None:
        retriever = DocxIndexRetriever.__new__(DocxIndexRetriever)

        def fail_build(_path: Path) -> dict:
            raise LLMResponseValidationError(
                "invalid attachment hierarchy",
                http_status=200,
                error_type="response_validation_error",
            )

        retriever._build_index_sync = fail_build  # type: ignore[method-assign]
        result = json.loads(asyncio.run(retriever.build_index("D02.docx")))

        self.assertEqual(result["error"]["http_status"], 200)
        self.assertEqual(result["error"]["error_type"], "response_validation_error")
        self.assertIn("invalid attachment hierarchy", result["error"]["detail"])
        self.assertEqual(ATTACHMENT_HIERARCHY_PROMPT_VERSION, "local_hierarchy_v4")

    def test_failed_validation_is_not_written_to_route_cache(self) -> None:
        client = _RejectingClient()
        config = RouteConfig(criteria=Path("unused.docx"), methods=METHODS)

        with TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "invalid route output"):
                plan_route(
                    {"structure_tree": []},
                    "检查合同金额",
                    client,
                    config,
                    cache_dir=cache_dir,
                    criterion_text="合同金额应明确",
                )

            self.assertFalse((cache_dir / "route.jsonl").exists())


class _Settings:
    model = "test-model"


class _RecordingClient:
    settings = _Settings()

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete_model(self, prompt: str, schema: type[RouteResponse]) -> RouteResponse:
        self.prompts.append(prompt)
        return schema.model_validate(json.loads(ROUTE_EXPECTED_JSON))


class _RejectingClient:
    settings = _Settings()

    def complete_model(self, prompt: str, schema: type[RouteResponse]) -> RouteResponse:
        del prompt, schema
        raise ValueError("invalid route output")


if __name__ == "__main__":
    unittest.main()
