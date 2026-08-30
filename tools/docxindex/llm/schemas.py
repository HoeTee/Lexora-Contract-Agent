from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)


class SummaryBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class SummaryBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summaries: list[SummaryBatchItem] = Field(default_factory=list)


class ExpandSubsection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: str = Field(min_length=1)
    title: str = Field(min_length=1)
    level_hint: int | None = Field(default=None, ge=1, le=6)


class ExpandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subsections: list[ExpandSubsection] = Field(default_factory=list)


class AttachmentHeading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: str = Field(min_length=1)
    title: str = Field(min_length=1)
    level: int = Field(ge=1, le=6)


class AttachmentLevelRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number_family: Literal[
        "subattachment",
        "chapter",
        "article",
        "section",
        "cn_comma",
        "cn_paren",
        "arabic_paren",
        "arabic_multilevel",
        "arabic_comma",
        "arabic_dot",
        "arabic_close",
        "letter",
    ]
    level: int = Field(ge=1, le=6)


class AttachmentHierarchySegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_anchor: str = Field(min_length=1)
    document_title: AttachmentHeading | None = None
    level_rules: list[AttachmentLevelRule] = Field(default_factory=list)
    additional_headings: list[AttachmentHeading] = Field(default_factory=list)


class AttachmentHierarchyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[AttachmentHierarchySegment] = Field(default_factory=list)


class QueryMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    reason: str = ""


class QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[QueryMatch] = Field(default_factory=list)


class RerankMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class RerankResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[RerankMatch] = Field(default_factory=list)


class RouteStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["title", "region", "rule", "join", "scan", "hybrid"]
    terms: list[str] = Field(default_factory=list)
    regions: list[Literal["frontmatter", "body", "tail", "attachments"]] = Field(default_factory=list)
    slots: list[str] = Field(default_factory=list)
    all_titles: bool = False
    context: int = Field(default=0, ge=0, le=2)


class RouteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[RouteStep] = Field(min_length=1, max_length=5)
    fallback: bool = False
