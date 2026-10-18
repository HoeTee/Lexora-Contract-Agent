"""Verify fixed and uploaded reviews share a criterion-independent execution plan."""

import unittest

import yaml
from pydantic import ValidationError

from agents.planner import assemble_tasks
from agents.schemas import SubAgentOutput
from routing.profile import MANDATORY_PROFILE_PATH, load_mandatory_tasks
from routing.schemas import ReviewExecutionPlan, RoutePlan


class ReviewExecutionTests(unittest.TestCase):
    def test_profile_keeps_configured_checks_without_flat_runtime_copies(self):
        policies = yaml.safe_load(MANDATORY_PROFILE_PATH.read_text(encoding="utf-8"))["policies"]
        tasks = load_mandatory_tasks()
        legacy_fields = {
            "route", "placement", "review_instruction", "coverage_audit",
            "comparison_audit", "edit_region", "evidence_region", "fallback_llm_route",
        }

        for policy, task in zip(policies, tasks, strict=True):
            with self.subTest(criterion=task["id"]):
                execution = task["execution"]
                self.assertEqual(task["check_points"], policy["check_points"])
                self.assertFalse(legacy_fields.intersection(task))
                self.assertEqual(execution["route"], RoutePlan.model_validate(policy["route"]).model_dump(mode="json"))
                self.assertEqual(execution["placement"], policy["placement"])
                self.assertEqual(execution["audit_mode"], {
                    "coverage": policy.get("coverage_audit", False),
                    "comparison": policy.get("comparison_audit", False),
                })
                self.assertEqual(execution["edit_region"], policy.get("edit_region", ""))
                self.assertEqual(execution["evidence_region"], policy.get("evidence_region", ""))

    def test_same_scan_route_does_not_select_an_audit_mode(self):
        route = {"steps": [{"method": "scan"}]}
        ordinary = ReviewExecutionPlan(route=route)
        compare = ReviewExecutionPlan(
            route=route, audit_mode={"comparison": True}, evidence_region="attachments",
        )
        coverage = ReviewExecutionPlan(route=route, audit_mode={"coverage": True})

        self.assertEqual(ordinary.route, compare.route)
        self.assertEqual(compare.route, coverage.route)
        self.assertEqual(ordinary.audit_mode.model_dump(), {"coverage": False, "comparison": False})
        self.assertEqual(compare.audit_mode.model_dump(), {"coverage": False, "comparison": True})
        self.assertEqual(coverage.audit_mode.model_dump(), {"coverage": True, "comparison": False})

    def test_comparison_audit_requires_a_counterpart_scope(self):
        with self.assertRaisesRegex(ValidationError, "explicit evidence_region"):
            ReviewExecutionPlan(audit_mode={"comparison": True})

        plan = ReviewExecutionPlan(audit_mode={"comparison": True}, evidence_region="attachments")
        self.assertEqual(plan.edit_region, "")
        self.assertFalse(ReviewExecutionPlan(evidence_region="attachments").audit_mode.comparison)

    def test_uploaded_tasks_keep_execution_independent_of_identifier(self):
        execution = ReviewExecutionPlan(
            route={"steps": [{"method": "region", "regions": ["tail"], "coverage": "complete"}]},
            placement={"target": "inline"},
            audit_mode={"coverage": True},
            edit_region="tail",
        ).model_dump(mode="json")
        skeletons = [
            {"id": identifier, "number": number, "section": "签署", "criterion": "检查签署信息"}
            for number, identifier in enumerate(("U01", "CUSTOM-anything", "H11"), 1)
        ]
        strategies = {item["id"]: {"id": item["id"], "execution": execution} for item in skeletons}

        tasks = assemble_tasks(skeletons, strategies)

        self.assertEqual([task["execution"] for task in tasks], [execution] * len(tasks))
        self.assertEqual([task["id"] for task in tasks], [item["id"] for item in skeletons])
        self.assertTrue(all(task["review_source"] == "uploaded" for task in tasks))
        self.assertTrue(all("route" not in task and "fallback_llm_route" not in task for task in tasks))

    def test_fixed_and_uploaded_tasks_have_the_same_execution_contract(self):
        fixed = load_mandatory_tasks()[0]
        uploaded = assemble_tasks(
            [{"id": "U-arbitrary", "number": 1, "section": "上传", "criterion": "另一条业务要求"}],
            {"U-arbitrary": {"id": "U-arbitrary", "execution": fixed["execution"]}},
        )[0]

        self.assertEqual(uploaded["execution"], fixed["execution"])
        self.assertEqual(
            ReviewExecutionPlan.model_validate(uploaded["execution"]),
            ReviewExecutionPlan.model_validate(fixed["execution"]),
        )

    def test_missing_uploaded_strategy_uses_route_absence_for_fallback(self):
        task = assemble_tasks(
            [{"id": "U01", "number": 1, "section": "上传", "criterion": "检查合同"}],
            {},
        )[0]

        self.assertIsNone(task["execution"]["route"])
        self.assertIsNone(task["execution"]["placement"])
        self.assertEqual(task["execution"]["audit_mode"], {"coverage": False, "comparison": False})
        self.assertNotIn("fallback_llm_route", task)


class ComparisonPairUniquenessTests(unittest.TestCase):
    def test_one_pair_cannot_report_conflicting_comparison_results(self):
        comparisons = [{
            "edit_anchor_type": "paragraph", "edit_anchor_id": "path:body/p1",
            "evidence_anchor_type": "paragraph", "evidence_anchor_id": "path:body/p2",
            "decision": outcome, "reason": "Compare the same business value",
        } for outcome in ("matched", "mismatch")]
        with self.assertRaisesRegex(ValidationError, "must not repeat the same edit/evidence pair"):
            SubAgentOutput.model_validate({"status": "compliant", "issues": [],
                                          "comparison_decisions": comparisons})


if __name__ == "__main__":
    unittest.main()
