"""Prepare frozen baseline evidence and fail closed until it is adjudicated.

No model calls. A complete review is a measured baseline, not a claim that all
its comments are correct. Semantic decisions still require evidence review.
"""

import argparse
import csv
from pathlib import Path
import subprocess
from xml.etree import ElementTree as ET
import zipfile

from tests.docxindex_ablation.judge import new_ai_comments
from tests.docxindex_ablation.run import ROOT, SUITE, mandatory_criteria, read_json, sha, write_csv, write_json


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def document_items(path):
    with zipfile.ZipFile(path) as package:
        body = ET.fromstring(package.read("word/document.xml")).find(W + "body")
    return [
        {"body_child_index": i, "text": "".join(t.text or "" for t in node.iter(W + "t"))}
        for i, node in enumerate(body)
    ]


def snapshot(output):
    manifest = read_json(output / "manifest.json")
    code_paths = ["tools", "agents", "workflow", "mcp_service", "endpoints", "config.py", "loggers"]
    result = subprocess.run(["git", "diff", "--quiet", manifest["git"], "--", *code_paths], cwd=ROOT, check=False)
    if result.returncode:
        raise ValueError("Production code differs from captured baseline; do not reuse its quality certificate")
    paths = {
        ROOT / "config.yaml": manifest["config_sha256"],
        ROOT / "resources/criteria/criteria-routes.yaml": manifest["routes_sha256"],
        SUITE / "expected_cases_10.csv": manifest["expected_sha256"],
    }
    with (SUITE / "expected_cases_10.csv").open(encoding="utf-8-sig", newline="") as handle:
        expected = list(csv.DictReader(handle))
    expected_ids = {f"D{d:02}-C{c:02}" for d in range(1, 11) for c in range(1, 19)}
    if len(expected) != 180 or {r["case_id"] for r in expected} != expected_ids:
        raise ValueError(
            "Expected cases must contain the original 180 unique cases; do not silently drop disputed cases"
        )
    contracts = {}
    for contract in sorted(manifest["contracts"]):
        doc = SUITE / "cases" / f"{contract}.docx"
        paths[doc] = manifest["contracts"][contract]["sha256"]
        task_path = output / "formal_verified/baseline" / f"{contract}.json"
        task = read_json(task_path)
        if (
            task["status"] != "succeeded"
            or not task.get("experiment_valid")
            or task["experiment_variant"] != "baseline"
        ):
            raise ValueError(f"Not a valid completed baseline: {task_path}")
        annotated = Path(task["output_docx"])
        if not annotated.is_absolute():
            annotated = ROOT / annotated
        paths[task_path.resolve()] = sha(task_path)
        paths[annotated] = sha(annotated)
        contracts[contract] = {
            "task_id": task["task_id"],
            "docx": str(annotated),
            "items": document_items(doc),
            "comments": new_ai_comments(annotated, doc),
        }
    for path, digest in paths.items():
        if sha(path) != digest:
            raise ValueError(f"Changed baseline input: {path}")
    return {
        "files": {str(p): digest for p, digest in paths.items()},
        "expected": expected,
        "criteria": {
            str(key): value
            for key, value in mandatory_criteria().items()
        },
        "contracts": contracts,
    }


def reservation_conflict(criterion, expected, items):
    """Flag a wording conflict, not an automatic legal/quality verdict."""
    if "预留" not in criterion or "未填写" not in expected:
        return []
    return [
        r
        for r in items
        if all(word in r["text"] for word in ("项目负责人", "姓名", "身份证号码", "电话号码", "【未填写】"))
    ]


def prepare(output):
    directory = output / "baseline_quality"
    if directory.exists():
        raise ValueError("Baseline review already exists; check it, do not overwrite adjudication")
    frozen = snapshot(output)
    cases, comments = [], []
    for row in frozen["expected"]:
        contract = row["contract_id"]
        criterion = frozen["criteria"][row["criterion_id"]]
        conflict = reservation_conflict(criterion, row["expected_problem"], frozen["contracts"][contract]["items"])
        cases.append(
            {
                "case_id": row["case_id"],
                "expected_status": "disputed" if conflict else "pending",
                "acceptance_condition": "",
                "baseline_verdict": "pending",
                "comment_ids": [],
                "evidence_excerpt": "",
                "location_correct": None,
                "reviewer": "",
                "reason": "",
                "wording_conflict_evidence": conflict,
            }
        )
    for contract, data in frozen["contracts"].items():
        for comment in data["comments"]:
            comments.append(
                {
                    "contract_id": contract,
                    "comment_id": comment["comment_id"],
                    "verdict": "pending",
                    "criterion_ids": [],
                    "location_correct": None,
                    "reviewer": "",
                    "reason": "",
                }
            )
    write_json(directory / "snapshot.json", frozen)
    write_json(directory / "review.json", {"cases": cases, "comments": comments})
    return check(output)


