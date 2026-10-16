"""Load all agent instructions and templates from the adjacent YAML catalog."""

from pathlib import Path
from string import Template

import yaml


PROMPTS_PATH = Path(__file__).with_name("prompts.yaml")
_REQUIRED_PROMPTS = (
    "PLANNER_SYSTEM_PROMPT",
    "PLANNER_STRATEGY_PROMPT",
    "SUB_AGENT_BASE_PROMPT",
    "REFLECTOR_SYSTEM_PROMPT",
    "SUMMARIZER_SYSTEM_PROMPT",
)


def load_prompts(path: Path = PROMPTS_PATH) -> dict[str, str]:
    """Read a YAML path and return nonempty named prompt strings."""
    if not path.is_file():
        raise RuntimeError(f"Prompt file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Invalid prompt YAML: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Prompt YAML must contain a top-level mapping")
    for name in _REQUIRED_PROMPTS:
        if name not in data:
            raise RuntimeError(f"Missing or invalid prompt: {name}")
    for name, value in data.items():
        if not isinstance(name, str) or not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"Missing or invalid prompt: {name}")
    return data


_PROMPTS = load_prompts()


def get_prompt(name: str) -> str:
    """Return one catalog entry, failing explicitly for an unknown name."""
    try:
        return _PROMPTS[name]
    except KeyError as exc:
        raise RuntimeError(f"Unknown agent prompt: {name}") from exc


def render_prompt(name: str, **values: object) -> str:
    """Substitute ${name} slots once; leave JSON braces and inserted text intact."""
    try:
        return Template(get_prompt(name)).substitute(values)
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Invalid template arguments for agent prompt {name}: {exc}") from exc


PLANNER_SYSTEM_PROMPT = get_prompt("PLANNER_SYSTEM_PROMPT")
PLANNER_STRATEGY_PROMPT = get_prompt("PLANNER_STRATEGY_PROMPT")
SUB_AGENT_BASE_PROMPT = get_prompt("SUB_AGENT_BASE_PROMPT")
REFLECTOR_SYSTEM_PROMPT = get_prompt("REFLECTOR_SYSTEM_PROMPT")
SUMMARIZER_SYSTEM_PROMPT = get_prompt("SUMMARIZER_SYSTEM_PROMPT")
