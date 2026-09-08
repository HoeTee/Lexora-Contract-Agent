"""Load the fixed 18-item mandatory review surface from committed resources."""

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from routing.criteria import split_criteria_from_docx
from routing.schemas import RoutePlan


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANDATORY_CRITERIA_PATH = PROJECT_ROOT / "resources" / "criteria" / "criteria-formal.docx"
MANDATORY_PROFILE_PATH = PROJECT_ROOT / "resources" / "criteria" / "criteria-formal.routes.yaml"
MANDATORY_CRITERIA_COUNT = 18


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["summary", "inline", "inline_at_title", "tail", "body_end"]
    note: str = ""


class MandatoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^H\d{2}$")
    criterion_number: int = Field(ge=1, le=MANDATORY_CRITERIA_COUNT)
    check_points: list[str] = Field(min_length=1)
    review_instruction: str = ""
    route: RoutePlan
    placement: Placement


class MandatoryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    criteria_count: int
    policies: list[MandatoryPolicy]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_mandatory_criteria(path: str | Path) -> bool:
    candidate = Path(path).resolve()
    if not candidate.is_file() or not MANDATORY_CRITERIA_PATH.is_file():
        return False
    candidate_items = split_criteria_from_docx(candidate)
    mandatory_items = split_criteria_from_docx(MANDATORY_CRITERIA_PATH)
    comparable_fields = ("number", "section", "criterion")
    return [tuple(item[field] for field in comparable_fields) for item in candidate_items] == [
        tuple(item[field] for field in comparable_fields) for item in mandatory_items
    ]


def mandatory_source_manifest() -> dict[str, Any]:
    """Return stable resource identities for plan-cache invalidation and logs."""
    return {
        "criteria_filename": MANDATORY_CRITERIA_PATH.name,
        "criteria_sha256": _sha256(MANDATORY_CRITERIA_PATH),
        "routing_profile_filename": MANDATORY_PROFILE_PATH.name,
        "routing_profile_sha256": _sha256(MANDATORY_PROFILE_PATH),
    }


def load_mandatory_tasks() -> list[dict[str, Any]]:
    """Combine fixed route policy with the authoritative 18 DOCX criteria."""
    if not MANDATORY_CRITERIA_PATH.is_file():
        raise FileNotFoundError(f"mandatory criteria DOCX not found: {MANDATORY_CRITERIA_PATH}")
    if not MANDATORY_PROFILE_PATH.is_file():
        raise FileNotFoundError(f"mandatory route profile not found: {MANDATORY_PROFILE_PATH}")
    raw = yaml.safe_load(MANDATORY_PROFILE_PATH.read_text(encoding="utf-8"))
    profile = MandatoryProfile.model_validate(raw)
    criteria = split_criteria_from_docx(MANDATORY_CRITERIA_PATH)
    if len(criteria) != MANDATORY_CRITERIA_COUNT or profile.criteria_count != MANDATORY_CRITERIA_COUNT:
        raise ValueError("mandatory criteria DOCX and route profile must both declare exactly 18 criteria")
    by_number = {item.criterion_number: item for item in profile.policies}
    if len(profile.policies) != MANDATORY_CRITERIA_COUNT or sorted(by_number) != list(range(1, 19)):
        raise ValueError("mandatory route profile must contain criterion_number 1..18 exactly once")
    expected_ids = [f"H{number:02d}" for number in range(1, MANDATORY_CRITERIA_COUNT + 1)]
    if [by_number[number].id for number in range(1, 19)] != expected_ids:
        raise ValueError("mandatory route profile ids must map H01..H18 to criterion_number 1..18")
    tasks: list[dict[str, Any]] = []
    for skeleton in criteria:
        policy = by_number[skeleton["number"]]
        tasks.append({
            "id": policy.id,
            "number": skeleton["number"],
            "section": skeleton["section"],
            "criterion": skeleton["criterion"],
            "check_points": policy.check_points,
            "review_instruction": policy.review_instruction,
            "route": policy.route.model_dump(mode="json"),
            "placement": policy.placement.model_dump(mode="json"),
            "fallback_llm_route": False,
            "review_source": "mandatory",
            "policy_id": policy.id,
        })
    return tasks
