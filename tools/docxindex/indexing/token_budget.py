from __future__ import annotations

from config import LLM_NAME
from endpoints.runtime.model_tokens import model_tokenizer

PARAGRAPH_CHUNK_TARGET_TOKENS = 700
PARAGRAPH_SPLIT_THRESHOLD_TOKENS = 1000
SUMMARY_TRIGGER_MIN_TOKENS = 300
SUMMARY_MAX_CHARS = 180
STRUCTURE_INLINE_BUDGET_TOKENS = 6000
STRUCTURE_PAGED_BUDGET_TOKENS = 20000

def estimate_tokens(text: str) -> int:
    return model_tokenizer(LLM_NAME).count_text(text)


def document_mode(tokens: int) -> str:
    if tokens <= STRUCTURE_INLINE_BUDGET_TOKENS:
        return "compact_structure"
    if tokens <= STRUCTURE_PAGED_BUDGET_TOKENS:
        return "paged_structure"
    return "filtered_or_paged_structure"
