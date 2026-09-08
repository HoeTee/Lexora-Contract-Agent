"""Load the allowed route-method descriptions from the application config."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
ROUTE_METHODS = {"title", "region", "rule", "scan", "hybrid"}


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fallback_enabled: bool = True
    methods: dict[str, str]

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
