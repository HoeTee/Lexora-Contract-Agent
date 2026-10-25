from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tools.docxindex.llm.config import docxindex_config, load_yaml


DEFAULT_RETRIEVAL_CONFIG = Path(__file__).resolve().parents[4] / "config.yaml"


class VectorRetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    score_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    probe_limit: int | None = Field(default=None, ge=1, strict=True)


class SemanticRetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structure_candidate_limit: int | None = Field(default=None, ge=1, strict=True)
    max_selected_nodes: int | None = Field(default=None, ge=1, strict=True)


class RerankConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=500, strict=True)
    output_tokens: int | None = Field(default=None, ge=500, strict=True)
    scan_batch_tokens: int | None = Field(default=None, ge=500, strict=True)
    vector: VectorRetrievalConfig = Field(default_factory=VectorRetrievalConfig)
    semantic: SemanticRetrievalConfig = Field(default_factory=SemanticRetrievalConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)

    @classmethod
    def from_sources(
        cls,
        config_path: Path | None = None,
        input_tokens: int | None = None,
        vector_enabled: bool | None = None,
        rerank_enabled: bool | None = None,
    ) -> "RetrievalConfig":
        raw = load_yaml(config_path or DEFAULT_RETRIEVAL_CONFIG)
        section = docxindex_config(raw)
        data: dict[str, Any] = {
            key: value
            for key, value in section.items()
            if key in {"input_tokens", "output_tokens", "scan_batch_tokens", "vector", "semantic", "rerank"}
        }
        vector = dict(data.get("vector") or {})
        semantic = dict(data.get("semantic") or {})
        rerank = dict(data.get("rerank") or {})
        if "concurrency" in section:
            raise ValueError("Remove retrieval.docxindex.concurrency; use model_semaphore limits.")
        routing = dict(section.get("routing") or {})

        _migrate_legacy_value(routing, "llm_candidates", semantic, "structure_candidate_limit")
        _migrate_legacy_value(routing, "max_nodes", semantic, "max_selected_nodes")
        _migrate_legacy_value(routing, "vector_candidates", vector, "probe_limit")
        _migrate_legacy_value(routing, "vector_threshold", vector, "score_threshold")

        if input_tokens is not None:
            data["input_tokens"] = input_tokens
        if vector_enabled is not None:
            vector["enabled"] = vector_enabled
        if rerank_enabled is not None:
            rerank["enabled"] = rerank_enabled

        data["vector"] = vector
        data["semantic"] = semantic
        data["rerank"] = rerank
        return cls.model_validate(data)


def _migrate_legacy_value(
    legacy: dict[str, Any],
    legacy_key: str,
    target: dict[str, Any],
    target_key: str,
) -> None:
    if legacy_key not in legacy:
        return
    legacy_value = legacy[legacy_key]
    if target_key in target and target[target_key] != legacy_value:
        raise ValueError(
            f"conflicting retrieval settings: routing.{legacy_key}={legacy_value!r} "
            f"and {target_key}={target[target_key]!r}"
        )
    target.setdefault(target_key, legacy_value)
