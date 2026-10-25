from __future__ import annotations

from pathlib import Path
import math
import logging

import numpy as _numpy  # noqa: F401 - eagerly load before background response parsing
from openai import AsyncOpenAI
from endpoints.runtime.model_budget import run_sync
from endpoints.runtime.model_calls import execute_model
from pydantic import BaseModel, ConfigDict, Field, model_validator
from config import EMBED_MAX_INPUT_TOKENS

from tools.docxindex.llm.config import find_local_env, find_project_config, load_env_file, load_yaml
from os import getenv as os_env
from loggers.call_log import logical_call, request_model_async

EMBEDDING_BATCH_SIZE = 10


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None = None
    api_key: str = "EMPTY"
    max_input_tokens: int | None = Field(default=None, gt=0, strict=True)
    chunk_overlap_tokens: int = Field(default=32, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_overlap(self):
        if self.max_input_tokens is not None and self.chunk_overlap_tokens >= self.max_input_tokens:
            logging.getLogger(__name__).warning("embedding overlap reaches input limit; slicing will reduce overlap to ensure progress")
        return self

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
            max_input_tokens=embedding.get("max_input_tokens"),
            chunk_overlap_tokens=embedding.get("chunk_overlap_tokens", 32),
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
            return run_sync(embed_request(
                api_key=self.settings.api_key, base_url=self.settings.base_url,
                request={"model": self.settings.model, "input": texts},
                caller="docxindex.embedding", input_limit=self.settings.max_input_tokens))


async def embed_request(*, api_key, base_url, request, caller, default_headers=None,
                        input_limit=EMBED_MAX_INPUT_TOKENS):
    """Send one embedding batch through shared admission, retries and validation."""
    texts = request["input"]

    def validate(response):
        rows = sorted(response.data, key=lambda row: row.index)
        if [row.index for row in rows] != list(range(len(texts))):
            raise ValueError("Embedding response must contain one unique index per input.")
        vectors = [list(row.embedding) for row in rows]
        if any(not vector or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                 or not math.isfinite(value) for value in vector) for vector in vectors):
            raise ValueError("Embedding response must contain finite nonempty vectors.")
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError("Embedding response vector dimensions differ.")
        return vectors

    async def send(remaining):
        timeout = None if remaining is None or not math.isfinite(remaining) else remaining
        async with AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=default_headers,
                               max_retries=0, timeout=timeout) as client:
            return await request_model_async(client.embeddings, **request)

    return await execute_model(model_type="embedding", model=request["model"],
        caller=caller, request=request, send=send, validate=validate, input_limit=input_limit,
        serialize=lambda response: {"usage": response.usage.model_dump() if response.usage else None,
                                    "vector_count": len(response.data)})
