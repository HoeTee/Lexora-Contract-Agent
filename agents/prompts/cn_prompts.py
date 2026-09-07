from __future__ import annotations

from pathlib import Path

import yaml


PROMPTS_PATH = Path(__file__).with_name("cn_prompts.yaml")

_REQUIRED_PROMPTS = (
    "PLANNER_SYSTEM_PROMPT",
    "PLANNER_STRATEGY_PROMPT",
    "SUB_AGENT_BASE_PROMPT",
    "REFLECTOR_SYSTEM_PROMPT",
    "SUMMARIZER_SYSTEM_PROMPT",
)


def load_prompts(path: Path) -> dict[str, str]:
    """Load and validate the externally managed system prompts."""
    if not path.is_file():
        raise RuntimeError(f"Prompt file does not exist: {path}")

    try:
        with path.open("r", encoding="utf-8") as prompt_file:
            data = yaml.safe_load(prompt_file)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid prompt YAML: {path}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Prompt YAML must contain a top-level mapping")

    prompts: dict[str, str] = {}
    for name in _REQUIRED_PROMPTS:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Missing or invalid prompt: {name}")
        prompts[name] = value

    return prompts


_PROMPTS = load_prompts(PROMPTS_PATH)

PLANNER_SYSTEM_PROMPT = _PROMPTS["PLANNER_SYSTEM_PROMPT"]
PLANNER_STRATEGY_PROMPT = _PROMPTS["PLANNER_STRATEGY_PROMPT"]
SUB_AGENT_BASE_PROMPT = _PROMPTS["SUB_AGENT_BASE_PROMPT"]
REFLECTOR_SYSTEM_PROMPT = _PROMPTS["REFLECTOR_SYSTEM_PROMPT"]
SUMMARIZER_SYSTEM_PROMPT = _PROMPTS["SUMMARIZER_SYSTEM_PROMPT"]
