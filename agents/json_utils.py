"""
Helpers for extracting, validating, and repairing JSON agent outputs.
"""

import json
from collections import Counter
from itertools import count
from config import MODEL_CALL_MAX_RETRIES
from contextlib import contextmanager, nullcontext
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from agents.prompts import get_prompt, render_prompt


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
    except json.JSONDecodeError as original_error:
        start = stripped.find("{")
        end = stripped.rfind("}") + 1
        if start == -1 or end <= start:
            raise
        try:
            parsed = _loads_json(stripped[start:end])
        except json.JSONDecodeError:
            # A truncated JSON object can contain earlier nested closing braces.
            # Report the full-object error, not an error introduced by cropping.
            if start == 0:
                raise original_error from None
            raise

    if not isinstance(parsed, dict):
        raise ValueError("agent output JSON must be an object")
    return parsed


def _validation_feedback(error: Exception) -> str:
    """Summarize repeated field failures without echoing input objects or URLs."""
    if not isinstance(error, ValidationError):
        return str(error)[:4000]
    groups: Counter[tuple[str, str]] = Counter()
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join("[]" if isinstance(part, int) else str(part) for part in item["loc"])
        groups[(location.replace(".[]", "[]"), item["msg"])] += 1
    lines = [
        render_prompt("JSON_VALIDATION_ERROR_ITEM", field=field, message=message[:400], count=count)
        for (field, message), count in list(groups.items())[:12]
    ]
    if len(groups) > 12:
        lines.append(get_prompt("JSON_VALIDATION_ERRORS_TRUNCATED"))
    return "\n".join(lines)


def _repair_prompt(error: Exception, expected_format: str) -> str:
    return render_prompt(
        "JSON_REPAIR_PROMPT", errors=_validation_feedback(error), expected_format=expected_format
    )


async def chat_until_valid_json(
    agent,
    initial_prompt: str,
    schema: type[ModelT],
    expected_format: str,
    *,
    max_attempts: int | None = None,
    final_attempt_schema: type[BaseModel] | None = None,
    validation_context: dict | None = None,
) -> tuple[dict, str]:
    """Validate agent JSON, optionally relaxing only the final parsed response."""
    scope = agent.validation_requests() if hasattr(agent, "validation_requests") else nullcontext()
    with scope:
        limits = [value for value in (max_attempts,
                  MODEL_CALL_MAX_RETRIES + 1 if MODEL_CALL_MAX_RETRIES is not None else None)
                  if value is not None]
        return await _chat_until_valid_json(
            agent, initial_prompt, schema, expected_format,
            max_attempts=min(limits) if limits else None,
            final_attempt_schema=final_attempt_schema, validation_context=validation_context,
        )


async def _chat_until_valid_json(
    agent, initial_prompt: str, schema: type[ModelT], expected_format: str, *,
    max_attempts: int | None, final_attempt_schema: type[BaseModel] | None,
    validation_context: dict | None,
) -> tuple[dict, str]:
    prompt = render_prompt(
        "JSON_INITIAL_REQUEST_PROMPT", initial_prompt=initial_prompt, expected_format=expected_format
    )
    last_error: Exception | None = None
    raw_output = ""

    attempts = count(1) if max_attempts is None else range(1, max_attempts + 1)
    for attempt in attempts:
        raw_output = await agent.chat(prompt)
        parsed: dict | None = None
        try:
            parsed = extract_json(raw_output)
            validated = schema.model_validate(parsed, context=validation_context)
            return validated.model_dump(), raw_output
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            if attempt == max_attempts:
                if final_attempt_schema is not None and parsed is not None:
                    try:
                        fallback = final_attempt_schema.model_validate(parsed, context=validation_context)
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
