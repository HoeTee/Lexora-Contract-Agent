"""
Central configuration for the contract review workflow.
"""

from __future__ import annotations

import os
import re
import math
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv
from endpoints.runtime.model_tokens import validate_tokenizer_resources


PROJECT_ROOT_PATH = Path(__file__).resolve().parent
PROJECT_ROOT = str(PROJECT_ROOT_PATH)
ENV_PATH = PROJECT_ROOT_PATH / ".env"
CONFIG_PATH = PROJECT_ROOT_PATH / "config.yaml"

if not ENV_PATH.exists():
    raise RuntimeError(f"Missing required .env file: {ENV_PATH}")
if not CONFIG_PATH.exists():
    raise RuntimeError(f"Missing required config.yaml file: {CONFIG_PATH}")

load_dotenv(ENV_PATH)


def _load_yaml_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RuntimeError("config.yaml must contain a top-level mapping.")
    return data


CONFIG = _load_yaml_config()


def _check_fields(data: Any, allowed: set[str], path: str) -> None:
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must be a mapping.")
    unknown = set(data) - allowed
    if unknown:
        raise RuntimeError(f"Unknown or removed configuration in {path}: {sorted(unknown)}. See docs/CONCURRENCY_TIMEOUT_CONFIGURATION.md")


def _validate_fields() -> None:
    sections = {
        "llm": {"base_url", "name", "enable_thinking", "stream", "max_input_tokens", "max_tool_calls", "temperature", "top_p", "seed"},
        "embedding": {"base_url", "name", "max_input_tokens", "chunk_overlap_tokens"},
        "rerank": {"base_url", "name", "max_input_tokens", "instruct", "provider_model_names"},
        "workflow": {"max_reflection_rounds", "max_orchestrator_concurrency", "model_wait_timeout_seconds", "model_call_timeout_seconds", "model_call_max_retries", "criterion_retry_max_attempts", "subagent_allowed_tools"},
        "queue": {"broker_url", "pending_timeout_seconds", "queued_timeout_seconds", "watchdog_interval_seconds", "worker_disable_prefetch", "task_time_limit_seconds"},
        "retrieval": {"backend", "docxindex", "llamaindex"},
        "api": {"require_request_model_config", "keep_input", "keep_output", "result_output_default", "result_upload_enabled", "result_upload_domain", "result_upload_path", "result_upload_authorization", "result_upload_timeout_seconds", "meta_required", "meta_fields", "callback_enabled", "callback_url", "callback_file_field", "url_download_timeout_seconds", "url_download_max_size"},
        "docx": {"comment_include_criterion"}, "logging": {"enabled", "detailed"},
        "model_semaphore": {"llm", "embedding", "reranker"},
    }
    _check_fields(CONFIG, set(sections) | {"tokenizers"}, "config.yaml")
    for name, allowed in sections.items():
        if name in CONFIG:
            _check_fields(CONFIG[name], allowed, name)
    retrieval = CONFIG.get("retrieval") or {}
    if "llamaindex" in retrieval:
        _check_fields(retrieval["llamaindex"], {"chunk_size", "chunk_overlap", "similarity_top_k", "rerank_top_n"}, "retrieval.llamaindex")
    docx = retrieval.get("docxindex", {})
    _check_fields(docx, {"input_tokens", "output_tokens", "scan_batch_tokens", "vector", "semantic", "rerank", "routing", "indexing"}, "retrieval.docxindex")
    for name, allowed in {"vector": {"enabled", "score_threshold", "probe_limit"}, "semantic": {"structure_candidate_limit", "max_selected_nodes"}, "rerank": {"enabled"}, "routing": {"fallback_enabled", "llm_candidates", "max_nodes", "vector_candidates", "vector_threshold"}}.items():
        if name in docx:
            _check_fields(docx[name], allowed, f"retrieval.docxindex.{name}")
    indexing = docx.get("indexing", {})
    indexing_sections = {
        "paragraph": {"split_threshold_tokens", "chunk_target_tokens"},
        "table": {"split_threshold_tokens", "chunk_target_tokens"},
        "summary": {"batch_max_nodes", "batch_max_tokens"},
        "hierarchy": {"input_max_tokens", "batch_target_tokens", "batch_overlap_tokens", "max_levels"},
        "attachment": {"max_levels"},
        "enrichment": {"llm_expand", "llm_summary", "llm_attachment_hierarchy"},
        "heading": {"profiles"},
    }
    _check_fields(indexing, set(indexing_sections), "retrieval.docxindex.indexing")
    for name, allowed in indexing_sections.items():
        if name in indexing:
            _check_fields(indexing[name], allowed, f"retrieval.docxindex.indexing.{name}")
    heading = indexing.get("heading", {})
    if "profiles" in heading:
        profiles = heading["profiles"]
        if not isinstance(profiles, dict) or not profiles:
            raise RuntimeError("retrieval.docxindex.indexing.heading.profiles must contain named profiles.")
        for name, profile in profiles.items():
            _check_fields(profile, {"level_1_examples", "level_2_examples", "level_3_examples"},
                          f"retrieval.docxindex.indexing.heading.profiles.{name}")


