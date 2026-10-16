from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tools.docxindex.detection.heading_profiles import (
    DEFAULT_PROFILE_EXAMPLES,
    CompiledHeadingProfile,
    compile_heading_profile,
)
from tools.docxindex.llm.config import docxindex_config, load_yaml


DEFAULT_INDEXING_CONFIG = Path(__file__).resolve().parents[4] / "config.yaml"


class ParagraphIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    split_threshold_tokens: int = Field(default=1000, ge=100)
    chunk_target_tokens: int = Field(default=700, ge=100)

    @model_validator(mode="after")
    def validate_chunk_budget(self) -> "ParagraphIndexConfig":
        if self.chunk_target_tokens > self.split_threshold_tokens:
            raise ValueError("indexing.paragraph.chunk_target_tokens must not exceed split_threshold_tokens")
        return self


class TableIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    split_threshold_tokens: int = Field(default=20000, ge=500)
    chunk_target_tokens: int = Field(default=18000, ge=500)

    @model_validator(mode="after")
    def validate_chunk_budget(self) -> "TableIndexConfig":
        if self.chunk_target_tokens > self.split_threshold_tokens:
            raise ValueError("indexing.table.chunk_target_tokens must not exceed split_threshold_tokens")
        return self


class AttachmentIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_levels: int = Field(default=6, ge=1, le=6)


class SummaryIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_max_nodes: int = Field(default=10, ge=1, le=20)
    batch_max_tokens: int = Field(default=20000, ge=1000, le=100000)


class EnrichmentIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_expand: bool = True
    llm_summary: bool = True
    llm_attachment_hierarchy: bool = True


class HierarchyIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_max_tokens: int = Field(default=20000, ge=1000, le=26000)
    batch_target_tokens: int = Field(default=16000, ge=1000, le=26000)
    batch_overlap_tokens: int = Field(default=800, ge=0, le=5000)
    max_levels: int = Field(default=6, ge=1, le=6)

    @model_validator(mode="after")
    def validate_batch_budget(self) -> "HierarchyIndexConfig":
        if self.batch_target_tokens > self.input_max_tokens:
            raise ValueError("indexing.hierarchy.batch_target_tokens must not exceed input_max_tokens")
        if self.batch_overlap_tokens >= self.batch_target_tokens:
            raise ValueError("indexing.hierarchy.batch_overlap_tokens must be smaller than batch_target_tokens")
        return self


class HeadingProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level_1_examples: list[str] = Field(min_length=1, max_length=5)
    level_2_examples: list[str] = Field(min_length=1, max_length=5)
    level_3_examples: list[str] = Field(min_length=1, max_length=5)


def _default_heading_profiles() -> dict[str, HeadingProfileConfig]:
    return {name: HeadingProfileConfig(**examples) for name, examples in DEFAULT_PROFILE_EXAMPLES.items()}


class HeadingIndexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: dict[str, HeadingProfileConfig] = Field(default_factory=_default_heading_profiles, min_length=1)

    def compile_profiles(self) -> tuple[CompiledHeadingProfile, ...]:
        return tuple(
            compile_heading_profile(
                name,
                profile.level_1_examples,
                profile.level_2_examples,
                profile.level_3_examples,
            )
            for name, profile in self.profiles.items()
        )


class IndexingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraph: ParagraphIndexConfig = Field(default_factory=ParagraphIndexConfig)
    table: TableIndexConfig = Field(default_factory=TableIndexConfig)
    hierarchy: HierarchyIndexConfig = Field(default_factory=HierarchyIndexConfig)
    attachment: AttachmentIndexConfig = Field(default_factory=AttachmentIndexConfig)
    summary: SummaryIndexConfig = Field(default_factory=SummaryIndexConfig)
    enrichment: EnrichmentIndexConfig = Field(default_factory=EnrichmentIndexConfig)
    heading: HeadingIndexConfig = Field(default_factory=HeadingIndexConfig)

    @classmethod
    def from_sources(cls, config_path: Path | None = None) -> "IndexingConfig":
        raw = load_yaml(config_path or DEFAULT_INDEXING_CONFIG)
        data: dict[str, Any] = dict(docxindex_config(raw).get("indexing") or {})
        return cls.model_validate(data)
