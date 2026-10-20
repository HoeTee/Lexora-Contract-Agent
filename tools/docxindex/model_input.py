"""Count and split model inputs without modifying document nodes or HTTP formats."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Callable

import tiktoken


TOKENIZER_ROOT = Path(__file__).resolve().parents[2] / "resources" / "tokenizers"
_ALIASES = {
    "bge-m3": "bge-m3",
    "bge-reranker-v2-m3": "bge-reranker-v2-m3",
    "qwen3-embedding": "qwen3-embedding",
    "qwen3-embedding-8b": "qwen3-embedding",
    "qwen3-reranker": "qwen3-reranker",
    "qwen3-reranker-8b": "qwen3-reranker",
    "qwen3.6-35b-a3b": "qwen3.6-35b-a3b",
}
_BOUNDARY = re.compile(r"\n+|[。！？；.!?;](?:\s+|$)")
MODEL_INPUT_BATCH_SIZE = 10


class ModelInputTooLong(ValueError):
    failure_stage = "input_validation"


@dataclass(frozen=True)
class InputChunk:
    text: str
    start: int
    end: int


class ModelTokenizer:
    def __init__(self, model: str) -> None:
        self.model = model
        self.slug = _ALIASES.get(model.rsplit("/", 1)[-1].lower())
        self.native = self.slug is not None
        self.context_tokens: int | None = None
        if self.slug:
            manifest_file = TOKENIZER_ROOT / "manifest.json"
            if not manifest_file.is_file():
                raise RuntimeError("Offline tokenizer resources are missing. Run scripts/prepare_tokenizers.py before deployment.")
            entry = json.loads(manifest_file.read_text(encoding="utf-8"))["models"][self.slug]
            directory = TOKENIZER_ROOT / self.slug
            for name, record in entry["files"].items():
                path = directory / name
                if not path.is_file() or sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                    raise RuntimeError(f"Offline tokenizer file is missing or corrupt: {self.slug}/{name}")
            # Optional heavyweight dependency: legacy providers and control tests
            # do not load Transformers. Native adapters must use local files.
            from transformers import AutoTokenizer

            self.encoder = AutoTokenizer.from_pretrained(directory, local_files_only=True, use_fast=True,
                                                        trust_remote_code=False)
            self.context_tokens = int(entry["context_tokens"])
            self.identity = f"{self.slug}:{entry['revision']}"
        else:
            # Preserve the existing adapters for closed services without published
            # tokenizer files. This is an estimate, never a claimed BGE tokenizer.
            self.encoder = tiktoken.get_encoding("cl100k_base")
            self.context_tokens = {"text-embedding-v4": 8000, "qwen3-rerank": 32768}.get(model.lower())
            self.identity = "legacy-estimate:cl100k_base"

    def effective_limit(self, configured: int) -> int:
        if isinstance(configured, bool) or not isinstance(configured, int) or configured <= 0:
            raise ValueError("max_input_tokens must be a positive integer")
        return min(configured, self.context_tokens) if self.context_tokens else configured

    def count_text(self, text: str) -> int:
        if self.native:
            return len(self.encoder.encode(text, add_special_tokens=False, verbose=False))
        return len(self.encoder.encode(text, disallowed_special=()))

    def count_embedding(self, text: str) -> int:
        if self.native:
            return len(self.encoder.encode(text, add_special_tokens=True, verbose=False))
        return self.count_text(text)

    def count_rerank(self, query: str, document: str, instruct: str = "") -> int:
        if self.slug == "bge-reranker-v2-m3":
            return len(self.encoder.encode(query, text_pair=document, add_special_tokens=True, verbose=False))
        if self.slug == "qwen3-reranker":
            # Account for the published scoring wrapper locally. The HTTP payload
            # and server-side template are not changed by this length check.
            instruction = instruct or "Given a web search query, retrieve relevant passages that answer the query"
            prefix = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the '
                      'Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
            suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
            return self.count_text(prefix + f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}" + suffix)
        return self.count_text(query) + self.count_text(document) + self.count_text(instruct) + 32


@lru_cache(maxsize=16)
def model_tokenizer(model: str) -> ModelTokenizer:
    return ModelTokenizer(model)


def split_input(text: str, fits: Callable[[str], bool], *, tokenizer: ModelTokenizer,
                overlap_tokens: int = 0) -> list[InputChunk]:
    """Return source slices covering text; each slice passes the supplied final-input check."""
    if overlap_tokens < 0:
        raise ValueError("chunk_overlap_tokens must be nonnegative")
    if fits(text):
        return [InputChunk(text, 0, len(text))]
    if not fits(""):
        raise ModelInputTooLong("Query or fixed model-input overhead exceeds max_input_tokens before candidate text.")
    chunks = []
    start = 0
    while start < len(text):
        low, high, end = start + 1, len(text), start
        while low <= high:
            middle = (low + high) // 2
            if fits(text[start:middle]):
                end, low = middle, middle + 1
            else:
                high = middle - 1
        if end == start:
            raise ModelInputTooLong("Model input limit cannot accommodate the next source character.")
        if end < len(text):
            boundaries = [m.end() + start for m in _BOUNDARY.finditer(text[start:end])
                          if m.end() >= (end - start) * 0.6]
            if boundaries:
                end = boundaries[-1]
        chunk = text[start:end]
        if not fits(chunk):
            raise ModelInputTooLong("Final input chunk exceeds max_input_tokens.")
        chunks.append(InputChunk(chunk, start, end))
        if end == len(text):
            break
        next_start = end
        if overlap_tokens:
            while next_start > start and tokenizer.count_text(text[next_start - 1:end]) <= overlap_tokens:
                next_start -= 1
            if next_start <= start:
                raise ValueError("chunk_overlap_tokens consumes the complete input chunk; reduce the overlap.")
        start = next_start
    return chunks


def embedding_chunks(text: str, model: str, limit: int, overlap: int) -> list[InputChunk]:
    tokenizer = model_tokenizer(model)
    effective = tokenizer.effective_limit(limit)
    return split_input(text, lambda value: tokenizer.count_embedding(value) <= effective,
                       tokenizer=tokenizer, overlap_tokens=overlap)
