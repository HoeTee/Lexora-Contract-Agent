"""Summarize persisted experiments without calling a model or guessing quality."""

import argparse
import csv
import json
from pathlib import Path

from tests.docxindex_ablation.run import ROOT, read_json, write_csv, write_json, anchor_texts


def csv_rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def report(output):
    manifest = read_json(output / "manifest.json")
    contracts = []
    singleton_calls = vector_covered = 0
    covered_rerank_sizes = []
    singleton_seconds = vector_seconds = 0.0
    for directory in sorted(output.glob("D[0-9][0-9]")):
        if not (directory / "complete.json").exists():
            continue
        queries = [read_json(f) for f in sorted(directory.glob("C[0-9][0-9].json"))]
        models = [r for query in queries for r in query["models"]]
        models += read_json(directory / "build_models.json")
        usage = [r["provider_usage"] for r in models if r.get("provider_usage")]
        singleton = [
            r for q in queries for r in q["calls"] if r["function"] == "rerank_matches" and len(r["candidates"]) == 1
        ]
        singleton_calls += len(singleton)
        singleton_seconds += sum(r["seconds"] for r in singleton)
        for query in queries:
            structure = set()
            covered = False
            for call in query["calls"]:
                if call["function"] == "llm_query":
                    structure = {
                        r["node_id"]
                        for r in call["output"][: manifest["retrieval"]["semantic"]["structure_candidate_limit"]]
                    }
                    covered = False
                if (
                    call["function"] == "vector_search"
                    and call["allowed_node_ids"] is not None
                    and set(call["allowed_node_ids"]).issubset(structure)
                ):
                    vector_covered += 1
                    vector_seconds += call["seconds"]
                    covered = True
                if call["function"] == "rerank_matches" and covered:
                    covered_rerank_sizes.append(len(call["candidates"]))
        contracts.append(
            {
                "contract_id": directory.name,
                "queries": len(queries),
                "build_seconds": manifest["contracts"][directory.name]["build_seconds"],
                "query_seconds_sum": sum(q["seconds"] for q in queries),
                "provider_requests": len(usage),
                "provider_prompt_tokens": sum(r.get("prompt_tokens", 0) for r in usage),
                "provider_completion_tokens": sum(r.get("completion_tokens", 0) for r in usage),
                "single_candidate_reranks": len(singleton),
            }
        )
    if contracts:
        write_csv(output / "capture_by_contract.csv", contracts, list(contracts[0]))
    offline = read_json(output / "offline_summary.json") if (output / "offline_summary.json").exists() else {}
    local = csv_rows(output / "local_rerank.csv")
    live = csv_rows(output / "live_summary.csv")
    formal = []
    failed_attempts = {}
    phases, criterion_traces = [], []
    # Old model judgements include mislabeled treatments and failed factual checks.
    # They remain archived but must not enter verified experiment statistics.
    recalls = []
    comments = []
    for variant in ("baseline", "safe"):
        for file in sorted((output / "formal_verified" / variant / "attempts").glob("*.json")):
            task = read_json(file)
            if task["status"] != "succeeded":
                failed_attempts[task["task_id"]] = {
                    "contract_id": task["contract_id"],
                    "variant": variant,
                    "task_id": task["task_id"],
                    "status": task["status"],
                    "seconds": task["duration_seconds"],
                    "error": task["error"],
                }
        for file in sorted((output / "formal_verified" / variant).glob("D[0-9][0-9].json")):
            task = read_json(file)
            if not task.get("experiment_valid"):
                continue
            events_path = (
                ROOT
                / "data"
                / "api"
                / f"docxindex_ablation_{variant}"
                / "api"
                / task["task_id"]
                / "logs"
                / "events.log"
            )
            events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
            if task["status"] != "succeeded":
                failed_attempts[task["task_id"]] = {
                    "contract_id": task["contract_id"],
                    "variant": variant,
                    "task_id": task["task_id"],
                    "status": task["status"],
                    "seconds": task["duration_seconds"],
                    "error": task["error"],
                }
            trace_path = (
                ROOT
                / "data"
                / "api"
                / f"docxindex_ablation_{variant}"
                / "api"
                / task["task_id"]
                / "logs"
                / "trace.json"
            )
            if trace_path.exists():
                runs = read_json(trace_path)["runs"]
                for run in runs:
                    common = {"contract_id": task["contract_id"], "variant": variant, "task_id": task["task_id"]}
                    if run["type"] == "phase":
                        phases.append(
                            {
                                **common,
                                "phase": run["name"],
                                "seconds": run["duration_seconds"],
                                "status": run["status"],
                                "trace_path": str(trace_path),
                            }
                        )
                    if run["type"] == "criterion":
                        ids = {run["id"]}
                        previous = 0
                        while len(ids) != previous:
                            previous = len(ids)
                            ids.update(r["id"] for r in runs if r["parent_id"] in ids)
                        children = [r for r in runs if r["id"] in ids]
                        criterion_traces.append(
                            {
                                **common,
                                "criterion": run["name"],
                                "seconds": run["duration_seconds"],
                                "search_calls": sum(r["name"] == "mcp.contract_search" for r in children),
                                "reflector_calls": sum(r["name"] == "llm.Reflector" for r in children),
                                "workflow_llm_spans": sum(r["type"] == "llm" for r in children),
                                "trace_path": str(trace_path),
                            }
                        )
            expected_rows = [r for r in recalls if r["contract_id"] == task["contract_id"] and r["variant"] == variant]
            comment_rows = [
                r
                for r in comments
                if r["contract_id"] == task["contract_id"] and r["variant"] == variant and r["author"] == "AI 条款审查"
            ]
            correct = sum(r["verdict"] == "correct" for r in comment_rows)
            incorrect = sum(r["verdict"] == "incorrect" for r in comment_rows)
            recalled = sum(r["expected_case"] == "valid" and r["recalled"] == "True" for r in expected_rows)
            formal.append(
                {
                    "contract_id": task["contract_id"],
                    "variant": variant,
                    "task_id": task["task_id"],
                    "status": task["status"],
                    "seconds": task["duration_seconds"],
                    "docx": task["output_docx"],
                    "verification": task.get("verification", "runtime intervention marker"),
                    "single_reranks_skipped": events.count("operation=skip_single_rerank"),
                    "llm_draft_recall": round(recalled / 18, 4) if len(expected_rows) == 18 else "",
                    "llm_draft_precision": round(correct / (correct + incorrect), 4) if correct + incorrect else "",
                    "recalled_of_18": recalled if expected_rows else "",
                    "correct_comments": correct if comment_rows else "",
                    "incorrect_comments": incorrect if comment_rows else "",
                    "uncertain_comments": sum(r["verdict"] == "uncertain" for r in comment_rows) if comment_rows else "",
                    "invalid_or_uncertain_expected": sum(r["expected_case"] != "valid" for r in expected_rows)
                    if expected_rows
                    else "",
                    "quality_status": "UNVERIFIED LLM draft; factual spot-check failed; do not use as quality gate"
                    if expected_rows
                    else "requires criterion-and-fact adjudication",
                }
            )
    write_csv(
        output / "formal_tasks.csv",
        formal,
        [
            "contract_id",
            "variant",
            "task_id",
            "status",
            "seconds",
            "docx",
            "verification",
            "single_reranks_skipped",
            "llm_draft_recall",
            "llm_draft_precision",
            "recalled_of_18",
            "correct_comments",
            "incorrect_comments",
            "uncertain_comments",
            "invalid_or_uncertain_expected",
            "quality_status",
        ],
    )
    if phases:
        write_csv(output / "formal_trace_phases.csv", phases, list(phases[0]))
    if criterion_traces:
        write_csv(output / "formal_trace_criteria.csv", criterion_traces, list(criterion_traces[0]))
    write_csv(
        output / "formal_failed_attempts.csv",
        list(failed_attempts.values()),
        ["contract_id", "variant", "task_id", "status", "seconds", "error"],
    )
    live_counts = {}
    noise = []
    for directory in sorted((output / "live").glob("*-R*")):
        if not directory.is_dir():
            continue
        baseline_fingerprints = []
        for repeat in range(3):
            files = sorted(directory.glob(f"*-{repeat}-evidence.json"))
            if files:
                baseline_fingerprints.append(json.dumps(anchor_texts(read_json(files[0])["baseline"]), sort_keys=True))
        if baseline_fingerprints:
            noise.append(
                {
                    "call_id": directory.name,
                    "repeats": len(baseline_fingerprints),
                    "distinct_baseline_anchor_sets": len(set(baseline_fingerprints)),
                }
            )
    write_json(output / "baseline_repeat_variation.json", noise)
    for variant in sorted({r["experiment"] for r in live}):
        selected = [r for r in live if r["experiment"] == variant]
        live_counts[variant] = {
            "pairs": len(selected),
            "same_anchor_evidence": sum(r["same_anchor_evidence"] == "True" for r in selected),
            "contracts": len({r["case_id"].split("-")[0] for r in selected}),
        }
    summary = {
        "contracts": len(contracts),
        "queries": sum(r["queries"] for r in contracts),
        "single_candidate_reranks": singleton_calls,
        "single_candidate_rerank_seconds_sum": singleton_seconds,
        "fully_covered_vector_calls": vector_covered,
        "covered_vector_seconds_sum": vector_seconds,
        "covered_vector_reranks_all_singleton": all(n == 1 for n in covered_rerank_sizes),
        "offline": offline,
        "local_rerank_rows": len(local),
        "live": live_counts,
        "repeated_baseline_calls": len(noise),
        "variable_baseline_calls": sum(r["distinct_baseline_anchor_sets"] > 1 for r in noise),
        "formal_tasks": len(formal),
        "formal_successes": sum(r["status"] == "succeeded" for r in formal),
        "formal_single_reranks_skipped": sum(r["single_reranks_skipped"] for r in formal),
    }
    summary["formal_failed_attempts_including_retried"] = len(failed_attempts)
    write_json(output / "summary.json", summary)
    lines = [
        "# 执行级消融实验结果",
        "",
        f"已采集 {len(contracts)} 份合同、{summary['queries']} 次真实检索。基线代码：`{manifest['git']}`。",
        "",
        "## 检索层确定性比较",
        "",
        "| 实验 | 比较次数 | 原文和锚点相同 | 跳过调用 | 重排输入改变 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, value in offline.items():
        lines.append(
            f"| {name} | {value['queries']} | {value['anchor_equal']} | {value['skipped_calls']} | {value['rerank_input_changed']} |"
        )
    lines += [
        "",
        "这里固定了模型响应；E02 若改变重排输入，不能凭上表判断完整等价。",
        "",
        f"全范围覆盖的向量调用：{vector_covered} 次；其后只有一个重排候选的调用：{sum(n == 1 for n in covered_rerank_sizes)} 次。限定到单候选条件，节点选择没有歧义。",
        "",
        f"单候选重排共 {singleton_calls} 次，其基线调用耗时合计 {singleton_seconds:.2f} 秒。",
        "该合计是串行工具层的可省调用耗时，不是完整并发审查的提速承诺。",
        "",
        "## 改变 LLM 输入的真实配对实验",
        "",
        "| 实验 | 合同数 | 配对次数 | 最终锚点原文相同 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, value in live_counts.items():
        lines.append(f"| {name} | {value['contracts']} | {value['pairs']} | {value['same_anchor_evidence']} |")
    lines += [
        "",
        "E03：取消重排前向量合并的数量截断，最终仍选 10 个节点。",
        "F01/F02/F03：分别只移除重排提示词中的 node_type/token_estimate/sources。",
        "字段实验每份合同只筛选一个候选载荷最大的重排调用，各配对 3 次；不是全调用覆盖。",
        "模型本身会波动，输出改变不等于质量变坏，也不能单凭未改变就删除字段。",
        "",
        f"同一重排输入重复 3 次：{len(noise)} 组中 {summary['variable_baseline_calls']} 组基线自己的最终锚点集合也发生变化。",
        "",
        "## 建议与边界",
        "",
        "- E01 单候选重排：可作为优先简化候选；唯一节点没有排序选择，允许略去无消费者的重排分数和理由。",
        "- E05 节点字典/E06 锚点范围索引：原文等价后再看收益；预计算维护成本也需要计入。",
        "- E07：仅在线单次重排 cache_dir=None 的无复用缓存操作；不代表可删除整个缓存模块。",
        "- E08 已停用：生产重排已移除局部 semaphore；不能通过关闭 Redis 全局模型控制制造对照。",
        "- E02：覆盖全作用域只能证明无新增节点；sources/vector_score 仍影响多节点重排，不能推广为“无新增都跳过”。",
        "- E03/E04/提示词字段：改变证据范围或模型输入，不能当作已证实的冗余直接删除。",
        "- VectorRetrievalConfig.auto_build：当前生产检索代码仅定义未读取；配置兼容迁移仍需单独处理。",
        "- node_type/token_estimate/sources 在其他代码中存在实际消费者；提示词消融不授权从节点 class/JSON 全局移除。",
        "",
        "## 正式主程序复测",
        "",
        f"当前已记录 {len(formal)} 个 API 任务，成功 {summary['formal_successes']} 个。见 formal_tasks.csv。",
        f"另按 task_id 保留全部失败尝试，共 {len(failed_attempts)} 次，含随后重试成功的原始失败；见 formal_failed_attempts.csv。",
        "当前链路为 app/API → Redis DB 0 → Celery worker → workflow → MCP → reviewer/reflector/summarizer → DOCX。",
        "历史任务曾使用进程内调度和 Redis 发布端口；当前启动器固定队列执行，DB 0/DB 2 地址完全读取配置，不绕过限流。",
        "历史 safe 组合包含 E01/E05/E06/E07/E08，现已停用；旧 LLM 重排与新独立 reranker 的数据不能直接混为当前对照。",
        "本轮候选组因修复启动器而补跑，未与早期基线交错执行；时间受模型波动及外部服务负载影响，不作因果提速结论。",
        "未判定的质量字段留空；完成 judge 的行是模型初判，不是人工准确率。",
        "抽检已发现自动评判存在事实误判，例如 D01-C10 对签署栏名称的判断；因此 llm_draft_* 仅作人工审阅草稿，不用于判定本轮质量提升或下降。",
        "召回按 18 条固定期望计算，错误或存疑期望另列；准确率=正确普通批注/(正确+错误普通批注)，存疑批注另列，总结不进该分母。",
        "初判依据审查标准、合同事实、可见批注和 AI 审查总结；原文由程序按真实批注ID提取，不依赖模型复述。",
        "",
        "## 明细入口",
        "",
        "- ablation_summary.csv / evidence_diffs.csv：逐用例离线结果、变化前后原文。",
        "- local_rerank.csv：独立重排函数的 E07 本地回放；concurrency.json 明确标识 E08 停用、全局限流未测量。",
        "- live_summary.csv / live/：真实配对的提示词、响应及最终证据。",
        "- content_boundary_diffs.csv：单节点构造的边界探针，不冒充实际查询预算违规。",
        "- formal_verified/<variant>/annotated/：正式主程序生成的有效对照批注合同。",
        "- invalid_treatment_runs.json：旧候选标签未传入 MCP 子进程的无效对照清单；原始 formal/ 保留但不参与本报告。",
        "",
    ]
    (output / "recommendations.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in {"offline", "live"}}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    report(parser.parse_args().output)


if __name__ == "__main__":
    main()