_validate_fields()


def cfg(section: str, key: str) -> Any:
    section_data = CONFIG.get(section)
    if not isinstance(section_data, dict):
        raise RuntimeError(f"Missing config.yaml section: {section}")
    if key not in section_data:
        raise RuntimeError(f"Missing config.yaml field: {section}.{key}")
    return section_data[key]


def cfg_optional(section: str, key: str, default: Any) -> Any:
    section_data = CONFIG.get(section)
    if not isinstance(section_data, dict):
        return default
    return section_data.get(key, default)


def cfg_nested(section: str, subsection: str, key: str) -> Any:
    section_data = CONFIG.get(section)
    if not isinstance(section_data, dict):
        raise RuntimeError(f"Missing config.yaml section: {section}")
    subsection_data = section_data.get(subsection)
    if not isinstance(subsection_data, dict):
        raise RuntimeError(f"Missing config.yaml section: {section}.{subsection}")
    if key not in subsection_data:
        raise RuntimeError(f"Missing config.yaml field: {section}.{subsection}.{key}")
    return subsection_data[key]


def env_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required .env field: {name}")
    return value.strip()


def env_optional(name: str) -> str:
    value = os.getenv(name)
    if value is None:
        return ""
    return value.strip()


def env_bool(name: str, default: bool | None) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return _parse_bool(raw, name)


def _parse_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
        normalized = raw.lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise RuntimeError(f"{field_name} must be true or false, got {value!r}.")


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise RuntimeError(f"{field_name} must be an integer, got {value!r}.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field_name} must be an integer, got {value!r}.") from exc


def _as_float(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field_name} must be a number, got {value!r}.") from exc
    if isinstance(value, bool) or not math.isfinite(result):
        raise RuntimeError(f"{field_name} must be a finite number, got {value!r}.")
    return result


def _limit(value: Any, field_name: str, *, integer: bool = True, minimum: int = 1) -> int | float | None:
    """An omitted/null upper bound is unlimited; zero is only valid for retry counts."""
    if value is None:
        return None
    result = _as_int(value, field_name) if integer else _as_float(value, field_name)
    if result < minimum or (not integer and result <= 0):
        raise RuntimeError(f"{field_name} must be at least {minimum}, or null.")
    return result


