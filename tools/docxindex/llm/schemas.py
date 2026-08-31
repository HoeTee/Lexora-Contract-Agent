from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    method: Literal["title", "region", "rule", "scan", "hybrid"]
    terms: list[str] = Field(default_factory=list)
    regions: list[Literal["frontmatter", "body", "tail", "attachments"]] = Field(default_factory=list)
    all_titles: bool = False

    @model_validator(mode="after")
    def validate_method_fields(self) -> "RouteStep":
        if self.method == "region" and len(self.regions) != 1:
            raise ValueError("region route requires exactly one regions value")
        if self.method != "region" and self.regions:
            raise ValueError(f"{self.method} route must not set regions")
        if self.method == "rule" and not self.terms:
            raise ValueError("rule route requires at least one term")
        if self.method == "title" and not (self.terms or self.all_titles):
            raise ValueError("title route requires terms or all_titles=true")
        if self.method == "scan" and self.terms:
            raise ValueError("scan route must not set terms")
        return self


class RouteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[RouteStep] = Field(min_length=1, max_length=5)
    fallback: bool = False

    @model_validator(mode="after")
    def validate_plan(self) -> "RouteResponse":
        methods = {step.method for step in self.steps}
        if "scan" in methods and len(self.steps) != 1:
            raise ValueError("scan must be the only route step")
        if self.fallback and methods.intersection({"region", "scan", "hybrid"}):
            raise ValueError("region, scan, and hybrid routes must not request global fallback")
        return self


class ScanFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ScanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ScanFinding] = Field(default_factory=list)
