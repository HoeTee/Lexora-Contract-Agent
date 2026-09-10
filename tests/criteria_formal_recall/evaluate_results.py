from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import zipfile
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = Path(__file__).resolve().parent
DEFAULT_EXPECTED = SUITE_ROOT / "expected_cases_10.csv"
DEFAULT_TASK_ROOT = PROJECT_ROOT / "data" / "api"
DEFAULT_CASES_DIR = SUITE_ROOT / "cases"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CRITERION_NUMBER = re.compile(r"^\s*(\d{1,2})(?:\s*[.、．]\s*|\s+)")
RISK_PREFIX = re.compile(r"^风险等级[：:]\s*[高中低]\s*")


MANUAL_ERROR_RULES = (
    ("D02", 1, "乙方合同编号", "cross_criterion", "C01检查主体及联系信息，合同编号缺失不属于该要点。"),
    ("D04", 1, "乙方的合同编号", "cross_criterion", "C01检查主体及联系信息，合同编号缺失不属于该要点。"),
    ("D06", 1, "签订日期字段", "cross_criterion", "签订日期属于期限检查，不属于C01主体联系信息。"),
    ("D06", 1, "乙方实际合同编号", "cross_criterion", "C01检查主体及联系信息，合同编号缺失不属于该要点。"),
    ("D10", 2, "乙方地址及联系信息", "cross_criterion", "地址缺失属于C01，不属于C02合同标题框架检查。"),
    ("D10", 4, "建议检查正文或附件", "instruction_mismatch", "C04要求在合同首部补充指定提示语，该批注只建议检查采购参数。"),
    ("D03", 5, "首部正文起始段落", "wrong_anchor", "批注锚点是发票条款，不是其所称的合同首部正文起始段。"),
    ("D01", 6, "合计行小写", "cross_criterion", "金额大小写不一致属于C14，不是C06付款计划合计核验。"),
    ("D10", 6, "此处总金额8.8万元", "cross_criterion", "正文与表格总金额不一致属于C14/C11，不是C06付款计划合计核验。"),
    ("D10", 9, "甲方地址字段", "cross_criterion", "地址缺失属于C01，不能作为C09法院管辖问题的识别结果。"),
    ("D09", 10, "统一社会信用代码栏", "cross_criterion", "统一社会信用代码缺失不属于C10首部与签署栏主体名称一致性检查。"),
    ("D01", 12, "大写金额", "cross_criterion", "金额大小写不一致属于C14，不是错别字。"),
    ("D01", 12, "删除重复的", "cross_criterion", "重复词属于C15语病检查，不是C12错别字。"),
    ("D03", 12, "一词重复", "cross_criterion", "重复词属于C15语病检查，不是C12错别字。"),
    ("D05", 12, "语句逻辑断裂", "cross_criterion", "语句拼接和逻辑断裂不属于C12错别字检查。"),
    ("D05", 12, "文字重复且缺标点", "cross_criterion", "重复词和标点问题属于C15语病检查，不是C12错别字。"),
    ("D09", 12, "重复用词错误", "cross_criterion", "重复词属于C15语病检查，不是C12错别字。"),
    ("D01", 13, "本章节序号调整为“三”", "wrong_judgment", "“二、产品/服务说明及交付约定”原本位于第一章之后，编号二正确。"),
    ("D04", 14, "请核对合同总价及分项金额", "unsupported", "锚点中的1,850,000元与大写壹佰捌拾伍万元一致，没有金额大小写错误。"),
    ("D03", 15, "主语缺失", "wrong_judgment", "“乙方向甲方发贷”存在明确主语乙方，实际问题是C12错别字。"),
)


@dataclass(frozen=True)
class Candidate:
    criterion_id: int
    issue_id: str
    anchor_type: str
    anchor_id: str
    quoted_text: str
    comment_text: str


