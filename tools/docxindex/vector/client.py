from __future__ import annotations

from pathlib import Path

import numpy as _numpy  # noqa: F401 - eagerly load before background response parsing
from openai import AsyncOpenAI
from endpoints.runtime.model_budget import run_sync
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.model_http import async_http_client
from pydantic import BaseModel, ConfigDict

from tools.docxindex.llm.config import find_local_env, find_project_config, load_env_file, load_yaml
from tools.docxindex.llm.client import os_env
from loggers.call_log import logical_call, request_model_async

EMBEDDING_BATCH_SIZE = 10


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None = None
    api_key: str = "EMPTY"
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
            max_input_tokens=int(embedding.get("max_input_tokens") or 8000),
        )


class EmbeddingClient:
    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings

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
        async def send(remaining):
            async with AsyncOpenAI(api_key=self.settings.api_key, base_url=self.settings.base_url,
                                   max_retries=0, timeout=remaining, http_client=async_http_client(remaining)) as client:
                return await request_model_async(client.embeddings, model=self.settings.model, input=texts)
        return run_sync(execute_model(model_type="embedding", model=self.settings.model,
            caller="docxindex.embedding", request={"input": texts}, send=send,
            validate=lambda response: [list(item.embedding) for item in response.data],
            serialize=lambda response: {"usage": response.usage.model_dump() if response.usage else None,
                                        "vector_count": len(response.data)}))
