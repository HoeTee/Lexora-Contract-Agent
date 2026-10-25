"""Offline model token counting shared by every outbound model adapter.

Tokenizer selection is explicit deployment configuration. Tokenizers count input;
they never impose a second, hidden context-capacity limit.
"""
from __future__ import annotations

from copy import deepcopy
import base64
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import gzip
import importlib.util
import json
from pathlib import Path
from typing import Any

import tiktoken

from endpoints.runtime.errors import ModelInputTooLong


TOKENIZER_ROOT = Path(__file__).resolve().parents[2] / "resources" / "tokenizers"


@lru_cache(maxsize=1)
def _fallback_encoder():
    """Load the packaged cl100k table directly; never use a download fallback."""
    data = gzip.decompress((TOKENIZER_ROOT / "cl100k_base.tiktoken.gz").read_bytes())
    if sha256(data).hexdigest() != "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7":
        raise RuntimeError("Offline cl100k_base tokenizer checksum mismatch")
    ranks = {base64.b64decode(token): int(rank) for line in data.splitlines()
             if line for token, rank in [line.split()]}
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s",
        mergeable_ranks=ranks,
        special_tokens={"<|endoftext|>": 100257, "<|fim_prefix|>": 100258, "<|fim_middle|>": 100259,
                        "<|fim_suffix|>": 100260, "<|endofprompt|>": 100276},
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _model_mapping() -> dict[str, str]:
    # Configuration imports this module for resource validation. Read the mapping
    # only when counting, after config has finished constructing its constants.
    from config import TOKENIZER_MODEL_NAMES

    return {
        name.strip().casefold(): slug
        for slug, names in TOKENIZER_MODEL_NAMES.items()
        for name in names
    }


def validate_tokenizer_resources(slugs: Any) -> None:
    """Verify each configured native resource before accepting requests."""
    for slug in slugs:
        if slug == "cl100k_base":
            _fallback_encoder()
        else:
            _native_resource(slug)


@lru_cache(maxsize=16)
def _native_resource(slug: str) -> tuple[dict, Path]:
    manifest_file = TOKENIZER_ROOT / "manifest.json"
    if not manifest_file.is_file():
        raise RuntimeError("Offline tokenizer resources are missing; run scripts/prepare_tokenizers.py before deployment.")
    entry = json.loads(manifest_file.read_text(encoding="utf-8"))["models"].get(slug)
    if entry is None:
        raise ValueError(f"Unknown offline tokenizer ID: {slug}")
    directory = TOKENIZER_ROOT / slug
    for name, record in entry["files"].items():
        path = directory / name
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError(f"Offline tokenizer file is missing or corrupt: {slug}/{name}")
    return entry, directory


@lru_cache(maxsize=16)
def _native_encoder(slug: str):
    _, directory = _native_resource(slug)
    # Transformers is heavyweight and unnecessary for cl100k-only deployments.
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(directory, local_files_only=True, use_fast=True,
                                         trust_remote_code=False)


