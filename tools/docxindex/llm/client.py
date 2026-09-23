from __future__ import annotations

import json
import re
import sys
import time
from contextlib import nullcontext
from contextvars import ContextVar
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError
from redis.exceptions import RedisError
from loggers.call_log import new_call

from .config import env_api_key, find_local_env, find_project_config, load_env_file, load_yaml


_LAST_CALL: ContextVar[tuple[str, str | None] | None] = ContextVar("docxindex_last_call", default=None)


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
    timeout_seconds: float = 120.0
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
    def __init__(self, settings: LLMSettings, max_concurrency: int = 10):
        from openai import OpenAI

        self.settings = settings
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=0,
        )
        self._limiter = BoundedSemaphore(max(1, max_concurrency))
        self._start_lock = Lock()
        self._next_start = 0.0
        self._start_interval_seconds = 0.15
        self._rate_limit_retries = 4

    def complete(self, prompt: str) -> str:
        extra_body = {}
        if self.settings.enable_thinking is not None:
            extra_body["enable_thinking"] = self.settings.enable_thinking
        for attempt in range(self._rate_limit_retries + 1):
            record = new_call(component="docxindex.llm", model=self.settings.model,
                              request={"messages": [{"role": "user", "content": prompt}],
                                       "temperature": self.settings.temperature,
                                       "extra_body": extra_body or None, "attempt": attempt + 1})
            self._wait_for_start_slot()
            try:
                with self._limiter:
                    with _global_model_slot("llm", "DocxIndexLLM", self.settings.model):
                        response = self.client.chat.completions.create(
                            model=self.settings.model,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=self.settings.temperature,
                            extra_body=extra_body or None,
                        )
                content = response.choices[0].message.content or ""
            except Exception as exc:
                record.failed(exc, stage="semaphore_wait" if isinstance(exc, RedisError) or type(exc).__name__ == "LLMGlobalSemaphoreTimeout" else "request")
                if not _is_rate_limit_error(exc) or attempt >= self._rate_limit_retries:
                    error = LLMClientError(
                        str(exc),
                        http_status=getattr(exc, "status_code", None),
                        error_type=type(exc).__name__,
                    )
                    error.call_id = record.call_id
                    error.call_path = str(record.path) if record.path else None
                    error.failure_stage = record.payload["failure_stage"]
                    raise error from exc
                time.sleep(2**attempt)
                continue
            record.succeeded({"content": content, "usage": response.usage.model_dump() if response.usage else None})
            _LAST_CALL.set((record.call_id, str(record.path) if record.path else None))
            return content
        raise RuntimeError("unreachable LLM retry state")

    def _wait_for_start_slot(self) -> None:
        with self._start_lock:
            now = time.monotonic()
            start_at = max(now, self._next_start)
            self._next_start = start_at + self._start_interval_seconds
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        return extract_json_object(self.complete(prompt))

    def complete_model(self, prompt: str, schema: type["TModel"], retries: int = 2) -> "TModel":
        current_prompt = prompt
        last_error: Exception | None = None
        last_content = ""
        for _ in range(retries + 1):
            last_content = self.complete(current_prompt)
            try:
                payload = extract_json_object(last_content)
                return schema.model_validate(payload)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
                current_prompt = retry_prompt(prompt, schema, last_content, exc)
        error = LLMResponseValidationError(
            f"LLM response failed {schema.__name__} validation after {retries + 1} attempts: "
            f"{last_error}; last_content={last_content[:2000]!r}",
            http_status=200,
            error_type="response_validation_error",
        )
        last_call = _LAST_CALL.get()
        if last_call:
            error.call_id, error.call_path = last_call
        error.failure_stage = "output_validation"
        raise error


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


def _is_rate_limit_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 429 or "429" in str(exc) or "Throttling.BurstRate" in str(exc)


def _global_model_slot(model_type: str, caller_name: str, model: str):
    if "config" not in sys.modules:
        return nullcontext()
    from endpoints.runtime.llm_semaphore import model_semaphore_sync

    return model_semaphore_sync(model_type, caller_name, model)


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
