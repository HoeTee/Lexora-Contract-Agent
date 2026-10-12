"""Load the allowed route-method descriptions from the application config."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
ROUTE_METHODS = {"title", "region", "rule", "scan", "hybrid"}
DEFAULT_ROUTE_METHODS = {
    "title": "读取一级标题，检查目录、必备标题和一级标题顺序。",
    "region": "按首部、正文、尾部、附件或标题关键词定位范围；complete 完整读取，relevant 在范围内执行结构语义检索并由向量补充。",
    "rule": "仅按路由明确给出的原文短词，执行全文精确关键词、金额和存在性定位。",
    "scan": "按 token 预算把全部 anchor 分批交给 LLM 逐项检查。",
    "hybrid": "无法限定标题、区域或精确关键词时，执行全局结构树语义检索；结果不足时使用向量补充。各步骤候选合并去重后统一重排并截取。",
}


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fallback_enabled: bool = True
    methods: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_ROUTE_METHODS))

    @classmethod
    def load(cls, path: Path | None = None) -> "RouteConfig":
        config_path = (path or DEFAULT_CONFIG_PATH).resolve()
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
        if not isinstance(raw, dict):
            raise ValueError(f"route config must be a mapping: {config_path}")
        retrieval = raw.get("retrieval") or {}
        docxindex = retrieval.get("docxindex") or {}
        data: dict[str, Any] = dict(docxindex.get("routing") or {})
        for legacy_key in (
            "criteria",
            "expected_criteria_count",
            "llm_candidates",
            "vector_candidates",
            "vector_threshold",
            "max_nodes",
        ):
            data.pop(legacy_key, None)
        config = cls.model_validate(data)
        if set(config.methods) != ROUTE_METHODS:
            raise ValueError("route methods must be exactly: title, region, rule, scan, hybrid")
        return config
