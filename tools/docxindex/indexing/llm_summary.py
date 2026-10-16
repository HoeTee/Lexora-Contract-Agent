from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from loggers.call_log import call_stage, current_call_stage, map_with_context
from dataclasses import dataclass
from pathlib import Path

from tools.docxindex.llm import LLMClient
from tools.docxindex.llm.client import LLMResponseValidationError
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash
from tools.docxindex.llm.prompts import (
    SUMMARY_BATCH_PROMPT_VERSION,
    SUMMARY_PROMPT_VERSION,
    summary_batch_prompt,
    summary_prompt,
)
from tools.docxindex.llm.schemas import SummaryBatchResponse, SummaryResponse
from tools.docxindex.schema import DocumentNode

from .token_budget import SUMMARY_TRIGGER_MIN_TOKENS, estimate_tokens

SUMMARY_INPUT_TARGET_TOKENS = 6000
TABLE_SUMMARY_INPUT_TARGET_TOKENS = 20000
SUMMARY_OUTPUT_MAX_TOKENS = 200


@dataclass(frozen=True)
class _SummaryTask:
    node: DocumentNode
    content: str
    cache_key: str


def summarize_nodes(
    roots: list[DocumentNode],
    client: LLMClient,
    cache_dir: Path | None,
    concurrency: int = 10,
    batch_max_nodes: int = 10,
    batch_max_tokens: int = 20000,
) -> dict[str, int]:
    cache = JsonlCache(cache_dir / "llm_summary.jsonl" if cache_dir else None)
    nodes: list[DocumentNode] = []
    for root in roots:
        _collect_nodes(root, nodes)

    tasks = _prepare_tasks(nodes, client, cache)
    batches = _pack_tasks(tasks, batch_max_nodes, batch_max_tokens)
    def summarize(entry):
        number, batch = entry
        with call_stage(f"index-summary-batch{number}"):
            return _summarize_batch(batch, client, cache)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        fallback_nodes = sum(map_with_context(executor, summarize, list(enumerate(batches, 1))))
    return {
        "eligible_nodes": len(tasks),
        "batches": len(batches),
        "fallback_nodes": fallback_nodes,
    }


def _collect_nodes(node: DocumentNode, result: list[DocumentNode]) -> None:
    for child in node.children:
        _collect_nodes(child, result)
    result.append(node)


def _prepare_tasks(nodes: list[DocumentNode], client: LLMClient, cache: JsonlCache) -> list[_SummaryTask]:
    tasks: list[_SummaryTask] = []
    for node in nodes:
        if node.token_estimate < SUMMARY_TRIGGER_MIN_TOKENS and not node.children:
            continue
        content = _summary_content(node)
        key = _node_cache_key(node, content, client)
        cached = cache.get(key)
        if cached and cached.get("summary"):
            node.summary = cached["summary"]
            continue
        tasks.append(_SummaryTask(node=node, content=content, cache_key=key))
    return tasks


def _node_cache_key(node: DocumentNode, content: str, client: LLMClient) -> str:
    key = cache_key(
        SUMMARY_PROMPT_VERSION,
        SUMMARY_BATCH_PROMPT_VERSION,
        client.settings.model,
        node.node_id,
        text_hash(content),
    )
    return key


