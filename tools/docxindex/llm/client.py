from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, TypeVar

from openai import AsyncOpenAI
from endpoints.runtime.model_budget import run_sync
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.model_http import async_http_client
from pydantic import BaseModel, ConfigDict, ValidationError
from loggers.call_log import logical_call, request_model_async

from .config import env_api_key, find_local_env, find_project_config, load_env_file, load_yaml




class LLMClientError(RuntimeError):
    """Preserve upstream HTTP and exception details across retrieval boundaries."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None,
        error_type: str,
    ) -> None:
        self.http_status = http_status
        self.error_type = error_type
        super().__init__(message)


class LLMResponseValidationError(ValueError):
    """Raised when an HTTP-successful model response violates the output schema."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None,
        error_type: str,
    ) -> None:
        self.http_status = http_status
        self.error_type = error_type
        super().__init__(message)


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None = None
    api_key: str = "EMPTY"
    temperature: float = 0.0
    enable_thinking: bool | None = None

    @classmethod
    def from_sources(
        cls,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        config_path: Path | None = None,
    ) -> "LLMSettings":
        local_env = load_env_file(find_local_env())
        config = load_yaml(config_path or find_project_config())
        llm = config.get("llm") or {}
        return cls(
            model=model or local_env.get("LLM_MODEL_NAME") or os_env("LLM_MODEL_NAME") or llm.get("name") or "qwen3.6-35b-a3b",
            base_url=base_url or local_env.get("LLM_BASE") or os_env("LLM_BASE") or llm.get("base_url"),
            api_key=api_key or local_env.get("LLM_API_KEY") or env_api_key(),
            temperature=float(llm.get("temperature", 0.0)),
            enable_thinking=llm.get("enable_thinking", False),
        )


class LLMClient:
    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def complete(self, prompt: str) -> str:
        return self._execute(prompt)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        return self._execute(prompt, validate=extract_json_object)

    def complete_model(self, prompt: str, schema: type["TModel"], *, validate=None) -> "TModel":
        def check(content):
            parsed = schema.model_validate(extract_json_object(content))
            return validate(parsed) if validate else parsed
        return self._execute(prompt, validate=check, schema=schema)

    def _execute(self, prompt, *, validate=None, schema=None):
        current_prompt = prompt
        request = {"messages": [{"role": "user", "content": prompt}]}
        async def send(remaining):
            async with AsyncOpenAI(api_key=self.settings.api_key, base_url=self.settings.base_url,
                                   max_retries=0, timeout=remaining, http_client=async_http_client(remaining)) as client:
                kwargs = dict(model=self.settings.model, messages=[{"role": "user", "content": current_prompt}],
                              temperature=self.settings.temperature)
                if self.settings.enable_thinking is not None:
                    kwargs["extra_body"] = {"enable_thinking": self.settings.enable_thinking}
                return await request_model_async(client.chat.completions, **kwargs)

        def check(response):
            nonlocal current_prompt
            content = response.choices[0].message.content or ""
            try:
                return validate(content) if validate else content
            except (ValueError, ValidationError) as exc:
                if schema:
                    current_prompt = retry_prompt(prompt, schema, content, exc)
                    request["messages"] = [{"role": "user", "content": current_prompt}]
                raise

        with logical_call("docxindex.llm", self.settings.model):
            try:
                return run_sync(execute_model(model_type="llm", model=self.settings.model,
                    caller="docxindex.llm", request=request, send=send, validate=check,
                    serialize=lambda response: response.model_dump()))
            except Exception as exc:
                error_type = "response_validation_error" if isinstance(exc, ValueError) else type(exc).__name__
                cls = LLMResponseValidationError if isinstance(exc, ValueError) else LLMClientError
                error = cls(str(exc), http_status=getattr(exc, "http_status", getattr(exc, "status_code", None)), error_type=error_type)
                for field in ("call_id", "call_path", "failure_stage"):
                    setattr(error, field, getattr(exc, field, None))
                raise error from exc


def extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if "```" in text:
        text = re.sub(r"^.*?```(?:json)?\s*", "", text, flags=re.S)
        text = text.split("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"LLM response does not contain a JSON object: {content[:200]!r}")
    return json.loads(text[start : end + 1])


TModel = TypeVar("TModel", bound=BaseModel)


def retry_prompt(original_prompt: str, schema: type[BaseModel], previous_content: str, error: Exception) -> str:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)
    return f"""{original_prompt}

上一次输出不符合要求，必须重新生成。

错误信息：
{error}

上一次输出：
{previous_content[:2000]}

必须严格返回一个合法 JSON object，不要返回 Markdown，不要返回解释，不要返回纯文本。
JSON 必须符合以下 schema：
{schema_json}
"""


def os_env(name: str) -> str | None:
    import os

    return os.getenv(name)
