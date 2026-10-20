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


MCP_SERVER_TARGET = "module:mcp_service.server.server"

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
MAX_CONTEXT_TOKENS = _as_int(cfg("llm", "max_context_tokens"), "llm.max_context_tokens")
MAX_PROMPT_TOKENS = _as_int(
    cfg_optional("llm", "max_prompt_tokens", 88_000),
    "llm.max_prompt_tokens",
)
if not 0 < MAX_PROMPT_TOKENS < 90_000:
    raise RuntimeError("llm.max_prompt_tokens must be greater than 0 and lower than 90000")
MAX_RESULT_TOKENS = _as_int(cfg("llm", "max_result_tokens"), "llm.max_result_tokens")
MAX_TOOL_CALLS = _as_int(cfg("llm", "max_tool_calls"), "llm.max_tool_calls")
TEMPERATURE = _as_float(cfg("llm", "temperature"), "llm.temperature")
TOP_P = _as_float(cfg("llm", "top_p"), "llm.top_p")
SEED = _as_int(cfg("llm", "seed"), "llm.seed")


def _validate_control_config() -> None:
    removed = {
        "workflow": {"max_api_concurrent_reviews"},
        "queue": {"enabled", "task_soft_time_limit_seconds", "task_soft_time_limit"},
        "llm": {"timeout_seconds", "global_semaphore_enabled", "max_global_concurrent_requests",
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
    section = (CONFIG.get("model_semaphore") or {}).get(model_type) or {}
    prefix = f"model_semaphore.{model_type}"
    if not isinstance(section, dict):
        raise RuntimeError(f"{prefix} must be a mapping.")
    allowed = {"max_concurrent_requests", "redis_url", "key", "lease_seconds", "poll_interval_seconds"}
    if set(section) - allowed:
        raise RuntimeError(f"Removed/unknown configuration in {prefix}: {sorted(set(section) - allowed)}")
    result = {
        "max_concurrent_requests": _as_int(section.get("max_concurrent_requests", default_max), prefix + ".max_concurrent_requests"),
        "redis_url": _as_str(section.get("redis_url", "redis://redis:6379/2"), prefix + ".redis_url"),
        "key": _as_str(section.get("key", f"contract_agent:model:{model_type}:semaphore"), prefix + ".key"),
        "lease_seconds": _as_float(section.get("lease_seconds", 600), prefix + ".lease_seconds"),
        "poll_interval_seconds": _as_float(section.get("poll_interval_seconds", 0.2), prefix + ".poll_interval_seconds"),
    }
    timeout = _as_float(cfg("workflow", "model_call_timeout_seconds"), "workflow.model_call_timeout_seconds")
    if timeout <= 0 or result["lease_seconds"] < timeout + 5:
        raise RuntimeError(f"{prefix}.lease_seconds must cover model_call_timeout_seconds plus 5 seconds cleanup allowance.")
    if result["max_concurrent_requests"] < 1 or result["poll_interval_seconds"] <= 0 or not result["key"]:
        raise RuntimeError(f"{prefix}: concurrency and poll interval must be positive.")
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

RERANK_BASE_URL = _as_str(cfg("rerank", "base_url"), "rerank.base_url")
RERANK_NAME = (
    env_optional("RERANKER_MODEL_NAME")
    if API_REQUIRE_REQUEST_MODEL_CONFIG and env_optional("RERANKER_MODEL_NAME")
    else _as_str(cfg("rerank", "name"), "rerank.name")
)
RERANK_PROVIDER_MODEL_NAMES = _as_provider_model_names(
    cfg_optional("rerank", "provider_model_names", {
        "higress_qwen": ["Qwen3-Reranker-8B", "qwen3-reranker"],
        "bge": ["bge-reranker-v2-m3"],
        "dashscope_qwen": ["qwen3-rerank", "gte-rerank-v2"],
    }),
    "rerank.provider_model_names",
)
RERANK_PROVIDER = infer_rerank_provider(RERANK_NAME)
RERANK_INSTRUCT = _as_str(cfg("rerank", "instruct"), "rerank.instruct")
RERANK_MAX_INPUT_TOKENS = _as_int(cfg_optional("rerank", "max_input_tokens", 20000), "rerank.max_input_tokens")
if RERANK_MAX_INPUT_TOKENS <= 0:
    raise RuntimeError("rerank.max_input_tokens must be greater than 0.")
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

MAX_REFLECTION_ROUNDS = _as_int(
    cfg("workflow", "max_reflection_rounds"),
    "workflow.max_reflection_rounds",
)
MAX_ORCHESTRATOR_CONCURRENCY = _as_int(
    cfg("workflow", "max_orchestrator_concurrency"),
    "workflow.max_orchestrator_concurrency",
)
MODEL_CALL_TIMEOUT_SECONDS = _as_float(
    cfg("workflow", "model_call_timeout_seconds"),
    "workflow.model_call_timeout_seconds",
)
MODEL_WAIT_TIMEOUT_SECONDS = _as_float(
    cfg_optional("workflow", "model_wait_timeout_seconds", 180),
    "workflow.model_wait_timeout_seconds",
)
if MODEL_WAIT_TIMEOUT_SECONDS <= 0:
    raise RuntimeError("workflow.model_wait_timeout_seconds must be greater than 0.")
MODEL_CALL_MAX_RETRIES = _as_int(
    cfg("workflow", "model_call_max_retries"),
    "workflow.model_call_max_retries",
)
CRITERION_RETRY_MAX_ATTEMPTS = _as_int(
    cfg_optional("workflow", "criterion_retry_max_attempts", 3),
    "workflow.criterion_retry_max_attempts",
)
if CRITERION_RETRY_MAX_ATTEMPTS < 1:
    raise RuntimeError("workflow.criterion_retry_max_attempts must be a positive integer.")
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
QUEUE_PENDING_TIMEOUT_SECONDS = _as_float(
    cfg_optional("queue", "pending_timeout_seconds", 60),
    "queue.pending_timeout_seconds",
)
QUEUE_QUEUED_TIMEOUT_SECONDS = _as_float(
    cfg_optional("queue", "queued_timeout_seconds", 7200),
    "queue.queued_timeout_seconds",
)
QUEUE_WATCHDOG_INTERVAL_SECONDS = _as_float(
    cfg_optional("queue", "watchdog_interval_seconds", 10),
    "queue.watchdog_interval_seconds",
)

QUEUE_TASK_TIME_LIMIT_SECONDS = _as_int(
    cfg_optional("queue", "task_time_limit_seconds", 1800), "queue.task_time_limit_seconds",
)
if QUEUE_TASK_TIME_LIMIT_SECONDS <= 0 or MODEL_CALL_MAX_RETRIES < 0 or MAX_ORCHESTRATOR_CONCURRENCY < 1:
    raise RuntimeError("Task time limit and criterion concurrency must be positive; model retries cannot be negative.")

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
SIMILARITY_TOP_K = _as_int(
    (CONFIG.get("retrieval", {}).get("llamaindex") or {}).get("similarity_top_k", 10),
    "retrieval.llamaindex.similarity_top_k",
)
RERANK_TOP_N = _as_int(
    (CONFIG.get("retrieval", {}).get("llamaindex") or {}).get("rerank_top_n", 5),
    "retrieval.llamaindex.rerank_top_n",
)

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
API_RESULT_UPLOAD_TIMEOUT_SECONDS = _as_float(
    cfg("api", "result_upload_timeout_seconds"),
    "api.result_upload_timeout_seconds",
)
API_META_REQUIRED = _parse_bool(cfg("api", "meta_required"), "api.meta_required")
API_META_FIELDS = _as_str_tuple(cfg("api", "meta_fields"), "api.meta_fields")
API_CALLBACK_ENABLED = _parse_bool(cfg("api", "callback_enabled"), "api.callback_enabled")
API_CALLBACK_URL = _as_str(cfg("api", "callback_url"), "api.callback_url")
API_CALLBACK_FILE_FIELD = _as_str(
    cfg("api", "callback_file_field"),
    "api.callback_file_field",
) or "file"
API_URL_DOWNLOAD_TIMEOUT_SECONDS = _as_float(
    cfg("api", "url_download_timeout_seconds"),
    "api.url_download_timeout_seconds",
)
API_URL_DOWNLOAD_MAX_BYTES = _as_size_bytes(
    cfg("api", "url_download_max_size"),
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

print(f"[config] Retrieval mode : {RETRIEVAL_BACKEND} temporary contract retrieval (MCP)")
print(
    f"[config] LLM            : {LLM_NAME}  "
    f"(ctx={MAX_CONTEXT_TOKENS:,}  prompt<={MAX_PROMPT_TOKENS:,}  "
    f"out={MAX_RESULT_TOKENS:,}  tools={MAX_TOOL_CALLS})"
)


def control_configuration_summary() -> dict[str, Any]:
    def safe_redis_url(url):
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 6379}{parsed.path}"
    return {
        "llm": {"stream": LLM_STREAM},
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
                   "key_ttl_seconds": int(raw["lease_seconds"]) + 60}
            for kind, raw in MODEL_SEMAPHORE_CONFIGS.items()
        },
        "deployment_contract": {"worker_count": 3, "concurrency_per_worker": 1, "pool": "prefork"},
        "internal": {"redis_cleanup_timeout_seconds": 2, "minimum_lease_margin_seconds": 5,
                     "heartbeat_interval_seconds": 10, "task_lock_wait_seconds": 30,
                     "deadline_reconciliation_margin_seconds": 5},
    }


print("[config] execution-control " + json.dumps(control_configuration_summary(), ensure_ascii=False), file=sys.stderr)
