"""
ReflectorAgent validates sub-agent review quality.
"""

from agents.base_agent import Agent
from agents.json_utils import chat_until_valid_json
from agents.prompts import get_prompt, render_prompt
from agents.schemas import ReflectorOutput


REFLECTOR_SYSTEM_PROMPT = get_prompt("REFLECTOR_SYSTEM_PROMPT")
REFLECTOR_EXPECTED_JSON = get_prompt("REFLECTOR_EXPECTED_JSON")


class ReflectorAgent(Agent):
    """Reviews sub-agent output and returns PASS/REJECT with feedback."""

    def __init__(self, criterion_id: str | None = None, **kwargs):
        super().__init__(
            system_prompt=REFLECTOR_SYSTEM_PROMPT,
            name=f"Reflector_{criterion_id}" if criterion_id else "Reflector",
            criterion_id=criterion_id,
            **kwargs,
        )

    async def review(self, agent_output: str, missing_text_review_notes: str = "") -> dict:
        """
        Review a sub-agent's output.
        Returns: {"status": "PASS"/"REJECT", "feedback": "..."}
        """
        prompt = render_prompt(
            "REFLECTOR_REVIEW_TASK_PROMPT",
            agent_output=agent_output,
            missing_text_review_notes=missing_text_review_notes,
        )
        parsed, _ = await chat_until_valid_json(
            self,
            prompt,
            ReflectorOutput,
            REFLECTOR_EXPECTED_JSON,
        )
        return parsed
