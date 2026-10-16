"""LlamaIndex embedding entry points backed by the common model controller."""

from __future__ import annotations

import math
from typing import Any

from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from openai import AsyncOpenAI

from endpoints.runtime.model_budget import run_sync
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.model_http import async_http_client
from loggers.call_log import logical_call


class SemaphoreOpenAILikeEmbedding(OpenAILikeEmbedding):
    """Keep the adapter name; scheduling and retries live in execute_model.

    Synchronous callers wait for cancellable async HTTP through run_sync, rather
    than performing uninterruptible synchronous HTTP in a worker thread.
    """

    def __init__(self, model_name: str, **kwargs: Any) -> None:
        kwargs["max_retries"] = 0
        super().__init__(model_name=model_name, **kwargs)

    async def _embed(self, texts: list[str], model: str) -> list[list[float]]:
        if not texts:
            return []
        request = {
            "model": model,
            "input": [text.replace("\n", " ") for text in texts],
            "encoding_format": "float",
            **self.additional_kwargs,
        }
        if any(key in request for key in ("timeout", "max_retries")):
            raise ValueError("Embedding timeout and retries must use the common model controller.")

        async def send(remaining: float):
            async with AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                default_headers=self.default_headers,
                timeout=remaining,
                max_retries=0,
                http_client=async_http_client(remaining),
            ) as client:
                raw = await client.embeddings.with_raw_response.create(**request)
                return raw.parse(), raw.status_code

        def validate(response):
            rows = sorted(response.data, key=lambda item: item.index)
            if [row.index for row in rows] != list(range(len(texts))):
                raise ValueError("Embedding response must contain one unique index per input.")
            vectors = [row.embedding for row in rows]
            if any(
                not vector
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in vector
                )
                for vector in vectors
            ):
                raise ValueError("Embedding response must contain finite numeric vectors.")
            if len({len(vector) for vector in vectors}) != 1:
                raise ValueError("Embedding vectors must have matching dimensions.")
            return vectors

        with logical_call("llamaindex.embedding", model):
            return await execute_model(
                model_type="embedding",
                model=model,
                caller="llamaindex.embedding",
                request=request,
                send=send,
                serialize=lambda value: {
                    "usage": value.usage.model_dump() if value.usage else None,
                    "vector_count": len(value.data),
                },
                validate=validate,
            )

    def _get_query_embedding(self, query: str) -> list[float]:
        return run_sync(self._embed([query], self._query_engine))[0]

    def _get_text_embedding(self, text: str) -> list[float]:
        return run_sync(self._embed([text], self._text_engine))[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return run_sync(self._embed(texts, self._text_engine))

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return (await self._embed([query], self._query_engine))[0]

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return (await self._embed([text], self._text_engine))[0]

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, self._text_engine)