def evaluate(frozen, review):
    reasons = []
    expected = {r["case_id"]: r for r in frozen["expected"]}
    visible = {(cid, r["comment_id"]): r for cid, data in frozen["contracts"].items() for r in data["comments"]}
    cases, comments = review.get("cases", []), review.get("comments", [])
    if len(cases) != len(expected) or {r["case_id"] for r in cases} != set(expected):
        reasons.append("Case coverage differs from frozen baseline")
    if len(comments) != len(visible) or {(r["contract_id"], r["comment_id"]) for r in comments} != set(visible):
        reasons.append("Visible-comment coverage differs from frozen baseline")
    for row in cases:
        cid = row["case_id"]
        if row["expected_status"] != "valid":
            reasons.append(f"{cid}: expected case is {row['expected_status']}; no automatic denominator removal")
        if not all(str(row.get(k) or "").strip() for k in ("reviewer", "reason", "acceptance_condition")):
            reasons.append(f"{cid}: missing accountable adjudication or acceptance condition")
        if row["baseline_verdict"] not in {"hit", "partial", "miss"}:
            reasons.append(f"{cid}: baseline outcome unreviewed")
        if row["baseline_verdict"] in {"hit", "partial"}:
            contract = expected.get(cid, {}).get("contract_id")
            keys = [(contract, key) for key in row["comment_ids"]]
            if not keys or any(key not in visible for key in keys):
                reasons.append(f"{cid}: missing or invented visible-comment reference")
            elif not row["evidence_excerpt"] or not any(row["evidence_excerpt"] in visible[k]["text"] for k in keys):
                reasons.append(f"{cid}: evidence is not a verbatim excerpt from a referenced comment")
            if row.get("location_correct") is not True:
                reasons.append(f"{cid}: evidence location is not verified")
    for row in comments:
        key = row["contract_id"], row["comment_id"]
        allowed = {"correct", "incorrect"}
        if visible.get(key, {}).get("author") == "AI 审查总结":
            allowed.add("mixed")
        if row["verdict"] not in allowed or not row["reviewer"].strip() or not row["reason"].strip():
            reasons.append(f"{key}: comment quality unreviewed")
        if not row["criterion_ids"] or any(type(n) is not int or n not in range(1, 19) for n in row["criterion_ids"]):
            reasons.append(f"{key}: missing valid criterion attribution")
        if type(row.get("location_correct")) is not bool or (
            row["verdict"] == "correct" and not row["location_correct"]
        ):
            reasons.append(f"{key}: location unreviewed or inconsistent with correct verdict")
    ordinary = [
        r for r in comments if visible.get((r["contract_id"], r["comment_id"]), {}).get("author") == "AI 条款审查"
    ]
    ready = not reasons
    return {
        "baseline_ready": ready,
        "blocking_reasons": reasons,
        "case_count": len(expected),
        "visible_comment_count": len(visible),
        "disputed_cases": [r["case_id"] for r in cases if r["expected_status"] == "disputed"],
        "recall": sum(r["baseline_verdict"] == "hit" for r in cases) / len(expected) if ready else None,
        "precision": sum(r["verdict"] == "correct" for r in ordinary) / len(ordinary) if ready and ordinary else None,
        "limitation": "Checks coverage, provenance and citations, not the truth of semantic judgements",
    }


def check(output):
    directory = output / "baseline_quality"
    frozen = read_json(directory / "snapshot.json")
    if frozen != snapshot(output):
        raise ValueError("Frozen evidence changed; baseline review cannot be reused")
    review = read_json(directory / "review.json")
    result = evaluate(frozen, review)
    write_json(directory / "gate.json", result)
    expected = {r["case_id"]: r for r in frozen["expected"]}
    rows = [
        {
            **expected[r["case_id"]],
            **r,
            "actual_conflict_text": "\n".join(
                x["text"]
                for x in reservation_conflict(
                    frozen["criteria"][expected[r["case_id"]]["criterion_id"]],
                    expected[r["case_id"]]["expected_problem"],
                    frozen["contracts"][expected[r["case_id"]]["contract_id"]]["items"],
                )
            ),
        }
        for r in review["cases"]
    ]
    write_csv(
        directory / "cases.csv",
        rows,
        [
            "case_id",
            "expected_problem",
            "expected_status",
            "actual_conflict_text",
            "acceptance_condition",
            "baseline_verdict",
            "comment_ids",
            "evidence_excerpt",
            "location_correct",
            "reviewer",
            "reason",
        ],
    )
    write_csv(
        directory / "conflicts.csv",
        [r for r in rows if r["expected_status"] == "disputed"],
        ["case_id", "expected_problem", "actual_conflict_text"],
    )
    visible = {(cid, r["comment_id"]): r for cid, data in frozen["contracts"].items() for r in data["comments"]}
    rows = [{**r, **visible[(r["contract_id"], r["comment_id"])]} for r in review["comments"]]
    write_csv(
        directory / "comments.csv",
        rows,
        [
            "contract_id",
            "comment_id",
            "author",
            "anchored_text",
            "text",
            "verdict",
            "criterion_ids",
            "location_correct",
            "reviewer",
            "reason",
        ],
    )
    return result


def require_baseline_quality(output):
    if not (output / "baseline_quality/review.json").exists():
        raise ValueError("Baseline quality is unreviewed: run quality prepare/check before any new ablation")
    result = check(output)
    if not result["baseline_ready"]:
        raise ValueError(
            f"Baseline quality gate BLOCKED: {len(result['blocking_reasons'])} unresolved checks; see baseline_quality/gate.json"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "check"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.output) if args.command == "prepare" else check(args.output)
    print({k: v for k, v in result.items() if k != "blocking_reasons"})
    raise SystemExit(0 if result["baseline_ready"] else 2)


if __name__ == "__main__":
    main()
