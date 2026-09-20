"""Load the fixed 18-item mandatory review surface from its YAML profile."""

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from routing.schemas import RoutePlan


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANDATORY_PROFILE_PATH = PROJECT_ROOT / "resources" / "criteria" / "criteria-routes.yaml"
MANDATORY_CRITERIA_COUNT = 18


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["summary", "inline", "inline_at_title", "tail", "body_end"]
    note: str = ""


class MandatoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^H\d{2}$")
    section: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    check_points: list[str] = Field(min_length=1)
    review_instruction: str = ""
    route: RoutePlan
    placement: Placement
    coverage_audit: bool = False
    comparison_audit: bool = False
    edit_region: str = ""
    evidence_region: str = ""
    dedupe_priority: int = Field(default=0, ge=0)
    dedupe_domain: str = ""


class MandatoryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    criteria_count: int
    policies: list[MandatoryPolicy]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mandatory_source_manifest() -> dict[str, Any]:
    """Return stable resource identities for plan-cache invalidation and logs."""
    return {
        "routing_profile_filename": MANDATORY_PROFILE_PATH.name,
        "routing_profile_sha256": _sha256(MANDATORY_PROFILE_PATH),
    }


def load_mandatory_tasks() -> list[dict[str, Any]]:
    """Build the 18 mandatory tasks from the self-contained YAML profile."""
    if not MANDATORY_PROFILE_PATH.is_file():
        raise FileNotFoundError(f"mandatory route profile not found: {MANDATORY_PROFILE_PATH}")
    raw = yaml.safe_load(MANDATORY_PROFILE_PATH.read_text(encoding="utf-8"))
    profile = MandatoryProfile.model_validate(raw)
    if len(profile.policies) != MANDATORY_CRITERIA_COUNT or profile.criteria_count != MANDATORY_CRITERIA_COUNT:
        raise ValueError("mandatory route profile must declare exactly 18 criteria")
    expected_ids = [f"H{number:02d}" for number in range(1, MANDATORY_CRITERIA_COUNT + 1)]
    if [policy.id for policy in profile.policies] != expected_ids:
        raise ValueError("mandatory route profile policies must be ordered H01..H18")
    tasks: list[dict[str, Any]] = []
    for policy in profile.policies:
        tasks.append({
            "id": policy.id,
            "section": policy.section,
            "criterion": policy.criterion,
            "check_points": policy.check_points,
            "review_instruction": policy.review_instruction,
            "route": policy.route.model_dump(mode="json"),
            "placement": policy.placement.model_dump(mode="json"),
            "fallback_llm_route": False,
            "review_source": "mandatory",
            "policy_id": policy.id,
            "coverage_audit": policy.coverage_audit,
            "comparison_audit": policy.comparison_audit,
            "edit_region": policy.edit_region,
            "evidence_region": policy.evidence_region,
            "dedupe_priority": policy.dedupe_priority,
            "dedupe_domain": policy.dedupe_domain,
        })
    return tasks
