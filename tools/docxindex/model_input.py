"""Count and split model inputs without modifying document nodes or HTTP formats."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Callable

from endpoints.runtime.errors import ModelInputTooLong
from endpoints.runtime.model_calls import record_input_rejection
from endpoints.runtime.model_tokens import TOKENIZER_ROOT, ModelTokenizer, model_tokenizer


_BOUNDARY = re.compile(r"\n+|[。！？；.!?;](?:\s+|$)")
MODEL_INPUT_BATCH_SIZE = 10
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class InputChunk:
    text: str
    start: int
    end: int


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
    overlap_warned = False
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
            if tokenizer.count_text(chunk) <= overlap_tokens:
                # An oversized overlap is a preparation warning, not an invalid
                # deployment. Always advance at least one new source character.
                next_start = start + max(1, (end - start) // 2)
                if not overlap_warned:
                    _LOGGER.warning("Embedding overlap reduced to keep source slices advancing: configured_tokens=%s chunk_chars=%s",
                                    overlap_tokens, end - start)
                    overlap_warned = True
            else:
                while next_start > start and tokenizer.count_text(text[next_start - 1:end]) <= overlap_tokens:
                    next_start -= 1
        start = next_start
    return chunks


def embedding_chunks(text: str, model: str, limit: int | None, overlap: int) -> list[InputChunk]:
    tokenizer = model_tokenizer(model)
    effective = tokenizer.effective_limit(limit)
    try:
        return split_input(text, lambda value: effective is None or tokenizer.count_embedding(value) <= effective,
                           tokenizer=tokenizer, overlap_tokens=overlap)
    except ModelInputTooLong as exc:
        record_input_rejection(model_type="embedding", model=model, caller="docxindex.embedding",
            request={"model": model, "input": [text]}, limit=effective, error=exc,
            preparation={"stage": "embedding-slicing", "fixed_input_tokens": tokenizer.count_embedding("")})
        raise
