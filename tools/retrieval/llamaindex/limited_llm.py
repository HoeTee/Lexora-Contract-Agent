"""LlamaIndex adapter using the common model controller and LLM transport."""

from __future__ import annotations

from typing import Any, Sequence

from llama_index.core.base.llms.types import ChatMessage, ChatResponse, CompletionResponse
from llama_index.llms.openai.utils import (
    from_openai_completion_logprobs,
    from_openai_message,
    from_openai_token_logprobs,
    to_openai_message_dicts,
)
from llama_index.llms.openai_like import OpenAILike
from openai import AsyncOpenAI

from endpoints.runtime.model_budget import run_sync
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.llm_client import request_llm
from endpoints.runtime.model_http import async_http_client
from loggers.call_log import logical_call


class UnifiedOpenAILike(OpenAILike):
    """Override network paths, retaining LlamaIndex callback conversions.

    llm.stream controls network streaming; the adapter returns complete results.
    Public chunk generators remain disabled to prevent bypassing shared control.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs["max_retries"] = 0
        super().__init__(**kwargs)

    async def _request(self, payload: dict[str, Any], *, chat: bool):
        payload = dict(payload)
        payload.pop("use_chat_completions", None)
        payload.pop("stream", None)
        if any(key in payload for key in ("timeout", "max_retries")):
            raise ValueError("LLM timeout and retries must use the common model controller.")
        payload.pop("stream_options", None)

        async def send(remaining: float):
            async with AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                default_headers=self.default_headers,
                timeout=remaining,
                max_retries=0,
                http_client=async_http_client(remaining),
            ) as client:
                endpoint = client.chat.completions if chat else client.completions
                return await request_llm(endpoint, chat=chat, **payload)

        def validate(response):
            if not response.choices:
                raise ValueError("LLM response contains no choices.")
            if chat:
                message = response.choices[0].message
                if message is None or (message.content is None and not message.tool_calls):
                    raise ValueError("LLM response contains neither text nor tool calls.")
            elif response.choices[0].text is None:
                raise ValueError("Completion response contains no text.")
            return response

        with logical_call("llamaindex.llm", self.model):
            return await execute_model(
                model_type="llm",
                model=self.model,
                caller="llamaindex.llm",
                request=payload,
                send=send,
                serialize=lambda value: value.model_dump(mode="json"),
                validate=validate,
            )

    def _chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        return run_sync(self._achat(messages, **kwargs))

    async def _achat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        payload = self._get_model_kwargs(**kwargs)
        payload["messages"] = to_openai_message_dicts(messages, model=self.model)
        response = await self._request(payload, chat=True)
        choice = response.choices[0]
        probabilities = choice.logprobs
        logprobs = (
            from_openai_token_logprobs(probabilities.content) if probabilities and probabilities.content else None
        )
        return ChatResponse(
            message=from_openai_message(choice.message, modalities=self.modalities or ["text"]),
            raw=response,
            logprobs=logprobs,
            additional_kwargs=self._get_response_token_counts(response),
        )

    def _complete(self, prompt: str, **kwargs: Any) -> CompletionResponse:
        return run_sync(self._acomplete(prompt, **kwargs))

    async def _acomplete(self, prompt: str, **kwargs: Any) -> CompletionResponse:
        payload = self._get_model_kwargs(**kwargs)
        self._update_max_tokens(payload, prompt)
        payload["prompt"] = prompt
        response = await self._request(payload, chat=False)
        choice = response.choices[0]
        return CompletionResponse(
            text=choice.text,
            raw=response,
            logprobs=from_openai_completion_logprobs(choice.logprobs) if choice.logprobs else None,
            additional_kwargs=self._get_response_token_counts(response),
        )

    def stream_chat(self, messages: Sequence[ChatMessage], **kwargs: Any):
        raise NotImplementedError("Streaming is not used by contract retrieval; use chat or achat.")

    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any):
        raise NotImplementedError("Streaming is not used by contract retrieval; use complete or acomplete.")

    async def astream_chat(self, messages: Sequence[ChatMessage], **kwargs: Any):
        raise NotImplementedError("Streaming is not used by contract retrieval; use chat or achat.")

    async def astream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any):
        raise NotImplementedError("Streaming is not used by contract retrieval; use complete or acomplete.")

    _stream_chat = stream_chat
    _stream_complete = stream_complete
    _astream_chat = astream_chat
    _astream_complete = astream_complete
