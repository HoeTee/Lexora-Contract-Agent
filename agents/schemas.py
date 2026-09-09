"""
Pydantic schemas for validating structured agent outputs.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from routing.schemas import RoutePlan


_INTERNAL_LOCATION_PATTERN = re.compile(
    r"(?:path:(?:body|frontmatter|tail|attachments)/\S+|\b(?:node_id|xml_anchor_id)\b|(?<![A-Za-z0-9])p\d+(?!\d)|检索结果\s*\d+)",
    flags=re.IGNORECASE,
)
_CONTEXT_DEPENDENT_PHRASES = ("同上", "如上", "见上", "前述问题")


def _validate_user_facing_review_text(value: str) -> str:
    """Reject comments that expose retrieval locations or depend on another comment."""
    text = value.strip()
    if _INTERNAL_LOCATION_PATTERN.search(text):
        raise ValueError("user-facing review text must not expose internal retrieval locations")
    if any(phrase in text for phrase in _CONTEXT_DEPENDENT_PHRASES):
        raise ValueError("user-facing review text must be understandable without references such as 同上")
    return value


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


class ExtractedPlacement(StrictOutputModel):
    """Where the review findings of one criterion should be written."""

    target: Literal["summary", "inline", "inline_at_title", "tail", "body_end"]
    note: str = ""


class CriterionStrategy(StrictOutputModel):
    """Planner-extracted execution strategy for one criterion."""

    id: str = Field(min_length=1)
    review_instruction: str = ""
    route: RoutePlan
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

    @field_validator("comment_text")
    @classmethod
    def comment_is_user_facing(cls, value: str) -> str:
        if not value.strip():
            return value
        return _validate_user_facing_review_text(value)


class EvidenceCoverageDecision(StrictOutputModel):
    """One semantic decision for an evidence item in a mandatory complete-region audit."""

    xml_anchor_type: Literal["paragraph", "table"]
    xml_anchor_id: str = Field(min_length=1)
    decision: Literal["issue", "compliant", "not_applicable"]
    reason: str = Field(min_length=1)

    @field_validator("xml_anchor_id", "reason")
    @classmethod
    def coverage_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("coverage decision fields must not be blank")
        return value


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
        edits = [anchor for anchor in self.anchors if anchor.role == "edit"]
        if self.anchors and not edits:
            raise ValueError("anchored issue requires an edit location with a specific comment; evidence alone is not an annotation. Use anchors=[] only for a genuine summary-only issue")
        missing_edits = [anchor for anchor in edits if anchor.defect_type == "missing_value"]
        if missing_edits and len(edits) != 1:
            raise ValueError("a missing-value issue must contain exactly one edit anchor; return one separate issue per missing field")
        return self

    @field_validator("issue_id", "issue_comment", "reasoning", "criterion", "check_point") # 字段验证
    @classmethod # 在实例方法创建前检验某字段
    def required_text_not_blank(cls, value: str) -> str: # cls 和 value 是约定俗成的参数名
        if not value.strip():
            raise ValueError("field must not be blank")
        return value

    @field_validator("issue_comment")
    @classmethod
    def issue_comment_is_user_facing(cls, value: str) -> str:
        return _validate_user_facing_review_text(value)


class SubAgentOutput(StrictOutputModel):
    status: Literal["compliant", "issues_found", "not_applicable"]
    applicability_reason: str = ""
    issues: list[SubAgentIssue] = Field(default_factory=list) # 使用 default_factory 来确保 issues 字段默认为一个空列表，而不是 None
    coverage_decisions: list[EvidenceCoverageDecision] = Field(default_factory=list)

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
        issue_ids = [issue.issue_id for issue in self.issues]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("each issue must have a unique issue_id")
        edit_keys: dict[tuple[str, str, str, str], str] = {}
        for issue in self.issues:
            for anchor in issue.anchors:
                if anchor.role != "edit":
                    continue
                key = (
                    anchor.xml_anchor_type,
                    anchor.xml_anchor_id,
                    "".join(anchor.quoted_text.split()),
                    anchor.defect_type,
                )
                if key in edit_keys:
                    first_issue_id = edit_keys[key]
                    raise ValueError(
                        "duplicate edit location across issues "
                        f"{first_issue_id} and {issue.issue_id}: "
                        f"{anchor.xml_anchor_type}/{anchor.xml_anchor_id}, "
                        f"defect_type={anchor.defect_type}, quoted_text={anchor.quoted_text!r}. "
                        "If both describe one defect, merge them into one issue. If they describe "
                        "different defects, each quoted_text must be the narrow continuous source "
                        "text that directly contains its own defect. When a field is wholly absent "
                        "and has no source location, use anchors=[] instead of borrowing adjacent text."
                    )
                edit_keys[key] = issue.issue_id
        coverage_keys = [
            (decision.xml_anchor_type, decision.xml_anchor_id)
            for decision in self.coverage_decisions
        ]
        if len(coverage_keys) != len(set(coverage_keys)):
            raise ValueError("coverage_decisions must contain each evidence anchor at most once")
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


class CoverageGap(StrictOutputModel):
    xml_anchor_type: Literal["paragraph", "table"]
    xml_anchor_id: str = Field(min_length=1)
    quoted_text: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReflectorOutput(StrictOutputModel):
    status: Literal["PASS", "REJECT"]
    feedback: str = ""
    finding_decisions: list[FindingDecision] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_findings(self):
        finding_ids = [item.finding_id for item in self.finding_decisions]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding_decisions must contain each finding_id at most once")
        if self.status == "PASS" and self.coverage_gaps:
            raise ValueError("PASS must not contain coverage_gaps")
        return self


class SummaryDuplicateGroup(StrictOutputModel):
    keep_issue_key: str = Field(min_length=1)
    merge_issue_keys: list[str] = Field(min_length=1)
    canonical_comment: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_distinct_keys(self):
        if self.keep_issue_key in self.merge_issue_keys:
            raise ValueError("keep_issue_key must not also appear in merge_issue_keys")
        if len(self.merge_issue_keys) != len(set(self.merge_issue_keys)):
            raise ValueError("merge_issue_keys must be unique")
        _validate_user_facing_review_text(self.canonical_comment)
        return self


class SummaryContentOutput(StrictOutputModel):
    overall_comment: str = "" # 总体审查总结
    priority_comments: list[str] = Field(default_factory=list) # 优先修改建议；使用 default_factory 来确保 priority_comments 字段默认为一个空列表，而不是 None
    duplicate_groups: list[SummaryDuplicateGroup] = Field(default_factory=list)

    @field_validator("priority_comments") # 字段验证
    @classmethod # 在实例方法创建前检验某字段
    def priority_comments_not_blank(cls, value: list[str]) -> list[str]: # cls 和 value 是约定俗成的参数名
        if any(not item.strip() for item in value): # 检查优先修改建议列表中的每个建议是否为非空字符串
            raise ValueError("priority_comments must not contain empty strings")
        return value # 如果验证通过，返回原始值

    @model_validator(mode="after")
    def validate_duplicate_group_membership(self):
        merged_keys = [
            issue_key
            for group in self.duplicate_groups
            for issue_key in group.merge_issue_keys
        ]
        if len(merged_keys) != len(set(merged_keys)):
            raise ValueError("an issue may be merged by at most one duplicate group")
        return self


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
