"""LlamaIndex embedding entry points backed by the common model controller."""

from __future__ import annotations

from typing import Any

from llama_index.embeddings.openai_like import OpenAILikeEmbedding

from endpoints.runtime.model_budget import run_sync
from endpoints.runtime.embedding_client import embed_request
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

        with logical_call("llamaindex.embedding", model):
            return await embed_request(api_key=self.api_key, base_url=self.api_base,
                default_headers=self.default_headers, request=request, caller="llamaindex.embedding")

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
