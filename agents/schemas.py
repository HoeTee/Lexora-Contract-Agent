"""
Pydantic schemas for validating structured agent outputs.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid") # 禁止模型接受未定义的字段，确保输出结构严格符合预期


class PlannerCriterion(StrictOutputModel):
    id: str = Field(min_length=1)
    section: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    check_points: list[str] = Field(min_length=1)

    @field_validator("check_points") # 字段验证
    @classmethod # 在实例方法创建前检验某字段
    def check_points_not_empty(cls, value: list[str]) -> list[str]: # cls 和 value 是约定俗成的参数名
        if any(not item.strip() for item in value):
            raise ValueError("check_points must not contain empty strings")
        return value


class PlannerOutput(StrictOutputModel):
    criteria: list[PlannerCriterion] = Field(min_length=1)


class ExtractedRouteStep(StrictOutputModel):
    """Single retrieval step planned by the planner strategy extraction."""

    method: Literal["title", "region", "rule", "scan", "subtree"]
    terms: list[str] = Field(default_factory=list)
    regions: list[Literal["frontmatter", "body", "tail", "attachments"]] = Field(default_factory=list)
    all_titles: bool = False
    anchor_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_method_fields(self) -> "ExtractedRouteStep":
        if self.method == "region" and len(self.regions) != 1:
            raise ValueError("region route requires exactly one regions value")
        if self.method != "region" and self.regions:
            raise ValueError(f"{self.method} route must not set regions")
        if self.method == "rule" and not self.terms:
            raise ValueError("rule route requires at least one term")
        if self.method == "title" and not (self.terms or self.all_titles):
            raise ValueError("title route requires terms or all_titles=true")
        if self.method == "subtree" and not self.anchor_terms:
            raise ValueError("subtree route requires at least one anchor term")
        if self.method == "scan" and self.terms:
            raise ValueError("scan route must not set terms")
        if self.method != "subtree" and self.anchor_terms:
            raise ValueError(f"{self.method} route must not set anchor_terms")
        return self


class ExtractedRoute(StrictOutputModel):
    """Deterministic retrieval plan executed by docxindex instead of LLM routing."""

    steps: list[ExtractedRouteStep] = Field(min_length=1, max_length=5)
    fallback: bool = False

    @model_validator(mode="after")
    def validate_scan_combination(self):
        # Match the existing docxindex RouteResponse contract before execution.
        methods = [step.method for step in self.steps]
        if methods.count("scan") > 1:
            raise ValueError("scan route may appear at most once")
        if "scan" in methods and any(m not in {"scan", "title", "rule"} for m in methods):
            raise ValueError("scan route may only be combined with title or rule routes")
        return self


class ExtractedPlacement(StrictOutputModel):
    """Where the review findings of one criterion should be written."""

    target: Literal["summary", "inline", "inline_at_title", "tail", "body_end"]
    note: str = ""


class CriterionStrategy(StrictOutputModel):
    """Planner-extracted execution strategy for one criterion."""

    id: str = Field(min_length=1)
    review_instruction: str = ""
    route: ExtractedRoute
    placement: ExtractedPlacement


class StrategyExtractionOutput(StrictOutputModel):
    strategies: list[CriterionStrategy] = Field(min_length=1)


class SubAgentAnchor(StrictOutputModel):
    role: Literal["edit", "evidence"] = "edit"
    defect_type: Literal["missing_value", "inconsistency", "wording", "other"] = "other"
    xml_anchor_type: Literal["paragraph", "table"]
    xml_anchor_id: str = Field(min_length=1)
    quoted_text: str = Field(min_length=1)
    comment_text: str = Field(default="", max_length=300)

    @field_validator("xml_anchor_id", "quoted_text")
    @classmethod
    def anchor_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be blank")
        return value

    @model_validator(mode="after")
    def validate_role(self):
        if self.role == "edit" and not self.comment_text.strip():
            raise ValueError("edit anchor requires a specific comment_text")
        if self.role == "evidence" and self.comment_text:
            raise ValueError("evidence anchor must have empty comment_text")
        return self


class SubAgentIssue(StrictOutputModel):
    issue_id: str = Field(min_length=1)
    risk_level: Literal["high", "medium", "low"] # 风险等级，必须是 "high"、"medium" 或 "low" 中的一个
    issue_comment: str = Field(min_length=1, max_length=600)
    anchors: list[SubAgentAnchor] = Field(default_factory=list)
    reasoning: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    check_point: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_edit_for_anchored_issue(self):
        if self.anchors and not any(a.role == "edit" for a in self.anchors):
            raise ValueError("anchored issue requires an edit location with a specific comment; evidence alone is not an annotation. Use anchors=[] only for a genuine summary-only issue")
        return self

    @field_validator("issue_id", "issue_comment", "reasoning", "criterion", "check_point") # 字段验证
    @classmethod # 在实例方法创建前检验某字段
    def required_text_not_blank(cls, value: str) -> str: # cls 和 value 是约定俗成的参数名
        if not value.strip():
            raise ValueError("field must not be blank")
        return value


class SubAgentOutput(StrictOutputModel):
    status: Literal["compliant", "issues_found", "not_applicable"]
    applicability_reason: str = ""
    issues: list[SubAgentIssue] = Field(default_factory=list) # 使用 default_factory 来确保 issues 字段默认为一个空列表，而不是 None

    @field_validator("applicability_reason") # 字段验证
    @classmethod # 在实例方法创建前检验某字段
    def applicability_reason_trimmed(cls, value: str) -> str: # cls 和 value 是约定俗成的参数名
        return value.strip()

    @model_validator(mode="after") # 总体验证
    def validate_status_issues(self):
        if self.status == "compliant" and self.issues:
            raise ValueError("status is compliant, so issues must be empty")
        if self.status == "issues_found" and not self.issues:
            raise ValueError("status is issues_found, so issues must not be empty")
        if self.status == "not_applicable":
            if self.issues:
                raise ValueError("status is not_applicable, so issues must be empty")
            if not self.applicability_reason:
                raise ValueError("status is not_applicable, so applicability_reason must not be blank")
        return self


class FindingDecision(StrictOutputModel):
    finding_id: str = Field(min_length=1)
    decision: Literal["confirm", "revise", "remove"]
    reason: str = Field(min_length=1)

    @field_validator("finding_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("finding decision must not be blank")
        return value


class ReflectorOutput(StrictOutputModel):
    status: Literal["PASS", "REJECT"]
    feedback: str = ""
    finding_decisions: list[FindingDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_findings(self):
        finding_ids = [item.finding_id for item in self.finding_decisions]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding_decisions must contain each finding_id at most once")
        return self


class SummaryContentOutput(StrictOutputModel):
    overall_comment: str = "" # 总体审查总结
    priority_comments: list[str] = Field(default_factory=list) # 优先修改建议；使用 default_factory 来确保 priority_comments 字段默认为一个空列表，而不是 None

    @field_validator("priority_comments") # 字段验证
    @classmethod # 在实例方法创建前检验某字段
    def priority_comments_not_blank(cls, value: list[str]) -> list[str]: # cls 和 value 是约定俗成的参数名
        if any(not item.strip() for item in value): # 检查优先修改建议列表中的每个建议是否为非空字符串
            raise ValueError("priority_comments must not contain empty strings")
        return value # 如果验证通过，返回原始值


class SummaryOutput(SummaryContentOutput):
    """Preferred concise summary shape used for the first three attempts."""

    @model_validator(mode="after") # 总体验证
    def validate_total_length(self):
        total_length = len(self.overall_comment) + sum(
            len(item) for item in self.priority_comments
        )
        if total_length > 200: # overall_comment和priority_comments的总长度必须小于等于200个字符
            raise ValueError("overall_comment and priority_comments must be <= 200 chars total")
        return self # 如果验证通过，返回整个对象 self
