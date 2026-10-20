"""Dedicated rerank protocol shared by DocxIndex and the LlamaIndex adapter."""
from __future__ import annotations

import math
import json
from dataclasses import dataclass
from pathlib import Path


import httpx

from config import RERANK_API_KEY, RERANK_BASE_URL, RERANK_INSTRUCT, RERANK_NAME, RERANK_MAX_INPUT_TOKENS, infer_rerank_provider
from endpoints.runtime.model_budget import run_sync
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.model_http import async_http_client
from loggers.call_log import logical_call
from tools.docxindex.llm.config import load_yaml
from tools.docxindex.model_input import ModelInputTooLong, model_tokenizer


def build_payload(provider, model, instruct, query, documents, top_n):
    if provider == "bge":
        return {"model": model, "query": query, "documents": documents, "top_n": top_n}
    if provider == "dashscope_qwen":
        result = {"model": model, "input": {"query": query, "documents": documents},
                  "parameters": {"return_documents": False, "top_n": top_n}}
    elif provider == "higress_qwen":
        result = {"model": model, "query": query, "documents": documents, "top_n": top_n}
    else:
        raise ValueError(f"Unsupported rerank provider: {provider}")
    if instruct:
        result["instruct"] = instruct
    return result


def parse_results(body, document_count, top_n):
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        output = body.get("output")
        items = body.get("results", output.get("results") if isinstance(output, dict) else None)
    else:
        items = None
    if not isinstance(items, list) or len(items) != top_n:
        raise ValueError("Reranker must return the requested number of results.")
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Reranker result must be an object.")
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < document_count or index in seen:
            raise ValueError("Reranker returned a duplicate or invalid document index.")
        if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
            raise ValueError("Reranker returned an invalid relevance score.")
        seen.add(index)
        result.append((index, float(score)))
    return sorted(result, key=lambda pair: pair[1], reverse=True)


def response_payload(text: str) -> dict:
    try:
        body = json.loads(text)
    except ValueError:
        return {"body": text}
    return body if isinstance(body, dict) else {"body": body}


@dataclass
class RerankerClient:
    model: str = RERANK_NAME
    base_url: str = RERANK_BASE_URL
    api_key: str = RERANK_API_KEY
    instruct: str = RERANK_INSTRUCT
    provider: str | None = None
    max_input_tokens: int = RERANK_MAX_INPUT_TOKENS

    @classmethod
    def from_sources(cls, config_path: Path, *, model=None, api_key=None, base_url=None):
        # The MCP process resolves request-scoped credentials before construction.
        settings = load_yaml(config_path).get("rerank") or {}
        if "chunk_overlap_tokens" in settings:
            raise ValueError("Remove rerank.chunk_overlap_tokens; reranker input preparation has no overlap.")
        return cls(model=model or RERANK_NAME, api_key=api_key or RERANK_API_KEY,
                   base_url=base_url or RERANK_BASE_URL, instruct=settings.get("instruct", RERANK_INSTRUCT),
                   max_input_tokens=settings.get("max_input_tokens", RERANK_MAX_INPUT_TOKENS))

    def rank(self, query: str, documents: list[str], top_n: int | None = None):
        if not documents:
            return []
        keep = min(top_n or len(documents), len(documents))
        provider = self.provider or infer_rerank_provider(self.model)
        tokenizer = model_tokenizer(self.model)
        limit = tokenizer.effective_limit(self.max_input_tokens)
        counts = [tokenizer.count_rerank(query, document, "" if provider == "bge" else self.instruct)
                  for document in documents]
        for index, count in enumerate(counts):
            if count > limit:
                raise ModelInputTooLong(f"Reranker input {index} contains {count} tokens; limit={limit} "
                                        f"(configured={self.max_input_tokens}, tokenizer={tokenizer.identity}).")
        payload = build_payload(provider, self.model,
                                self.instruct, query, documents, keep)
        async def send(remaining):
            headers = {}
            if self.api_key:
                headers = {"X-API-Key": self.api_key} if provider == "bge" else {"Authorization": f"Bearer {self.api_key}"}
            async with async_http_client(remaining) as client:
                response = await client.post(self.base_url, json=payload, headers=headers)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise httpx.HTTPStatusError(f"{exc}\nResponse body: {response.text[:4096]}",
                                                request=exc.request, response=exc.response) from exc
                return response.text, response.status_code

        with logical_call("docxindex.reranker", self.model):
            return run_sync(execute_model(model_type="reranker", model=self.model,
                caller="docxindex.reranker", request={**payload, "input_control": {
                    "configured_input_tokens": self.max_input_tokens, "effective_input_tokens": limit,
                    "input_token_counts": counts, "tokenizer": tokenizer.identity}}, send=send,
                validate=lambda body: parse_results(json.loads(body), len(documents), keep),
                serialize=response_payload))
