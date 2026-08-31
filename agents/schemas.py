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


class SubAgentAnchor(StrictOutputModel):
    xml_anchor_type: Literal["paragraph", "table"]
    xml_anchor_id: str = Field(min_length=1)
    quoted_text: str = Field(min_length=1)
    comment_text: str = Field(min_length=1, max_length=100)

    @field_validator("comment_text", mode="before")
    @classmethod
    def truncate_comment_text(cls, value):
        return value[:100] if isinstance(value, str) else value

    @field_validator("xml_anchor_id", "quoted_text", "comment_text")
    @classmethod
    def anchor_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be blank")
        return value


class SubAgentIssue(StrictOutputModel):
    issue_id: str = Field(min_length=1)
    risk_level: Literal["high", "medium", "low"] # 风险等级，必须是 "high"、"medium" 或 "low" 中的一个
    issue_comment: str = Field(min_length=1, max_length=160)
    anchors: list[SubAgentAnchor] = Field(default_factory=list)
    reasoning: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    check_point: str = Field(min_length=1)

    @field_validator("issue_comment", mode="before")
    @classmethod
    def truncate_issue_comment(cls, value):
        return value[:160] if isinstance(value, str) else value

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


class ReflectorOutput(StrictOutputModel):
    status: Literal["PASS", "REJECT"]
    feedback: str = ""


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
