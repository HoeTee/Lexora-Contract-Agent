from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from routing.schemas import RoutePlan, RouteResponse, RouteStep


__all__ = ["RoutePlan", "RouteResponse", "RouteStep"]


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


class ScanFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ScanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ScanFinding] = Field(default_factory=list)
