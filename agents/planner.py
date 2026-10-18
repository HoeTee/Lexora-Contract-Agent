"""
PlannerAgent splits criteria DOCX deterministically and extracts per-criterion
retrieval routes and placement targets via one batched LLM call per review run.
"""

from __future__ import annotations

import json

from agents.base_agent import Agent
from agents.json_utils import chat_until_valid_json
from agents.prompts import get_prompt, render_prompt
from agents.schemas import StrategyExtractionOutput
from routing.criteria import split_criteria_from_docx
from routing.schemas import ReviewExecutionPlan


__all__ = ["PlannerAgent", "assemble_tasks", "split_criteria_from_docx"]


PLANNER_STRATEGY_PROMPT = get_prompt("PLANNER_STRATEGY_PROMPT")
STRATEGY_EXPECTED_JSON = get_prompt("PLANNER_STRATEGY_EXPECTED_JSON")


class PlannerAgent(Agent):
    """Extracts per-criterion route/placement strategies from split skeletons."""

    def __init__(self, **kwargs):
        super().__init__(
            system_prompt=PLANNER_STRATEGY_PROMPT,
            name="Planner",
            **kwargs,
        )
        self.call_stage = "plan-strategies"

    async def extract_strategies(self, skeletons: list[dict]) -> dict[str, dict]:
        """Infer route/placement for every criterion in one batched LLM call.

        Returns a mapping from criterion id to its strategy dict.
        """
        listing = [
            {"id": item["id"], "section": item["section"], "criterion": item["criterion"]}
            for item in skeletons
        ]
        prompt = render_prompt(
            "PLANNER_STRATEGY_TASK_PROMPT",
            listing_json=json.dumps(listing, ensure_ascii=False, indent=2),
        )
        parsed, _ = await chat_until_valid_json(
            self,
            prompt,
            StrategyExtractionOutput,
            STRATEGY_EXPECTED_JSON,
        )
        return {item["id"]: item for item in parsed.get("strategies", [])}


def assemble_tasks(skeletons: list[dict], strategies: dict[str, dict]) -> list[dict]:
    """Merge skeletons and strategies into the runtime criteria list."""
    criteria_list: list[dict] = []
    for item in skeletons:
        strategy = strategies.get(item["id"]) or {}
        execution = ReviewExecutionPlan.model_validate(strategy.get("execution") or {})
        execution.review_instruction = execution.review_instruction.strip()
        criteria_list.append({
            "id": item["id"],
            "number": item["number"],
            "section": item["section"],
            "criterion": item["criterion"],
            "check_points": [item["criterion"]],
            "execution": execution.model_dump(mode="json"),
            "review_source": "uploaded",
            "policy_id": "",
        })
    return criteria_list
