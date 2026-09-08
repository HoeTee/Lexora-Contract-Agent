"""Track observed findings across existing reflection calls, without another LLM."""

from copy import deepcopy
import hashlib
import json


def finding_entries(opinion: dict) -> dict[str, dict]:
    """Index edit locations; wording changes must not change a field's identity."""
    entries = {}
    for issue in opinion.get("issues", []):
        edits = [a for a in issue.get("anchors", []) if a.get("role", "edit") == "edit"]
        for anchor in edits or [None]:
            identity = (
                [anchor.get("xml_anchor_type"), anchor.get("xml_anchor_id"),
                 "".join(anchor.get("quoted_text", "").split()), anchor.get("defect_type", "other")]
                if anchor else [issue.get("check_point"), issue.get("issue_comment")]
            )
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
            local_issue = deepcopy(issue)
            if anchor:
                local_issue["anchors"] = [deepcopy(anchor)]
            entries[key] = {"finding_id": key, "issue": local_issue, "anchor": deepcopy(anchor)}
    return entries


class FindingLedger:
    def __init__(self, opinion: dict):
        self.observed: dict[str, dict] = {}
        self.removals: dict[str, str] = {}
        self.history: list[dict] = []
        self.observe(opinion)

    def observe(self, opinion: dict) -> None:
        for key, entry in finding_entries(opinion).items():
            entry["finding_id"] = (self.observed[key]["finding_id"] if key in self.observed
                                   else f"F{len(self.observed) + 1:03d}")
            self.observed[key] = entry

    def current(self, opinion: dict) -> list[dict]:
        """A compact location index, not a second copy of SubAgent's output."""
        return [{"finding_id": self.observed[key]["finding_id"],
                 "issue_id": entry["issue"]["issue_id"],
                 "location": entry["anchor"], "description": entry["issue"]["issue_comment"]}
                for key, entry in finding_entries(opinion).items()]

    def missing(self, opinion: dict) -> list[dict]:
        current = finding_entries(opinion)
        return [entry for key, entry in self.observed.items()
                if key not in current and entry["finding_id"] not in self.removals]

    def review(self, opinion: dict, review: dict, round_number: int) -> bool:
        unknown = []
        known_ids = {entry["finding_id"] for entry in self.observed.values()}
        current_ids = {entry["finding_id"] for entry in self.current(opinion)}
        # A revised, currently present finding must be evaluated anew. A past
        # withdrawal is not a permanent veto on the location itself.
        for key in current_ids:
            self.removals.pop(key, None)
        for item in review.get("removals", []):
            key = item["finding_id"]
            if key in known_ids and item["reason"].strip():
                if key not in current_ids:
                    self.removals[key] = item["reason"]
            else:
                unknown.append(key)
        self.history.append({"round": round_number, "opinion": deepcopy(opinion),
                             "review": deepcopy(review), "unknown_removal_ids": unknown})
        # A removal decision is not applied by deleting arbitrary model text.
        # The reviewer must actually retract the finding on its next revision.
        retained_retractions = current_ids & {item["finding_id"] for item in review.get("removals", [])}
        return (review["status"] == "PASS" and not self.missing(opinion)
                and not unknown and not retained_retractions)

    def unresolved_issues(self, opinion: dict) -> list[dict]:
        issues = []
        for entry in self.missing(opinion):
            issue = deepcopy(entry["issue"])
            if entry["anchor"]:
                issue["anchors"] = [deepcopy(entry["anchor"])]
                issue["issue_comment"] = "先前发现尚无有效撤回说明：" + entry["anchor"].get("comment_text", "")
            issue["issue_id"] = "unresolved_" + entry["finding_id"]
            issues.append(issue)
        return issues