def _pack_tasks(
    tasks: list[_SummaryTask],
    batch_max_nodes: int,
    batch_max_tokens: int,
) -> list[list[_SummaryTask]]:
    batches: list[list[_SummaryTask]] = []
    current: list[_SummaryTask] = []
    current_tokens = 0
    for task in tasks:
        task_tokens = estimate_tokens(task.content)
        if current and (
            len(current) >= max(1, batch_max_nodes)
            or current_tokens + task_tokens > max(1, batch_max_tokens)
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(task)
        current_tokens += task_tokens
    if current:
        batches.append(current)
    return batches


def _summarize_batch(tasks: list[_SummaryTask], client: LLMClient, cache: JsonlCache) -> int:
    if not tasks:
        return 0
    if len(tasks) == 1:
        _apply_individual_summary(tasks[0], client, cache)
        return 0

    prompt_nodes = [
        {
            "node_id": task.node.node_id,
            "title": task.node.title,
            "node_type": task.node.node_type,
            "content": task.content,
        }
        for task in tasks
    ]
    prompt = summary_batch_prompt(prompt_nodes, max_tokens=SUMMARY_OUTPUT_MAX_TOKENS)
    response = client.complete_model(prompt, SummaryBatchResponse)
    summaries = {
        item.node_id: _truncate_summary(item.summary.strip())
        for item in response.summaries
        if item.summary.strip()
    }

    fallback_nodes = 0
    for task in tasks:
        summary = summaries.get(task.node.node_id)
        if summary:
            _store_summary(task, summary, cache)
        else:
            fallback_nodes += 1
            _apply_individual_summary(task, client, cache, reason="批量摘要未返回该节点的有效摘要，改为逐节点生成")
    return fallback_nodes


def _apply_individual_summary(task: _SummaryTask, client: LLMClient, cache: JsonlCache,
                              *, reason: str | None = None) -> None:
    stage = f"{current_call_stage() or 'index-summary'}-node-{task.node.node_id}"
    with call_stage(stage, reason):
        generated = _generate_summary(client, task.node, task.content)
    _store_summary(task, generated["summary"].strip(), cache)


def _store_summary(task: _SummaryTask, summary: str, cache: JsonlCache) -> None:
    normalized = _truncate_summary(summary.strip())
    if not normalized:
        return
    task.node.summary = normalized
    cache.set(task.cache_key, {"summary": normalized})


def _generate_summary(client: LLMClient, node: DocumentNode, content: str) -> dict[str, str]:
    prompt = summary_prompt(node.title, node.node_type, content, max_tokens=SUMMARY_OUTPUT_MAX_TOKENS)
    last_summary = ""

    def check_length(response: SummaryResponse) -> SummaryResponse:
        nonlocal last_summary
        summary = response.summary.strip()
        last_summary = summary
        if not summary:
            raise ValueError("Summary must contain non-whitespace text.")
        if estimate_tokens(summary) > SUMMARY_OUTPUT_MAX_TOKENS:
            raise ValueError(f"Summary exceeds {SUMMARY_OUTPUT_MAX_TOKENS} tokens; shorten its text.")
        return response

    try:
        response = client.complete_model(prompt, SummaryResponse, validate=check_length)
    except LLMResponseValidationError as exc:
        if getattr(exc, "failure_stage", None) != "output_validation" or not last_summary:
            raise
        # Reuse an already parsed candidate after output repairs are exhausted.
        # Network/deadline failures must propagate instead of starting more calls.
        return {"summary": _truncate_summary(last_summary)}
    return {"summary": response.summary.strip()}


def _truncate_summary(summary: str) -> str:
    if estimate_tokens(summary) <= SUMMARY_OUTPUT_MAX_TOKENS:
        return summary
    chars = max(120, int(len(summary) * SUMMARY_OUTPUT_MAX_TOKENS / max(estimate_tokens(summary), 1)))
    result = summary[:chars].rstrip()
    while estimate_tokens(result) > SUMMARY_OUTPUT_MAX_TOKENS:
        result = result[: max(0, len(result) - max(1, len(result) // 10))].rstrip()
    return result


def _summary_content(node: DocumentNode) -> str:
    input_tokens = (
        TABLE_SUMMARY_INPUT_TARGET_TOKENS
        if node.node_type in {"table", "table_chunk"}
        else SUMMARY_INPUT_TARGET_TOKENS
    )
    if node.children:
        lines = []
        for child in node.children:
            child_summary = child.summary or child.title
            lines.append(f"- {child.title}: {child_summary}")
        return _truncate_by_tokens("\n".join(lines), input_tokens)
    return _truncate_by_tokens(node.text, input_tokens)


def _truncate_by_tokens(text: str, input_tokens: int = SUMMARY_INPUT_TARGET_TOKENS) -> str:
    if estimate_tokens(text) <= input_tokens:
        return text
    chars = max(1000, int(len(text) * input_tokens / max(estimate_tokens(text), 1)))
    return text[:chars].rstrip() + "..."
