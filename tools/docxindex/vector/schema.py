from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VectorMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    node_type: str
    start_index: int | None = None
    end_index: int | None = None
    start_anchor: str | None = None
    end_anchor: str | None = None
    token_estimate: int = 0


class VectorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vector_id: str
    node_id: str
    text_hash: str
    text: str
    embedding: list[float]
    metadata: VectorMetadata
    chunk_index: int = 0
    source_start: int = 0
    source_end: int | None = None


class VectorIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_index: str = "document_index.json"
    embedding_model: str
    items: list[VectorItem] = Field(default_factory=list)
    format_version: int = 1
    configured_input_tokens: int | None = None
    effective_input_tokens: int | None = None
    chunk_overlap_tokens: int = 0
    tokenizer: str | None = None