def _tokenizer_model_names(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or not value:
        raise RuntimeError("tokenizers must explicitly map tokenizer IDs to model name lists, including other.")
    supported = {"qwen3.6-35b-a3b", "qwen3-embedding", "qwen3-reranker", "bge-m3",
                 "bge-reranker-v2-m3", "deepseek-v4-flash-0731", "cl100k_base"}
    result = {}
    seen = {}
    for tokenizer_id, names in value.items():
        if tokenizer_id not in supported:
            raise RuntimeError(f"Unknown tokenizer ID: {tokenizer_id!r}.")
        if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
            raise RuntimeError(f"tokenizers.{tokenizer_id} must be a nonempty list of model names.")
        unique = {}
        for name in names:
            normalized = name.strip().casefold()
            previous = seen.get(normalized)
            if previous is not None and previous != tokenizer_id:
                raise RuntimeError(f"Tokenizer model name {name!r} occurs under both {previous!r} and {tokenizer_id!r}.")
            seen[normalized] = tokenizer_id
            unique.setdefault(normalized, name.strip())
        result[tokenizer_id] = tuple(unique.values())
    if seen.get("other") != "cl100k_base":
        raise RuntimeError("tokenizers.cl100k_base must explicitly contain other for unmatched model names.")
    return result


def _as_str(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError(f"{field_name} must be a string, got {value!r}.")
    return value.strip()


def _as_str_tuple(value: Any, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        raise RuntimeError(f"{field_name} must be a string or list, got {value!r}.")
    result = tuple(str(item).strip() for item in items if str(item).strip())
    if not result and not allow_empty:
        raise RuntimeError(f"{field_name} must contain at least one value.")
    return result


def _as_provider_model_names(value: Any, field_name: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{field_name} must be a mapping of provider to model name list.")
    result: dict[str, tuple[str, ...]] = {}
    seen: dict[str, str] = {}
    for provider, model_names in value.items():
        provider_name = _as_str(provider, f"{field_name} provider").lower()
        if provider_name not in {"higress_qwen", "bge", "dashscope_qwen"}:
            raise RuntimeError(
                f"{field_name} provider must be one of 'higress_qwen', 'bge', or 'dashscope_qwen', "
                f"got {provider_name!r}."
            )
        names = _as_str_tuple(model_names, f"{field_name}.{provider_name}")
        for model_name in names:
            normalized = model_name.lower()
            if normalized in seen:
                raise RuntimeError(
                    f"{field_name} model name {model_name!r} is mapped to both "
                    f"{seen[normalized]!r} and {provider_name!r}."
                )
            seen[normalized] = provider_name
        result[provider_name] = names
    if not result:
        raise RuntimeError(f"{field_name} must contain at least one provider mapping.")
    return result


def _as_size_bytes(value: Any, field_name: str) -> int:
    if isinstance(value, int):
        if value <= 0:
            raise RuntimeError(f"{field_name} must be greater than 0.")
        return value
    raw = _as_str(value, field_name)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb|b)?", raw.lower())
    if not match:
        raise RuntimeError(f"{field_name} must be a size like '512KB', '50MB', '1GB', or bytes.")
    amount = float(match.group(1))
    unit = match.group(2) or "b"
    multipliers = {
        "b": 1,
        "kb": 1024,
        "mb": 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
    }
    size = int(amount * multipliers[unit])
    if size <= 0:
        raise RuntimeError(f"{field_name} must be greater than 0.")
    return size


def _project_path(value: Any, field_name: str) -> str:
    raw = _as_str(value, field_name)
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT_PATH / path
    return str(path)


def infer_rerank_provider(model_name: str) -> str:
    normalized = _as_str(model_name, "reranker model name").lower()
    for provider, model_names in RERANK_PROVIDER_MODEL_NAMES.items():
        if normalized in {item.lower() for item in model_names}:
            return provider
    raise RuntimeError(f"Unsupported reranker model name: {model_name!r}.")


MCP_SERVER_TARGET = "module:mcp_service.server.stdio_entry"
TOKENIZER_MODEL_NAMES = _tokenizer_model_names(CONFIG.get("tokenizers"))
validate_tokenizer_resources(TOKENIZER_MODEL_NAMES)

API_REQUIRE_REQUEST_MODEL_CONFIG = _parse_bool(
    cfg_optional("api", "require_request_model_config", False),
    "api.require_request_model_config",
)

LLM_API_KEY = env_optional("LLM_API_KEY") if API_REQUIRE_REQUEST_MODEL_CONFIG else env_required("LLM_API_KEY")
EMBED_API_KEY = env_optional("EMBED_API_KEY") if API_REQUIRE_REQUEST_MODEL_CONFIG else env_required("EMBED_API_KEY")
SESSION_SECRET_KEY = env_required("SESSION_SECRET_KEY")

LLM_BASE_URL = _as_str(cfg("llm", "base_url"), "llm.base_url")
LLM_NAME = env_optional("LLM_MODEL_NAME") if API_REQUIRE_REQUEST_MODEL_CONFIG and env_optional("LLM_MODEL_NAME") else _as_str(cfg("llm", "name"), "llm.name")
LLM_ENABLE_THINKING = _parse_bool(cfg("llm", "enable_thinking"), "llm.enable_thinking")
LLM_STREAM = _parse_bool(cfg_optional("llm", "stream", False), "llm.stream")
if not isinstance(LLM_STREAM, bool):
    raise RuntimeError("llm.stream must be true or false.")
LLM_MAX_INPUT_TOKENS = _limit(cfg_optional("llm", "max_input_tokens", None), "llm.max_input_tokens")
MAX_TOOL_CALLS = _limit(cfg_optional("llm", "max_tool_calls", None), "llm.max_tool_calls")
TEMPERATURE = _as_float(cfg("llm", "temperature"), "llm.temperature")
TOP_P = _as_float(cfg("llm", "top_p"), "llm.top_p")
SEED = _as_int(cfg("llm", "seed"), "llm.seed")


def _validate_control_config() -> None:
    removed = {
        "workflow": {"max_api_concurrent_reviews"},
        "queue": {"enabled", "task_soft_time_limit_seconds", "task_soft_time_limit"},
        "llm": {"max_context_tokens", "max_prompt_tokens", "max_result_tokens", "timeout_seconds", "global_semaphore_enabled", "max_global_concurrent_requests",
                "global_semaphore_redis_url", "global_semaphore_key", "global_semaphore_wait_timeout_seconds",
                "global_semaphore_lease_seconds", "global_semaphore_poll_interval_seconds"},
        "embedding": {"timeout_seconds"},
        "rerank": {"timeout", "timeout_seconds", "max_retries", "initial_retry_delay", "max_retry_delay"},
    }
    for section, keys in removed.items():
        for key in keys.intersection(CONFIG.get(section) or {}):
            raise RuntimeError(f"Removed configuration: {section}.{key}. See docs/CONCURRENCY_TIMEOUT_CONFIGURATION.md")
    semaphore = CONFIG.get("model_semaphore") or {}
    if not isinstance(semaphore, dict):
        raise RuntimeError("model_semaphore must contain per-type mappings.")
    legacy = set(semaphore) - {"llm", "embedding", "reranker"}
    if legacy:
        raise RuntimeError(f"Migrate shared model_semaphore fields {sorted(legacy)} to per-type configuration; remove enabled/wait_timeout_seconds.")
    docx = (CONFIG.get("retrieval") or {}).get("docxindex") or {}
    if "concurrency" in docx or "retry_count" in ((docx.get("indexing") or {}).get("hierarchy") or {}):
        raise RuntimeError("Remove retrieval.docxindex.concurrency and indexing.hierarchy.retry_count; use unified model control.")


def _model_semaphore_config(model_type: str, *, default_max: int) -> dict[str, Any]:
    section = (CONFIG.get("model_semaphore") or {}).get(model_type, {})
    prefix = f"model_semaphore.{model_type}"
    if not isinstance(section, dict):
        raise RuntimeError(f"{prefix} must be a mapping.")
    allowed = {"max_concurrent_requests", "max_requests_per_minute", "redis_url", "key", "lease_seconds", "poll_interval_seconds"}
    if set(section) - allowed:
        raise RuntimeError(f"Removed/unknown configuration in {prefix}: {sorted(set(section) - allowed)}")
    result = {
        "max_concurrent_requests": _limit(section.get("max_concurrent_requests"), prefix + ".max_concurrent_requests"),
        "max_requests_per_minute": _limit(section.get("max_requests_per_minute"), prefix + ".max_requests_per_minute"),
        "redis_url": _as_str(section.get("redis_url", "redis://redis:6379/2"), prefix + ".redis_url"),
        "key": _as_str(section.get("key", f"contract_agent:model:{model_type}:semaphore"), prefix + ".key"),
        "lease_seconds": _as_float(section.get("lease_seconds", 600), prefix + ".lease_seconds"),
        "poll_interval_seconds": _as_float(section.get("poll_interval_seconds", 0.2), prefix + ".poll_interval_seconds"),
    }
    if result["lease_seconds"] <= 0 or result["poll_interval_seconds"] <= 0 or not result["key"]:
        raise RuntimeError(f"{prefix}: lease, poll interval and resource key must be valid.")
    return result


_validate_control_config()
MODEL_SEMAPHORE_CONFIGS = {
    kind: _model_semaphore_config(kind, default_max=limit)
    for kind, limit in (("llm", 5), ("embedding", 3), ("reranker", 10))
}
if len({(value["redis_url"], value["key"]) for value in MODEL_SEMAPHORE_CONFIGS.values()}) != 3:
    raise RuntimeError("Each model type must have a distinct Redis semaphore key.")

EMBED_BASE_URL = _as_str(cfg("embedding", "base_url"), "embedding.base_url")
EMBED_NAME = (
    env_optional("EMBEDDING_MODEL_NAME")
    if API_REQUIRE_REQUEST_MODEL_CONFIG and env_optional("EMBEDDING_MODEL_NAME")
    else _as_str(cfg("embedding", "name"), "embedding.name")
)
EMBED_MAX_INPUT_TOKENS = _limit(cfg_optional("embedding", "max_input_tokens", None), "embedding.max_input_tokens")

RERANK_BASE_URL = _as_str(cfg("rerank", "base_url"), "rerank.base_url")
RERANK_NAME = (
    env_optional("RERANKER_MODEL_NAME")
    if API_REQUIRE_REQUEST_MODEL_CONFIG and env_optional("RERANKER_MODEL_NAME")
    else _as_str(cfg("rerank", "name"), "rerank.name")
)
RERANK_PROVIDER_MODEL_NAMES = _as_provider_model_names(
    cfg_optional("rerank", "provider_model_names", {
        "higress_qwen": ["Qwen-Reranker-8B", "Qwen3-Reranker-8B", "qwen3-reranker"],
        "bge": ["bge-reranker-v2-m3"],
        "dashscope_qwen": ["qwen3-rerank", "gte-rerank-v2"],
    }),
    "rerank.provider_model_names",
)
RERANK_PROVIDER = infer_rerank_provider(RERANK_NAME)
RERANK_INSTRUCT = _as_str(cfg("rerank", "instruct"), "rerank.instruct")
RERANK_MAX_INPUT_TOKENS = _limit(cfg_optional("rerank", "max_input_tokens", None), "rerank.max_input_tokens")
MODEL_INPUT_LIMITS = {"llm": LLM_MAX_INPUT_TOKENS, "embedding": EMBED_MAX_INPUT_TOKENS, "reranker": RERANK_MAX_INPUT_TOKENS}
if cfg_optional("rerank", "chunk_overlap_tokens", None) is not None:
    raise RuntimeError("Remove rerank.chunk_overlap_tokens; reranker candidates are prepared without overlap.")
RERANK_API_KEY = env_optional("RERANK_API_KEY")
if (
    RERANK_PROVIDER in {"higress_qwen", "dashscope_qwen"}
    and not RERANK_API_KEY
    and not API_REQUIRE_REQUEST_MODEL_CONFIG
):
    raise RuntimeError(
        f"Missing required .env field: RERANK_API_KEY for reranker provider {RERANK_PROVIDER!r} "
        "inferred from rerank.provider_model_names."
    )

MAX_REFLECTION_ROUNDS = _limit(
    cfg_optional("workflow", "max_reflection_rounds", None),
    "workflow.max_reflection_rounds",
)
MAX_ORCHESTRATOR_CONCURRENCY = _limit(
    cfg_optional("workflow", "max_orchestrator_concurrency", None),
    "workflow.max_orchestrator_concurrency",
)
MODEL_CALL_TIMEOUT_SECONDS = _limit(
    cfg_optional("workflow", "model_call_timeout_seconds", None),
    "workflow.model_call_timeout_seconds", integer=False,
)
MODEL_WAIT_TIMEOUT_SECONDS = _limit(
    cfg_optional("workflow", "model_wait_timeout_seconds", None),
    "workflow.model_wait_timeout_seconds", integer=False,
)
MODEL_CALL_MAX_RETRIES = _limit(
    cfg_optional("workflow", "model_call_max_retries", None),
    "workflow.model_call_max_retries", minimum=0,
)
CRITERION_RETRY_MAX_ATTEMPTS = _limit(
    cfg_optional("workflow", "criterion_retry_max_attempts", None),
    "workflow.criterion_retry_max_attempts",
)
SUBAGENT_ALLOWED_TOOLS = _as_str_tuple(
    cfg_optional("workflow", "subagent_allowed_tools", ["contract_search"]),
    "workflow.subagent_allowed_tools",
    allow_empty=True,
)
QUEUE_BROKER_URL = _as_str(
    cfg_optional("queue", "broker_url", "redis://localhost:6379/0"),
    "queue.broker_url",
)
QUEUE_WORKER_DISABLE_PREFETCH = _parse_bool(
    cfg_optional("queue", "worker_disable_prefetch", True),
    "queue.worker_disable_prefetch",
)
QUEUE_PENDING_TIMEOUT_SECONDS = _limit(
    cfg_optional("queue", "pending_timeout_seconds", None),
    "queue.pending_timeout_seconds", integer=False,
)
QUEUE_QUEUED_TIMEOUT_SECONDS = _limit(
    cfg_optional("queue", "queued_timeout_seconds", None),
    "queue.queued_timeout_seconds", integer=False,
)
QUEUE_WATCHDOG_INTERVAL_SECONDS = _as_float(
    cfg_optional("queue", "watchdog_interval_seconds", 10),
    "queue.watchdog_interval_seconds",
)
if QUEUE_WATCHDOG_INTERVAL_SECONDS <= 0:
    raise RuntimeError("queue.watchdog_interval_seconds must be greater than 0.")

QUEUE_TASK_TIME_LIMIT_SECONDS = _limit(
    cfg_optional("queue", "task_time_limit_seconds", None), "queue.task_time_limit_seconds",
)

RETRIEVAL_BACKEND = _as_str(cfg("retrieval", "backend"), "retrieval.backend").lower()
if RETRIEVAL_BACKEND not in {"llamaindex", "docxindex"}:
    raise RuntimeError("retrieval.backend must be 'llamaindex' or 'docxindex'.")
CHUNK_SIZE = _as_int(
    (CONFIG.get("retrieval", {}).get("llamaindex") or {}).get("chunk_size", 512),
    "retrieval.llamaindex.chunk_size",
)
CHUNK_OVERLAP = _as_int(
    (CONFIG.get("retrieval", {}).get("llamaindex") or {}).get("chunk_overlap", 100),
    "retrieval.llamaindex.chunk_overlap",
)
SIMILARITY_TOP_K = _limit(
    (CONFIG.get("retrieval", {}).get("llamaindex") or {}).get("similarity_top_k"),
    "retrieval.llamaindex.similarity_top_k",
)
RERANK_TOP_N = _limit(
    (CONFIG.get("retrieval", {}).get("llamaindex") or {}).get("rerank_top_n"),
    "retrieval.llamaindex.rerank_top_n",
)


def _validate_retrieval_values() -> None:
    """Reject malformed numeric values; pressure between valid budgets is WARN."""
    retrieval = CONFIG.get("retrieval") or {}
    docx = retrieval.get("docxindex") or {}
    indexing = docx.get("indexing") or {}
    sections = {
        "retrieval.docxindex": (docx, {"input_tokens": (500, True), "output_tokens": (500, True),
                                      "scan_batch_tokens": (500, True)}),
        "retrieval.docxindex.vector": (docx.get("vector") or {}, {"probe_limit": (1, True)}),
        "retrieval.docxindex.semantic": (docx.get("semantic") or {},
            {"structure_candidate_limit": (1, True), "max_selected_nodes": (1, True)}),
        "retrieval.docxindex.routing": (docx.get("routing") or {},
            {"llm_candidates": (1, True), "max_nodes": (1, True), "vector_candidates": (1, True)}),
        "retrieval.docxindex.indexing.summary": (indexing.get("summary") or {},
            {"batch_max_nodes": (1, True), "batch_max_tokens": (1000, True)}),
        "retrieval.docxindex.indexing.hierarchy": (indexing.get("hierarchy") or {},
            {"input_max_tokens": (1000, True), "batch_target_tokens": (1000, False),
             "batch_overlap_tokens": (0, False), "max_levels": (1, False)}),
        "retrieval.docxindex.indexing.paragraph": (indexing.get("paragraph") or {},
            {"split_threshold_tokens": (100, False), "chunk_target_tokens": (100, False)}),
        "retrieval.docxindex.indexing.table": (indexing.get("table") or {},
            {"split_threshold_tokens": (500, False), "chunk_target_tokens": (500, False)}),
        "retrieval.docxindex.indexing.attachment": (indexing.get("attachment") or {}, {"max_levels": (1, False)}),
    }
    for prefix, (section, fields) in sections.items():
        for field, (minimum, nullable) in fields.items():
            if field not in section:
                continue
            value = section[field]
            if value is None and nullable:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise RuntimeError(f"{prefix}.{field} must be an integer >= {minimum}"
                                   + (", or null." if nullable else "."))
            if field == "max_levels" and value > 6:
                raise RuntimeError(f"{prefix}.{field} must be between 1 and 6.")
    for section, field, prefix in ((docx.get("vector") or {}, "score_threshold", "retrieval.docxindex.vector"),
                                    (docx.get("routing") or {}, "vector_threshold", "retrieval.docxindex.routing")):
        if field in section:
            value = _as_float(section[field], prefix + "." + field)
            if not 0 <= value <= 1:
                raise RuntimeError(f"{prefix}.{field} must be between 0 and 1.")
    for name, fields in {"vector": ("enabled",), "rerank": ("enabled",),
                         "routing": ("fallback_enabled",)}.items():
        section = docx.get(name) or {}
        for field in fields:
            if field in section and not isinstance(_parse_bool(section[field], f"retrieval.docxindex.{name}.{field}"), bool):
                raise RuntimeError(f"retrieval.docxindex.{name}.{field} must be true or false.")
    overlap = _as_int(cfg_optional("embedding", "chunk_overlap_tokens", 32), "embedding.chunk_overlap_tokens")
    if overlap < 0:
        raise RuntimeError("embedding.chunk_overlap_tokens must be >= 0.")
    if CHUNK_SIZE <= 0 or CHUNK_OVERLAP < 0 or CHUNK_OVERLAP >= CHUNK_SIZE:
        raise RuntimeError("retrieval.llamaindex requires chunk_size > chunk_overlap >= 0.")


_validate_retrieval_values()

DATA_DIR = str(PROJECT_ROOT_PATH / "data")
USERS_FILE = str(PROJECT_ROOT_PATH / "profiles" / "users.json")

API_KEEP_INPUT = _parse_bool(cfg("api", "keep_input"), "api.keep_input")
API_KEEP_OUTPUT = _parse_bool(cfg("api", "keep_output"), "api.keep_output")
API_RESULT_OUTPUT_DEFAULT = _as_str(
    cfg("api", "result_output_default"),
    "api.result_output_default",
).lower()
if API_RESULT_OUTPUT_DEFAULT not in {"file", "url"}:
    raise RuntimeError("api.result_output_default must be 'file' or 'url'.")
API_RESULT_UPLOAD_ENABLED = _parse_bool(
    cfg("api", "result_upload_enabled"),
    "api.result_upload_enabled",
)
API_RESULT_UPLOAD_DOMAIN = _as_str(
    cfg("api", "result_upload_domain"),
    "api.result_upload_domain",
)
API_RESULT_UPLOAD_PATH = _as_str(
    cfg("api", "result_upload_path"),
    "api.result_upload_path",
)
API_RESULT_UPLOAD_AUTHORIZATION = _as_str(
    cfg("api", "result_upload_authorization"),
    "api.result_upload_authorization",
)
API_RESULT_UPLOAD_TIMEOUT_SECONDS = _limit(
    cfg_optional("api", "result_upload_timeout_seconds", None),
    "api.result_upload_timeout_seconds", integer=False,
)
API_META_REQUIRED = _parse_bool(cfg("api", "meta_required"), "api.meta_required")
API_META_FIELDS = _as_str_tuple(cfg("api", "meta_fields"), "api.meta_fields")
API_CALLBACK_ENABLED = _parse_bool(cfg("api", "callback_enabled"), "api.callback_enabled")
API_CALLBACK_URL = _as_str(cfg("api", "callback_url"), "api.callback_url")
API_CALLBACK_FILE_FIELD = _as_str(
    cfg("api", "callback_file_field"),
    "api.callback_file_field",
) or "file"
API_URL_DOWNLOAD_TIMEOUT_SECONDS = _limit(
    cfg_optional("api", "url_download_timeout_seconds", None),
    "api.url_download_timeout_seconds", integer=False,
)
API_URL_DOWNLOAD_MAX_BYTES = None if cfg_optional("api", "url_download_max_size", None) is None else _as_size_bytes(
    cfg_optional("api", "url_download_max_size", None),
    "api.url_download_max_size",
)

DOCX_COMMENT_INCLUDE_CRITERION = _parse_bool(
    cfg("docx", "comment_include_criterion"),
    "docx.comment_include_criterion",
)

LOGGING_ENABLED = _parse_bool(
    cfg("logging", "enabled"),
    "logging.enabled",
)
LOGGING_DETAILED = _parse_bool(cfg_optional("logging", "detailed", False), "logging.detailed")


def input_configuration_warnings() -> list[dict[str, Any]]:
    """Report expected pressure; only a final prepared request can be rejected."""
    warnings = []
    retrieval = CONFIG.get("retrieval") or {}
    if RETRIEVAL_BACKEND == "docxindex":
        docx = retrieval.get("docxindex") or {}
        indexing = docx.get("indexing") or {}
        budgets = {
            "retrieval.docxindex.input_tokens": docx.get("input_tokens"),
            "retrieval.docxindex.output_tokens": docx.get("output_tokens"),
            "retrieval.docxindex.scan_batch_tokens": docx.get("scan_batch_tokens"),
            "retrieval.docxindex.indexing.summary.batch_max_tokens": (indexing.get("summary") or {}).get("batch_max_tokens"),
            "retrieval.docxindex.indexing.hierarchy.input_max_tokens": (indexing.get("hierarchy") or {}).get("input_max_tokens"),
            "retrieval.docxindex.indexing.hierarchy.batch_target_tokens": (indexing.get("hierarchy") or {}).get("batch_target_tokens", 16000),
        }
        for field, budget in budgets.items():
            if LLM_MAX_INPUT_TOKENS is not None and budget is not None and budget >= LLM_MAX_INPUT_TOKENS:
                warnings.append({"field": field, "budget": budget, "input_limit": LLM_MAX_INPUT_TOKENS,
                                 "message": "Content budget reaches LLM input limit before prompt, query, tools and history are counted."})
        hierarchy = indexing.get("hierarchy") or {}
        hierarchy_limit = hierarchy.get("input_max_tokens")
        target = hierarchy.get("batch_target_tokens", 16000)
        if hierarchy_limit is not None and target > hierarchy_limit:
            warnings.append({"field": "retrieval.docxindex.indexing.hierarchy.batch_target_tokens",
                             "budget": target, "input_limit": hierarchy_limit,
                             "message": "Hierarchy batch target exceeds its content budget; final request remains subject to LLM input validation."})
    else:
        settings = retrieval.get("llamaindex") or {}
        chunk_size = settings.get("chunk_size", 512)
        if EMBED_MAX_INPUT_TOKENS is not None and chunk_size >= EMBED_MAX_INPUT_TOKENS:
            warnings.append({"field": "retrieval.llamaindex.chunk_size", "budget": chunk_size,
                             "input_limit": EMBED_MAX_INPUT_TOKENS, "message": "Chunk target reaches embedding input limit before model wrapping."})
    overlap = cfg_optional("embedding", "chunk_overlap_tokens", 32)
    if isinstance(overlap, (int, float)) and EMBED_MAX_INPUT_TOKENS is not None and overlap >= EMBED_MAX_INPUT_TOKENS:
        warnings.append({"field": "embedding.chunk_overlap_tokens", "budget": overlap,
                         "input_limit": EMBED_MAX_INPUT_TOKENS, "message": "Overlap reaches input limit; slicing reduces overlap to ensure progress."})
    return warnings


for _warning in input_configuration_warnings():
    print("[config] WARN input-budget " + json.dumps(_warning, ensure_ascii=False), file=sys.stderr)

print(f"[config] Retrieval mode : {RETRIEVAL_BACKEND} temporary contract retrieval (MCP)")
print(
    f"[config] LLM            : {LLM_NAME}  "
    f"(input_limit={LLM_MAX_INPUT_TOKENS}  tools={MAX_TOOL_CALLS})"
)


def control_configuration_summary() -> dict[str, Any]:
    def safe_redis_url(url):
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 6379}{parsed.path}"
    return {
        "llm": {"stream": LLM_STREAM, "max_input_tokens": LLM_MAX_INPUT_TOKENS},
        "model_input_limits": MODEL_INPUT_LIMITS,
        "tokenizers": TOKENIZER_MODEL_NAMES,
        "queue": {
            "broker": safe_redis_url(QUEUE_BROKER_URL),
            "pending_timeout_seconds": QUEUE_PENDING_TIMEOUT_SECONDS,
            "queued_timeout_seconds": QUEUE_QUEUED_TIMEOUT_SECONDS,
            "watchdog_interval_seconds": QUEUE_WATCHDOG_INTERVAL_SECONDS,
            "task_time_limit_seconds": QUEUE_TASK_TIME_LIMIT_SECONDS,
            "soft_time_limit": "disabled",
            "worker_disable_prefetch": QUEUE_WORKER_DISABLE_PREFETCH,
            "worker_prefetch_multiplier": 1,
            "task_acks_late": False,
            "visibility_timeout_seconds": 3600,
        },
        "workflow": {"criterion_concurrency_per_contract": MAX_ORCHESTRATOR_CONCURRENCY,
                     "criterion_retry_max_attempts": CRITERION_RETRY_MAX_ATTEMPTS,
                     "model_wait_timeout_seconds": MODEL_WAIT_TIMEOUT_SECONDS,
                     "model_request_timeout_seconds": MODEL_CALL_TIMEOUT_SECONDS,
                     "llm_timeout_mode": "content_idle" if LLM_STREAM else "request_total",
                     "model_extra_send_limit": MODEL_CALL_MAX_RETRIES},
        "model_semaphore": {
            kind: {**{key: value for key, value in raw.items() if key != "redis_url"},
                   "redis_url": safe_redis_url(raw["redis_url"]),
                   "lease_auto_renew": True,
                   "lease_renew_interval_seconds": raw["lease_seconds"] / 3,
                   "key_ttl_seconds": max(120, math.ceil(raw["lease_seconds"]) + 60),
                   "waiter_heartbeat_timeout_seconds": max(2.0, min(raw["lease_seconds"], 30.0))}
            for kind, raw in MODEL_SEMAPHORE_CONFIGS.items()
        },
        "deployment_contract": {"worker_count": 3, "concurrency_per_worker": 1, "pool": "prefork"},
        "internal": {"redis_cleanup_timeout_seconds": 2,
                     "heartbeat_interval_seconds": 10, "task_lock_wait_seconds": 30,
                     "deadline_reconciliation_margin_seconds": 5},
    }


print("[config] execution-control " + json.dumps(control_configuration_summary(), ensure_ascii=False), file=sys.stderr)
