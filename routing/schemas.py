"""Canonical schemas shared by routing planners and retrieval executors."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RouteStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["title", "region", "rule", "scan", "hybrid"]
    terms: list[str] = Field(default_factory=list)
    regions: list[Literal["frontmatter", "body", "tail", "attachments"]] = Field(default_factory=list)
    all_titles: bool = False
    anchor_terms: list[str] = Field(default_factory=list)
    coverage: Literal["relevant", "complete"] = "relevant"

    @model_validator(mode="after")
    def validate_method_fields(self) -> "RouteStep":
        if self.method == "region" and (
            bool(self.regions) == bool(self.anchor_terms) or len(self.regions) > 1
        ):
            raise ValueError("region route requires exactly one locator: regions or anchor_terms")
        if self.method != "region" and self.regions:
            raise ValueError(f"{self.method} route must not set regions")
        if self.method == "rule" and not self.terms:
            raise ValueError("rule route requires at least one term")
        if self.method == "title" and not (self.terms or self.all_titles):
            raise ValueError("title route requires terms or all_titles=true")
        if self.method == "scan" and self.terms:
            raise ValueError("scan route must not set terms")
        if self.method != "region" and self.anchor_terms:
            raise ValueError(f"{self.method} route must not set anchor_terms")
        if self.method != "region" and self.coverage != "relevant":
            raise ValueError(f"{self.method} route must use coverage=relevant")
        return self


class RoutePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[RouteStep] = Field(min_length=1, max_length=5)
    fallback: bool = False

    @model_validator(mode="after")
    def validate_plan(self) -> "RoutePlan":
        methods = {step.method for step in self.steps}
        scan_count = sum(step.method == "scan" for step in self.steps)
        if scan_count > 1:
            raise ValueError("scan route may appear at most once")
        if "scan" in methods and methods.intersection({"region", "hybrid"}):
            raise ValueError("scan route may only be combined with title or rule routes")
        if self.fallback and methods.intersection({"region", "scan", "hybrid"}):
            raise ValueError("region, scan, and hybrid routes must not request global fallback")
        return self


# Compatibility name for callers that previously consumed the docxindex schema.
RouteResponse = RoutePlan
