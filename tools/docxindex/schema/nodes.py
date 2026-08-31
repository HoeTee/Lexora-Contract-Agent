from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    parent_id: str | None = None
    node_type: str
    level: int | None = None
    title: str
    text: str = ""
    summary: str = ""
    start_anchor: str
    end_anchor: str
    page_start: int | None = None
    page_end: int | None = None
    page_source: str = "unavailable"
    page_confidence_score: int = 0
    token_estimate: int = 0
    confidence_score: int = 0
    confidence_evidence: list[str] = Field(default_factory=list)
    children: list["DocumentNode"] = Field(default_factory=list)
    source_start: int | None = None
    source_end: int | None = None
    start_index: int | None = None
    end_index: int | None = None
    key_items: list[str] = Field(default_factory=list)
    table_id: str | None = None
    mapping_ref: str | None = None
    row_start: int | None = None
    row_end: int | None = None

    def storage_view(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"children", "source_start", "source_end"})
        data["nodes"] = [child.node_id for child in self.children]
        return data

    def structure_view(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "title": self.title,
            "node_id": self.node_id,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "summary": self.summary,
            "key_items": self.key_items,
            "node_type": self.node_type,
            "token_estimate": self.token_estimate,
        }
        if self.table_id:
            data["table_id"] = self.table_id
        if self.page_start is not None or self.page_end is not None:
            data["page_start"] = self.page_start
            data["page_end"] = self.page_end
        if self.children:
            data["nodes"] = [child.structure_view() for child in self.children]
        else:
            data["nodes"] = []
        return data
