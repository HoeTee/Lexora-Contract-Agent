"""
Helpers for extracting, validating, and repairing JSON agent outputs.
"""

import json
from config import MODEL_CALL_MAX_RETRIES
from contextlib import contextmanager, nullcontext
from typing import TypeVar

from pydantic import BaseModel, ValidationError


ModelT = TypeVar("ModelT", bound=BaseModel)


@contextmanager
def agent_call_purpose(agent, stage: str, reason: str | None = None):
    previous = (getattr(agent, "call_stage", "model"), getattr(agent, "call_reason", None))
    agent.call_stage, agent.call_reason = stage, reason
    try:
        yield
    finally:
        agent.call_stage, agent.call_reason = previous


class AgentOutputValidationError(ValueError):
    """Raised when an agent exhausts JSON schema-repair attempts."""


def _loads_json(text: str):
    """Parse JSON, allowing only literal TAB, CR, and LF as a controlled fallback."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if not exc.msg.startswith("Invalid control character"):
            raise
        if any(ord(character) < 0x20 and character not in "\t\r\n" for character in text):
            raise
        return json.loads(text, strict=False)


def extract_json(text: str) -> dict:
    """Extract a JSON object from raw LLM output."""
    stripped = text.strip()

    if "```json" in stripped:
        stripped = stripped.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in stripped:
        stripped = stripped.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        parsed = _loads_json(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}") + 1
        if start == -1 or end <= start:
            raise
        parsed = _loads_json(stripped[start:end])

    if not isinstance(parsed, dict):
        raise ValueError("agent output JSON must be an object")
    return parsed


def _repair_prompt(error: Exception, expected_format: str) -> str:
    return (
        "你的上一次输出没有通过 JSON 格式校验。\n\n"
        f"校验错误如下：\n{error}\n\n"
        "请严格修正为下面要求的数据结构，只返回 JSON，不要返回 Markdown、解释、代码块或额外字段：\n\n"
        f"{expected_format}"
    )


async def chat_until_valid_json(
    agent,
    initial_prompt: str,
    schema: type[ModelT],
    expected_format: str,
    *,
    max_attempts: int | None = None,
    final_attempt_schema: type[BaseModel] | None = None,
) -> tuple[dict, str]:
    """Validate agent JSON, optionally relaxing only the final parsed response."""
    scope = agent.validation_requests() if hasattr(agent, "validation_requests") else nullcontext()
    with scope:
        return await _chat_until_valid_json(
            agent, initial_prompt, schema, expected_format,
            max_attempts=min(max_attempts or MODEL_CALL_MAX_RETRIES + 1, MODEL_CALL_MAX_RETRIES + 1), final_attempt_schema=final_attempt_schema,
        )


async def _chat_until_valid_json(
    agent, initial_prompt: str, schema: type[ModelT], expected_format: str, *,
    max_attempts: int, final_attempt_schema: type[BaseModel] | None,
) -> tuple[dict, str]:
    prompt = initial_prompt
    last_error: Exception | None = None
    raw_output = ""

    for attempt in range(1, max_attempts + 1):
        raw_output = await agent.chat(prompt)
        parsed: dict | None = None
        try:
            parsed = extract_json(raw_output)
            validated = schema.model_validate(parsed)
            return validated.model_dump(), raw_output
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            if attempt == max_attempts:
                if final_attempt_schema is not None and parsed is not None:
                    try:
                        fallback = final_attempt_schema.model_validate(parsed)
                        return fallback.model_dump(), raw_output
                    except (ValidationError, ValueError) as fallback_exc:
                        last_error = fallback_exc
                if hasattr(agent, "reject_schema_response"):
                    agent.reject_schema_response(last_error)
                break
            if hasattr(agent, "reject_schema_response"):
                agent.reject_schema_response(exc)
            prompt = _repair_prompt(exc, expected_format)

    raise AgentOutputValidationError(
        f"{agent.name} output failed schema validation after {max_attempts} attempts: {last_error}"
    )
