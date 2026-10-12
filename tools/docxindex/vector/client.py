from __future__ import annotations

import time
import sys
from contextlib import nullcontext
from pathlib import Path
from threading import BoundedSemaphore, Lock

import numpy as _numpy  # noqa: F401 - eagerly load before background response parsing
from pydantic import BaseModel, ConfigDict

from tools.docxindex.llm.config import find_local_env, find_project_config, load_env_file, load_yaml
from tools.docxindex.llm.client import os_env
from loggers.call_log import logical_call, new_call, request_model

EMBEDDING_BATCH_SIZE = 10


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None = None
    api_key: str = "EMPTY"
    timeout_seconds: float = 120.0
    max_input_tokens: int = 8000

    @classmethod
    def from_sources(
        cls,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        config_path: Path | None = None,
    ) -> "EmbeddingSettings":
        local_env = load_env_file(find_local_env())
        config = load_yaml(config_path or find_project_config())
        embedding = config.get("embedding") or {}
        return cls(
            model=(
                model
                or local_env.get("EMBEDDING_MODEL_NAME")
                or os_env("EMBEDDING_MODEL_NAME")
                or embedding.get("name")
                or "text-embedding-v4"
            ),
            base_url=base_url or local_env.get("EMBEDDING_BASE") or os_env("EMBEDDING_BASE") or embedding.get("base_url"),
            api_key=api_key or local_env.get("EMBED_API_KEY") or os_env("EMBED_API_KEY") or "EMPTY",
            timeout_seconds=float(embedding.get("timeout_seconds") or 120.0),
            max_input_tokens=int(embedding.get("max_input_tokens") or 8000),
        )


class EmbeddingClient:
    def __init__(self, settings: EmbeddingSettings, max_concurrency: int = 10):
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
        self._start_interval_seconds = 0.10
        self._rate_limit_retries = 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        result = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[start : start + EMBEDDING_BATCH_SIZE]
            result.extend(self.embed_batch(batch))
        return result

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with logical_call("docxindex.embedding", self.settings.model):
            return self._embed_batch(texts)

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(self._rate_limit_retries + 1):
            record = new_call(component="docxindex.embedding", model=self.settings.model,
                              request={"input": texts, "attempt": attempt + 1})
            self._wait_for_start_slot()
            request_started = False
            try:
                with self._limiter:
                    with _global_model_slot("embedding", "DocxIndexEmbedding", self.settings.model):
                        request_started = True
                        record.sent()
                        response, http_status = request_model(self.client.embeddings, model=self.settings.model, input=texts)
                vectors = [list(item.embedding) for item in response.data]
            except Exception as exc:
                record.failed(exc, stage="request" if request_started else "semaphore_wait")
                if not _is_rate_limit_error(exc) or attempt >= self._rate_limit_retries:
                    exc.call_id = record.reference_id
                    exc.call_path = str(record.reference_path) if record.reference_path else None
                    exc.failure_stage = record.payload["failure_stage"]
                    raise
                time.sleep(2**attempt)
                continue
            record.succeeded({"vector_count": len(vectors), "dimensions": len(vectors[0]) if vectors else 0,
                              "usage": response.usage.model_dump() if response.usage else None},
                             http_status=http_status)
            return vectors
        raise RuntimeError("unreachable embedding retry state")

    def _wait_for_start_slot(self) -> None:
        with self._start_lock:
            now = time.monotonic()
            start_at = max(now, self._next_start)
            self._next_start = start_at + self._start_interval_seconds
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)


def _is_rate_limit_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 429 or "429" in str(exc) or "Throttling.BurstRate" in str(exc)


def _global_model_slot(model_type: str, caller_name: str, model: str):
    if "config" not in sys.modules:
        return nullcontext()
    from endpoints.runtime.llm_semaphore import model_semaphore_sync

    return model_semaphore_sync(model_type, caller_name, model)