@dataclass(frozen=True)
class BackendIssue:
    criterion_id: int
    issue_id: str
    issue_comment: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并真实API运行并核对最终DOCX可见批注。")
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--expected-csv", type=Path, default=DEFAULT_EXPECTED)
    parser.add_argument("--task-root", type=Path, default=DEFAULT_TASK_ROOT)
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-duration-seconds", type=float, default=300.0)
    parser.add_argument("--min-recall", type=float, default=0.85)
    parser.add_argument("--min-accuracy", type=float, default=0.90)
    parser.add_argument("--fail-on-threshold", action="store_true")
    return parser.parse_args()


def normalized(value: str) -> str:
    value = RISK_PREFIX.sub("", value.strip())
    return re.sub(r"\s+", "", value).replace("'", "’")


def criterion_id(payload: dict[str, Any]) -> int | None:
    raw_id = str(payload.get("criterion_id") or "").strip()
    id_match = re.fullmatch(r"[CH](\d{1,2})", raw_id, flags=re.IGNORECASE)
    if id_match:
        value = int(id_match.group(1))
        return value if 1 <= value <= 18 else None
    match = CRITERION_NUMBER.match(str(payload.get("criterion") or ""))
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 18 else None


def load_candidates(
    task_root: Path, task_id: str
) -> tuple[list[Candidate], list[BackendIssue], set[int], dict[int, int]]:
    candidates: list[Candidate] = []
    backend_issues: list[BackendIssue] = []
    backend_reported: set[int] = set()
    backend_issue_counts: dict[int, int] = {}
    criteria_dir = task_root / task_id / "state" / "criteria"
    for path in sorted(criteria_dir.glob("*.output.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        number = criterion_id(payload)
        if number is None:
            continue
        issues = payload.get("issues")
        if not isinstance(issues, list) or not issues:
            continue
        backend_reported.add(number)
        backend_issue_counts[number] = backend_issue_counts.get(number, 0) + len(
            [issue for issue in issues if isinstance(issue, dict)]
        )
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            issue_comment = str(issue.get("issue_comment") or "").strip()
            backend_issues.append(
                BackendIssue(
                    criterion_id=number,
                    issue_id=str(issue.get("issue_id") or ""),
                    issue_comment=issue_comment,
                )
            )
            if issue_comment:
                candidates.append(
                    Candidate(
                        criterion_id=number,
                        issue_id=str(issue.get("issue_id") or ""),
                        anchor_type="",
                        anchor_id="",
                        quoted_text="",
                        comment_text=issue_comment,
                    )
                )
            for anchor in issue.get("anchors") or []:
                if not isinstance(anchor, dict):
                    continue
                text = str(anchor.get("comment_text") or "").strip()
                if not text:
                    continue
                candidates.append(
                    Candidate(
                        criterion_id=number,
                        issue_id=str(issue.get("issue_id") or ""),
                        anchor_type=str(anchor.get("xml_anchor_type") or ""),
                        anchor_id=str(anchor.get("xml_anchor_id") or ""),
                        quoted_text=str(anchor.get("quoted_text") or ""),
                        comment_text=text,
                    )
                )
    return candidates, backend_issues, backend_reported, backend_issue_counts


def json_values(values: list[str]) -> str:
    unique = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    return json.dumps(unique, ensure_ascii=False)


def consolidate_annotated_docs(
    records: dict[str, dict[str, Any]], output_dir: Path
) -> list[Path]:
    annotated_dir = output_dir / "annotated"
    annotated_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for contract_id_value in sorted(records):
        source = Path(str(records[contract_id_value]["output_docx"])).resolve()
        if not source.is_file():
            continue
        target = annotated_dir / f"{contract_id_value}_已审查.docx"
        shutil.copy2(source, target)
        outputs.append(target)
    return outputs


def docx_comments(docx_path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(docx_path, "r") as package:
        root = ElementTree.fromstring(package.read("word/comments.xml"))
    comments = []
    for node in root.findall(f"{{{W_NS}}}comment"):
        comments.append(
            {
                "comment_id": str(node.get(f"{{{W_NS}}}id") or ""),
                "author": str(node.get(f"{{{W_NS}}}author") or ""),
                "comment_text": "".join(node.itertext()).strip(),
            }
        )
    return comments


def visible_ai_comments(output_docx: Path, input_docx: Path) -> list[dict[str, str]]:
    original = Counter(
        (comment["author"], normalized(comment["comment_text"])) for comment in docx_comments(input_docx)
    )
    visible = []
    for comment in docx_comments(output_docx):
        fingerprint = (comment["author"], normalized(comment["comment_text"]))
        if original[fingerprint]:
            original[fingerprint] -= 1
            continue
        if comment["author"] == "AI 条款审查":
            visible.append(comment)
    return visible


def match_candidate(comment_text: str, candidates: list[Candidate]) -> tuple[Candidate | None, float]:
    target = normalized(comment_text)
    best: Candidate | None = None
    best_score = 0.0
    for candidate in candidates:
        source = normalized(candidate.comment_text)
        if target == source:
            score = 1.0
        elif target in source or source in target:
            score = 0.95
        else:
            score = SequenceMatcher(None, target, source).ratio()
        if score > best_score:
            best = candidate
            best_score = score
    return (best, best_score) if best_score >= 0.62 else (None, best_score)


def read_expected(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def load_records(run_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        payload = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
        for record in payload:
            records[str(record["contract_id"])] = record
    return records


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def manual_error(row: dict[str, Any]) -> tuple[str, str] | None:
    for contract, criterion, fragment, error_type, reason in MANUAL_ERROR_RULES:
        if (
            row["contract_id"] == contract
            and int(row["criterion_id"]) == criterion
            and fragment in row["comment_text"]
        ):
            return error_type, reason
    return None


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def write_metrics(
    output_dir: Path,
    expected: list[dict[str, str]],
    records: dict[str, dict[str, Any]],
    inventory: list[dict[str, Any]],
    incorrect_rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    max_duration_seconds: float,
    min_recall: float,
    min_accuracy: float,
) -> bool:
    expected_contract = Counter(row["contract_id"] for row in expected)
    failed_contract = Counter(row["contract_id"] for row in failures)
    comments_contract = Counter(row["contract_id"] for row in inventory)
    incorrect_contract = Counter(row["contract_id"] for row in incorrect_rows)
    contract_rows: list[dict[str, Any]] = []
    for contract in sorted(expected_contract):
        expected_count = expected_contract[contract]
        recalled = expected_count - failed_contract[contract]
        comments = comments_contract[contract]
        accurate = comments - incorrect_contract[contract]
        record = records.get(contract, {})
        duration = float(record.get("duration_seconds") or 0.0)
        contract_rows.append(
            {
                "contract_id": contract,
                "task_status": record.get("status", "missing"),
                "duration_seconds": f"{duration:.3f}",
                "within_time_limit": "yes" if 0 < duration < max_duration_seconds else "no",
                "expected_cases": expected_count,
                "recalled_cases": recalled,
                "recall": f"{ratio(recalled, expected_count):.4%}",
                "visible_comments": comments,
                "accurate_comments": accurate,
                "accuracy": f"{ratio(accurate, comments):.4%}",
            }
        )
    write_csv(
        output_dir / "metrics_by_contract.csv",
        (
            "contract_id",
            "task_status",
            "duration_seconds",
            "within_time_limit",
            "expected_cases",
            "recalled_cases",
            "recall",
            "visible_comments",
            "accurate_comments",
            "accuracy",
        ),
        contract_rows,
    )

    expected_criterion = Counter(int(row["criterion_id"]) for row in expected)
    failed_criterion = Counter(int(row["criterion_id"]) for row in failures)
    comments_criterion = Counter(
        int(row["criterion_id"]) for row in inventory if str(row["criterion_id"]).isdigit()
    )
    incorrect_criterion = Counter(
        int(row["criterion_id"])
        for row in incorrect_rows
        if str(row["criterion_id"]).isdigit()
    )
    criterion_rows: list[dict[str, Any]] = []
    for criterion in sorted(expected_criterion):
        expected_count = expected_criterion[criterion]
        recalled = expected_count - failed_criterion[criterion]
        comments = comments_criterion[criterion]
        accurate = comments - incorrect_criterion[criterion]
        criterion_rows.append(
            {
                "criterion_id": criterion,
                "expected_cases": expected_count,
                "recalled_cases": recalled,
                "recall": f"{ratio(recalled, expected_count):.4%}",
                "visible_comments": comments,
                "accurate_comments": accurate,
                "accuracy": f"{ratio(accurate, comments):.4%}",
            }
        )
    write_csv(
        output_dir / "metrics_by_criterion.csv",
        (
            "criterion_id",
            "expected_cases",
            "recalled_cases",
            "recall",
            "visible_comments",
            "accurate_comments",
            "accuracy",
        ),
        criterion_rows,
    )

    total_expected = len(expected)
    total_recalled = total_expected - len(failures)
    total_comments = len(inventory)
    total_accurate = total_comments - len(incorrect_rows)
    overall_recall = ratio(total_recalled, total_expected)
    overall_accuracy = ratio(total_accurate, total_comments)
    time_pass = bool(contract_rows) and all(
        row["task_status"] == "succeeded" and row["within_time_limit"] == "yes"
        for row in contract_rows
    )
    threshold_pass = time_pass and overall_recall >= min_recall and overall_accuracy >= min_accuracy
    summary_rows = [
        {
            "contracts": len(contract_rows),
            "max_duration_seconds": max(
                (float(row["duration_seconds"]) for row in contract_rows), default=0.0
            ),
            "all_within_time_limit": "yes" if time_pass else "no",
            "expected_cases": total_expected,
            "recalled_cases": total_recalled,
            "recall": f"{overall_recall:.4%}",
            "visible_comments": total_comments,
            "accurate_comments": total_accurate,
            "accuracy": f"{overall_accuracy:.4%}",
            "threshold_pass": "yes" if threshold_pass else "no",
        }
    ]
    write_csv(
        output_dir / "metrics_summary.csv",
        tuple(summary_rows[0]),
        summary_rows,
    )
    report_lines = [
        "# criteria-formal 评测结果",
        "",
        f"阈值结论：{'通过' if threshold_pass else '未通过'}。",
        "",
        "| 合同 | 状态 | 端到端秒数 | <5分钟 | 召回率 | 准确率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    report_lines.extend(
        f"| {row['contract_id']} | {row['task_status']} | {row['duration_seconds']} | "
        f"{row['within_time_limit']} | {row['recall']} | {row['accuracy']} |"
        for row in contract_rows
    )
    report_lines.extend(
        [
            "",
            "| 审查要点 | 召回数/期望数 | 召回率 | 准确批注/可见批注 | 准确率 |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    report_lines.extend(
        f"| {row['criterion_id']} | {row['recalled_cases']}/{row['expected_cases']} | "
        f"{row['recall']} | {row['accurate_comments']}/{row['visible_comments']} | {row['accuracy']} |"
        for row in criterion_rows
    )
    report_lines.extend(
        [
            "",
            f"总体召回率：{total_recalled}/{total_expected} = {overall_recall:.4%}。",
            f"总体准确率：{total_accurate}/{total_comments} = {overall_accuracy:.4%}。",
            "准确率口径：可见 AI 批注能够映射到后端审查要点，且未命中人工核定错误规则；无法映射的批注计为错误。",
        ]
    )
    (output_dir / "metrics_report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return threshold_pass


def main() -> int:
    args = parse_args()
    run_dirs = [path.resolve() for path in args.run_dir]
    task_root = args.task_root.resolve()
    cases_dir = args.cases_dir.resolve()
    output_dir = args.output_dir.resolve()
    expected = read_expected(args.expected_csv.resolve())
    records = load_records(run_dirs)
    inventory: list[dict[str, Any]] = []
    visible_by_contract: dict[str, set[int]] = {}
    backend_by_contract: dict[str, set[int]] = {}
    backend_candidates_by_contract: dict[str, list[Candidate]] = {}
    backend_issues_by_contract: dict[str, list[BackendIssue]] = {}
    backend_issue_counts_by_contract: dict[str, dict[int, int]] = {}

    for contract_id_value in sorted(records):
        record = records[contract_id_value]
        if record.get("status") != "succeeded" or not record.get("output_docx"):
            visible_by_contract[contract_id_value] = set()
            backend_by_contract[contract_id_value] = set()
            backend_candidates_by_contract[contract_id_value] = []
            backend_issues_by_contract[contract_id_value] = []
            backend_issue_counts_by_contract[contract_id_value] = {}
            continue
        task_id_value = str(record["task_id"])
        candidates, backend_issues, backend_reported, backend_issue_counts = (
            load_candidates(task_root, task_id_value)
        )
        backend_by_contract[contract_id_value] = backend_reported
        backend_candidates_by_contract[contract_id_value] = candidates
        backend_issues_by_contract[contract_id_value] = backend_issues
        backend_issue_counts_by_contract[contract_id_value] = backend_issue_counts
        docx_path = Path(str(record["output_docx"])).resolve()
        visible_criteria: set[int] = set()
        input_docx = cases_dir / f"{contract_id_value}.docx"
        for ordinal, comment in enumerate(visible_ai_comments(docx_path, input_docx), start=1):
            candidate, score = match_candidate(comment["comment_text"], candidates)
            if candidate is not None:
                visible_criteria.add(candidate.criterion_id)
            inventory.append(
                {
                    "contract_id": contract_id_value,
                    "task_id": task_id_value,
                    "ai_comment_number": ordinal,
                    "comment_xml_id": comment["comment_id"],
                    "criterion_id": candidate.criterion_id if candidate else "",
                    "issue_id": candidate.issue_id if candidate else "",
                    "anchor_id": candidate.anchor_id if candidate else "",
                    "quoted_text": candidate.quoted_text if candidate else "",
                    "comment_text": comment["comment_text"],
                    "match_score": f"{score:.4f}",
                    "output_docx": str(docx_path),
                }
            )
        visible_by_contract[contract_id_value] = visible_criteria

    incorrect_rows: list[dict[str, Any]] = []
    accurate_visible_by_contract: dict[str, set[int]] = {}
    for row in inventory:
        if not row["criterion_id"]:
            incorrect_rows.append(
                {
                    **row,
                    "error_type": "unmapped_comment",
                    "error_reason": "可见批注无法映射到对应的后端审查要点。",
                }
            )
            continue
        error = manual_error(row)
        if error is None:
            accurate_visible_by_contract.setdefault(row["contract_id"], set()).add(int(row["criterion_id"]))
            continue
        error_type, reason = error
        incorrect_rows.append({**row, "error_type": error_type, "error_reason": reason})

    failures = []
    for row in expected:
        contract = row["contract_id"]
        number = int(row["criterion_id"])
        if number in accurate_visible_by_contract.get(contract, set()):
            continue
        backend_reported = number in backend_by_contract.get(contract, set())
        visibly_reported = number in visible_by_contract.get(contract, set())
        backend_candidates = [
            candidate
            for candidate in backend_candidates_by_contract.get(contract, [])
            if candidate.criterion_id == number
        ]
        backend_issues = [
            issue
            for issue in backend_issues_by_contract.get(contract, [])
            if issue.criterion_id == number
        ]
        visible_incorrect = [
            comment
            for comment in incorrect_rows
            if comment["contract_id"] == contract
            and str(comment["criterion_id"]).isdigit()
            and int(comment["criterion_id"]) == number
        ]
        failures.append(
            {
                "case_id": row["case_id"],
                "contract_id": contract,
                "criterion_id": number,
                "expected_problem": row["expected_problem"],
                "expected_location": row["expected_location"],
                "expected_quote": row["expected_quote"],
                "backend_reported": "yes" if backend_reported else "no",
                "backend_issue_count": backend_issue_counts_by_contract.get(contract, {}).get(
                    number, 0
                ),
                "backend_issue_comments": json_values(
                    [issue.issue_comment for issue in backend_issues]
                ),
                "backend_quoted_texts": json_values(
                    [candidate.quoted_text for candidate in backend_candidates]
                ),
                "backend_anchor_comments": json_values(
                    [candidate.comment_text for candidate in backend_candidates]
                ),
                "visible_quoted_texts": json_values(
                    [str(comment["quoted_text"]) for comment in visible_incorrect]
                ),
                "visible_comments": json_values(
                    [str(comment["comment_text"]) for comment in visible_incorrect]
                ),
                "error_reasons": json_values(
                    [str(comment["error_reason"]) for comment in visible_incorrect]
                ),
                "backend_evidence_status": (
                    "not_reported_no_recalled_text"
                    if not backend_reported
                    else "reported_with_anchor_text"
                    if backend_candidates
                    else "reported_without_anchor_no_recalled_text"
                ),
                "failure_stage": (
                    "incorrect_only" if visibly_reported else "docx_write" if backend_reported else "review"
                ),
            }
        )

    write_csv(
        output_dir / "comment_inventory.csv",
        (
            "contract_id",
            "task_id",
            "ai_comment_number",
            "comment_xml_id",
            "criterion_id",
            "issue_id",
            "anchor_id",
            "quoted_text",
            "comment_text",
            "match_score",
            "output_docx",
        ),
        inventory,
    )
    write_csv(
        output_dir / "recall_failures.csv",
        (
            "case_id",
            "contract_id",
            "criterion_id",
            "expected_problem",
            "expected_location",
            "expected_quote",
            "backend_reported",
            "backend_issue_count",
            "backend_issue_comments",
            "backend_quoted_texts",
            "backend_anchor_comments",
            "visible_quoted_texts",
            "visible_comments",
            "error_reasons",
            "backend_evidence_status",
            "failure_stage",
        ),
        failures,
    )
    write_csv(
        output_dir / "incorrect_comments.csv",
        (
            "contract_id",
            "task_id",
            "ai_comment_number",
            "comment_xml_id",
            "criterion_id",
            "issue_id",
            "anchor_id",
            "quoted_text",
            "comment_text",
            "match_score",
            "error_type",
            "error_reason",
            "output_docx",
        ),
        incorrect_rows,
    )
    consolidated_docs = consolidate_annotated_docs(records, output_dir)
    threshold_pass = write_metrics(
        output_dir,
        expected,
        records,
        inventory,
        incorrect_rows,
        failures,
        max_duration_seconds=args.max_duration_seconds,
        min_recall=args.min_recall,
        min_accuracy=args.min_accuracy,
    )
    total = len(expected)
    recalled = total - len(failures)
    accurate_comments = len(inventory) - len(incorrect_rows)
    print(f"Contracts: {len(records)}")
    print(f"Visible AI comments: {len(inventory)}")
    print(f"Accurate visible criterion recall: {recalled}/{total} = {ratio(recalled, total):.2%}")
    print(f"Recall failures: {len(failures)}")
    print(
        f"Accurate comments: {accurate_comments}/{len(inventory)} = "
        f"{ratio(accurate_comments, len(inventory)):.2%}"
    )
    print(f"Thresholds: {'PASS' if threshold_pass else 'FAIL'}")
    print(f"Consolidated annotated DOCX files: {len(consolidated_docs)}")
    print(f"Output: {output_dir}")
    return 1 if args.fail_on_threshold and not threshold_pass else 0


if __name__ == "__main__":
    raise SystemExit(main())
