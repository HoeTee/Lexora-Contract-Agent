"""
Central configuration for the contract review workflow.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

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
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field_name} must be an integer, got {value!r}.") from exc


def _as_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field_name} must be a number, got {value!r}.") from exc


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
MCP_SERVER_URL = "http://localhost:8000/mcp"
DOCS_DIR = str(PROJECT_ROOT_PATH / "docs")
REPORTS_DIR = str(PROJECT_ROOT_PATH / "reports")
LOGS_DIR = str(PROJECT_ROOT_PATH / "logs" / "workflow")

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


def _model_semaphore_section(model_type: str) -> dict[str, Any]:
    section_data = CONFIG.get("model_semaphore")
    if isinstance(section_data, dict) and isinstance(section_data.get(model_type), dict):
        return section_data[model_type]
    return {}


def _model_semaphore_value(model_type: str, key: str, default: Any) -> Any:
    section_data = _model_semaphore_section(model_type)
    if key in section_data:
        return section_data[key]
    if model_type == "llm":
        legacy_key_map = {
            "enabled": "global_semaphore_enabled",
            "max_concurrent_requests": "max_global_concurrent_requests",
            "redis_url": "global_semaphore_redis_url",
            "key": "global_semaphore_key",
            "wait_timeout_seconds": "global_semaphore_wait_timeout_seconds",
            "lease_seconds": "global_semaphore_lease_seconds",
            "poll_interval_seconds": "global_semaphore_poll_interval_seconds",
        }
        legacy_key = legacy_key_map[key]
        return cfg_optional("llm", legacy_key, default)
    return default


def _model_semaphore_config(model_type: str, *, default_max: int) -> dict[str, Any]:
    field_prefix = f"model_semaphore.{model_type}"
    return {
        "enabled": bool(
            _parse_bool(
                _model_semaphore_value(model_type, "enabled", False),
                f"{field_prefix}.enabled",
            )
        ),
        "max_concurrent_requests": _as_int(
            _model_semaphore_value(model_type, "max_concurrent_requests", default_max),
            f"{field_prefix}.max_concurrent_requests",
        ),
        "redis_url": _as_str(
            _model_semaphore_value(
                model_type,
                "redis_url",
                cfg_optional("queue", "broker_url", "redis://localhost:6379/0"),
            ),
            f"{field_prefix}.redis_url",
        ),
        "key": _as_str(
            _model_semaphore_value(
                model_type,
                "key",
                f"contract_agent:model:{model_type}:semaphore",
            ),
            f"{field_prefix}.key",
        ),
        "wait_timeout_seconds": _as_float(
            _model_semaphore_value(model_type, "wait_timeout_seconds", 600),
            f"{field_prefix}.wait_timeout_seconds",
        ),
        "lease_seconds": _as_float(
            _model_semaphore_value(model_type, "lease_seconds", 600),
            f"{field_prefix}.lease_seconds",
        ),
        "poll_interval_seconds": _as_float(
            _model_semaphore_value(model_type, "poll_interval_seconds", 0.2),
            f"{field_prefix}.poll_interval_seconds",
        ),
    }


MODEL_SEMAPHORE_CONFIGS = {
    "llm": _model_semaphore_config("llm", default_max=10),
    "embedding": _model_semaphore_config("embedding", default_max=5),
    "reranker": _model_semaphore_config("reranker", default_max=5),
}
LLM_GLOBAL_SEMAPHORE_ENABLED = MODEL_SEMAPHORE_CONFIGS["llm"]["enabled"]
LLM_GLOBAL_SEMAPHORE_REDIS_URL = MODEL_SEMAPHORE_CONFIGS["llm"]["redis_url"]
LLM_GLOBAL_SEMAPHORE_KEY = MODEL_SEMAPHORE_CONFIGS["llm"]["key"]
LLM_GLOBAL_SEMAPHORE_MAX_REQUESTS = MODEL_SEMAPHORE_CONFIGS["llm"]["max_concurrent_requests"]
LLM_GLOBAL_SEMAPHORE_WAIT_TIMEOUT_SECONDS = MODEL_SEMAPHORE_CONFIGS["llm"]["wait_timeout_seconds"]
LLM_GLOBAL_SEMAPHORE_LEASE_SECONDS = MODEL_SEMAPHORE_CONFIGS["llm"]["lease_seconds"]
LLM_GLOBAL_SEMAPHORE_POLL_INTERVAL_SECONDS = MODEL_SEMAPHORE_CONFIGS["llm"]["poll_interval_seconds"]

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
    cfg("rerank", "provider_model_names"),
    "rerank.provider_model_names",
)
RERANK_PROVIDER = infer_rerank_provider(RERANK_NAME)
RERANK_INSTRUCT = _as_str(cfg("rerank", "instruct"), "rerank.instruct")
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
MAX_API_CONCURRENT_REVIEWS = _as_int(
    cfg("workflow", "max_api_concurrent_reviews"),
    "workflow.max_api_concurrent_reviews",
)
MODEL_CALL_TIMEOUT_SECONDS = _as_float(
    cfg("workflow", "model_call_timeout_seconds"),
    "workflow.model_call_timeout_seconds",
)
MODEL_CALL_MAX_RETRIES = _as_int(
    cfg("workflow", "model_call_max_retries"),
    "workflow.model_call_max_retries",
)
CRITERION_RETRY_MAX_ATTEMPTS = _as_int(
    cfg_optional("workflow", "criterion_retry_max_attempts", 2),
    "workflow.criterion_retry_max_attempts",
)
SUBAGENT_ALLOWED_TOOLS = _as_str_tuple(
    cfg_optional("workflow", "subagent_allowed_tools", ["contract_search"]),
    "workflow.subagent_allowed_tools",
    allow_empty=True,
)
QUEUE_ENABLED = _parse_bool(cfg_optional("queue", "enabled", False), "queue.enabled")
QUEUE_BROKER_URL = _as_str(
    cfg_optional("queue", "broker_url", "redis://localhost:6379/0"),
    "queue.broker_url",
)

RETRIEVAL_BACKEND = _as_str(cfg("retrieval", "backend"), "retrieval.backend").lower()
if RETRIEVAL_BACKEND not in {"llamaindex", "docxindex"}:
    raise RuntimeError("retrieval.backend must be 'llamaindex' or 'docxindex'.")
CHUNK_SIZE = _as_int(
    cfg_nested("retrieval", "llamaindex", "chunk_size"),
    "retrieval.llamaindex.chunk_size",
)
CHUNK_OVERLAP = _as_int(
    cfg_nested("retrieval", "llamaindex", "chunk_overlap"),
    "retrieval.llamaindex.chunk_overlap",
)
SIMILARITY_TOP_K = _as_int(
    cfg_nested("retrieval", "llamaindex", "similarity_top_k"),
    "retrieval.llamaindex.similarity_top_k",
)
RERANK_TOP_N = _as_int(
    cfg_nested("retrieval", "llamaindex", "rerank_top_n"),
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

print(f"[config] Retrieval mode : {RETRIEVAL_BACKEND} temporary contract retrieval (MCP)")
print(
    f"[config] LLM            : {LLM_NAME}  "
    f"(ctx={MAX_CONTEXT_TOKENS:,}  prompt<={MAX_PROMPT_TOKENS:,}  "
    f"out={MAX_RESULT_TOKENS:,}  tools={MAX_TOOL_CALLS})"
)
