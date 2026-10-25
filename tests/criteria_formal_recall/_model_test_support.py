"""Offline SDK transports for tests of the shared model request controller."""

from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI


@asynccontextmanager
async def available_slot(*args, **kwargs):
    yield SimpleNamespace(key="test:model:semaphore")


@contextmanager
def model_transport(module, handler):
    module = {"tools.docxindex.llm.client": "endpoints.runtime.llm_client",
              "tools.docxindex.vector.client": "endpoints.runtime.embedding_client"}.get(module, module)
    def factory(**kwargs):
        return AsyncOpenAI(api_key="fake", base_url="https://model.test/v1", max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with patch(f"{module}.AsyncOpenAI", side_effect=factory), \
         patch("endpoints.runtime.model_calls.model_semaphore", available_slot), \
         patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
        yield


@contextmanager
def reranker_transport(handler):
    client_type = httpx.AsyncClient

    def factory(timeout):
        return client_type(transport=httpx.MockTransport(handler), timeout=timeout)

    with patch("endpoints.runtime.reranker_client.httpx.AsyncClient", side_effect=factory), \
         patch("endpoints.runtime.model_calls.model_semaphore", available_slot), \
         patch("endpoints.runtime.model_calls._retry_delay", return_value=0):
        yield