@lru_cache(maxsize=1)
def _deepseek_encoding():
    _, directory = _native_resource("deepseek-v4-flash-0731")
    # Execute only the pinned, checksum-verified reference implementation shipped
    # in the image. No remote tokenizer code or runtime download is permitted.
    spec = importlib.util.spec_from_file_location(
        "contract_agent_deepseek_0731_encoding", directory / "encoding" / "encoding_dsv4.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("DeepSeek 0731 message encoder is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelTokenizer:
    def __init__(self, model: str, slug: str, *, fallback: bool = False) -> None:
        self.model = model
        self.slug = slug
        self.native = slug != "cl100k_base"
        self.match = "other" if fallback else "model_name"
        if self.native:
            entry, _ = _native_resource(slug)
            self.encoder = _native_encoder(slug)
            self.identity = f"{slug}:{entry['revision']}"
        else:
            self.encoder = _fallback_encoder()
            self.identity = "cl100k_base"

    @property
    def metadata(self) -> dict:
        return {"tokenizer_id": self.slug, "tokenizer": self.identity,
                "tokenizer_match": self.match, "estimated": not self.native}

    def effective_limit(self, configured: int | None) -> int | None:
        """Return only the configured limit; tokenizer capacity is not a cap."""
        if configured is not None and (
            isinstance(configured, bool) or not isinstance(configured, int) or configured <= 0
        ):
            raise ValueError("max_input_tokens must be a positive integer or null")
        return configured

    def count_text(self, text: str) -> int:
        if self.native:
            return len(self.encoder.encode(text, add_special_tokens=False, truncation=False, verbose=False))
        return len(self.encoder.encode(text, disallowed_special=()))

    def count_embedding(self, text: str) -> int:
        if self.native:
            return len(self.encoder.encode(text, add_special_tokens=True, truncation=False, verbose=False))
        return self.count_text(text)

    def count_rerank(self, query: str, document: str, instruct: str = "") -> int:
        if self.slug == "bge-reranker-v2-m3":
            return len(self.encoder.encode(query, text_pair=document, add_special_tokens=True,
                                           truncation=False, verbose=False))
        if self.slug == "qwen3-reranker":
            instruction = instruct or "Given a web search query, retrieve relevant passages that answer the query"
            prefix = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the '
                      'Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
            suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
            return self.count_text(prefix + f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}" + suffix)
        return self.count_text(query) + self.count_text(document) + self.count_text(instruct) + 32

    def count_chat(self, messages: list[dict], tools: list | None = None, **options) -> int:
        if self.slug == "deepseek-v4-flash-0731":
            prepared = deepcopy(messages)
            if tools or options.get("response_format"):
                if not prepared or prepared[0].get("role") != "system":
                    prepared.insert(0, {"role": "system", "content": ""})
                if tools:
                    prepared[0]["tools"] = tools
                if options.get("response_format"):
                    prepared[0]["response_format"] = options["response_format"]
            prompt = _deepseek_encoding().encode_messages(
                prepared, thinking_mode="chat" if options.get("enable_thinking") is False else "thinking",
                reasoning_effort=options.get("reasoning_effort"),
            )
            return self.count_text(prompt)
        if self.native and self.encoder.chat_template:
            prepared = deepcopy(messages)
            # Qwen's official template consumes argument mappings, whereas the
            # OpenAI wire protocol represents function arguments as JSON strings.
            for message in prepared:
                for call in message.get("tool_calls") or []:
                    function = call.get("function", call)
                    if isinstance(function.get("arguments"), str):
                        try:
                            function["arguments"] = json.loads(function["arguments"])
                        except (ValueError, TypeError):
                            function["arguments"] = {"arguments": function["arguments"]}
            rendered = self.encoder.apply_chat_template(
                prepared, tools=tools or None, tokenize=False, add_generation_prompt=True,
                **{key: value for key, value in options.items() if value is not None},
            )
            return self.count_text(rendered)
        # Unknown-provider chat wrapping cannot be reconstructed exactly. Count
        # the complete serialized request with explicit conservative overhead.
        return self.count_text(_json({"messages": messages, "tools": tools or [], **options})) + len(messages) * 16 + 64


@lru_cache(maxsize=64)
def _selected_tokenizer(model: str, slug: str, fallback: bool) -> ModelTokenizer:
    return ModelTokenizer(model, slug, fallback=fallback)


def model_tokenizer(model: str) -> ModelTokenizer:
    mapping = _model_mapping()
    normalized = model.strip().casefold()
    fallback = normalized not in mapping or normalized == "other"
    slug = mapping.get(normalized) if not fallback else mapping.get("other")
    if slug is None:
        raise ValueError("tokenizers must explicitly configure cl100k_base: [other]")
    return _selected_tokenizer(model, slug, fallback)


model_tokenizer.cache_clear = _selected_tokenizer.cache_clear


@dataclass(frozen=True)
class ModelInputCount:
    input_tokens: int
    input_token_counts: list[int]
    metadata: dict


def count_model_input(model_type: str, model: str, request: dict) -> ModelInputCount:
    """Count complete LLM input or each embedding/rerank item before sending."""
    tokenizer = model_tokenizer(model)
    if model_type == "llm":
        messages = request.get("messages")
        if messages is None:
            prompts = request.get("prompt", "")
            if isinstance(prompts, str) or prompts and isinstance(prompts[0], int):
                prompts = [prompts]
            counts = [tokenizer.count_text(prompt) if isinstance(prompt, str) else len(prompt) for prompt in prompts]
        else:
            extra = request.get("extra_body") or {}
            counts = [tokenizer.count_chat(
                messages, request.get("tools"), enable_thinking=extra.get("enable_thinking"),
                reasoning_effort=request.get("reasoning_effort"), response_format=request.get("response_format"),
            )]
    elif model_type == "embedding":
        values = request.get("input", request.get("texts", []))
        if isinstance(values, dict):
            values = values.get("texts", values.get("input", []))
        values = [values] if isinstance(values, str) else values
        # OpenAI also accepts already-tokenized IDs. Their length is the input count.
        if values and isinstance(values[0], int):
            values = [values]
        counts = [len(value) if isinstance(value, list) else tokenizer.count_embedding(value) for value in values]
    elif model_type in {"rerank", "reranker"}:
        payload = request.get("input") if isinstance(request.get("input"), dict) else request
        query = str(payload.get("query", ""))
        instruct = str(payload.get("instruct", request.get("instruct", "")) or "")
        documents = payload.get("documents", [])
        counts = [tokenizer.count_rerank(query, str(doc.get("text", "") if isinstance(doc, dict) else doc), instruct)
                  for doc in documents]
    else:
        raise ValueError(f"Unknown model type for token counting: {model_type}")
    return ModelInputCount(sum(counts), counts, tokenizer.metadata)


def count_model_output(model: str, response: Any) -> int:
    """Count all received generated text, including incomplete streamed choices."""
    if hasattr(response, "model_dump"):
        response = response.model_dump()
    if isinstance(response, str):
        return model_tokenizer(model).count_text(response)
    choices = response.get("choices", response) if isinstance(response, dict) else response
    choices = choices.values() if isinstance(choices, dict) else choices or []
    texts = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", choice.get("delta", choice))
        text = "".join(value for key in ("content", "reasoning_content", "reasoning", "refusal")
                       if isinstance((value := message.get(key)), str))
        text += str(choice.get("text", ""))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            text += str(function.get("name", ""))
            arguments = function.get("arguments", "")
            text += arguments if isinstance(arguments, str) else _json(arguments)
        texts.append(text)
    return sum(model_tokenizer(model).count_text(text) for text in texts)


count_llm_output = count_model_output


def prepare_chat_messages(messages: list[dict], *, model: str, tools: list | None,
                          max_input_tokens: int | None, strict: bool = True,
                          **options) -> tuple[list[dict], dict]:
    """Return a sending copy with old complete history groups removed if needed.

    System/developer instructions, the original task, the latest user request,
    the current tail, and incomplete tool transactions are never removed.
    """
    tokenizer = model_tokenizer(model)
    limit = tokenizer.effective_limit(max_input_tokens)
    before = tokenizer.count_chat(messages, tools, **options)
    copied = deepcopy(messages)
    groups: list[tuple[int, ...]] = []
    incomplete: set[int] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") or []
        if message.get("role") == "assistant" and calls:
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            group = tuple(range(index, end))
            expected = {call.get("id") for call in calls}
            returned = {messages[i].get("tool_call_id") for i in group[1:]}
            if not expected <= returned:
                incomplete.update(group)
            groups.append(group)
            index = end
        else:
            groups.append((index,))
            index += 1
    users = [i for i, message in enumerate(messages) if message.get("role") == "user"]
    pinned = {i for i, message in enumerate(messages) if message.get("role") in {"system", "developer"}}
    pinned.update(incomplete)
    if users:
        pinned.update((users[0], users[-1]))
    if messages:
        pinned.add(len(messages) - 1)
    removed: list[list[int]] = []
    retained = set(range(len(messages)))
    count = before
    if limit is not None and count > limit:
        for group in groups:
            if pinned.intersection(group):
                continue
            retained.difference_update(group)
            removed.append(list(group))
            copied = deepcopy([message for i, message in enumerate(messages) if i in retained])
            count = tokenizer.count_chat(copied, tools, **options)
            if count <= limit:
                break
    metadata = {**tokenizer.metadata, "input_tokens_before": before, "input_tokens_after": count,
                "max_input_tokens": limit, "removed_message_groups": removed,
                "removed_message_count": sum(map(len, removed)), "retained_message_indexes": sorted(retained)}
    if strict and limit is not None and count > limit:
        error = ModelInputTooLong(
            f"Required LLM input exceeds max_input_tokens after removing old complete history "
            f"({count} tokens > {limit}); the model call was not sent",
            model_type="llm", input_tokens=count, limit=limit,
        )
        error.input_preparation = metadata
        raise error
    return copied, metadata
