from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AnchorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body_child_index: int
    type: str
    text: str
    raw_text: str | None = None
    node_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    xml_path: str


class DocumentIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "docx-index-v5"
    source_file: str
    top_regions: dict[str, str]
    settings: dict[str, Any]
    nodes: list[dict[str, Any]]
    structure_tree: list[dict[str, Any]]
    anchor_map: dict[str, AnchorRecord]
    table_map: dict[str, dict[str, Any]] = Field(default_factory=dict)
    region_boundaries: dict[str, dict[str, str]] = Field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
