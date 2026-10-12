from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any

from tools.docxindex.indexing import estimate_tokens
from tools.docxindex.llm import LLMClient
from tools.docxindex.llm.prompts import scan_prompt
from tools.docxindex.llm.schemas import ScanResponse
from loggers.call_log import (
    append_route_event,
    logical_call,
    map_with_context,
    reset_call_stage,
    set_call_stage,
)

from .title import _candidate

SCAN_PROMPT_VERSION = "docx_scan_v3"


def scan_matches(
    index: dict[str, Any],
    query: str,
    client: LLMClient,
    batch_tokens: int = 6000,
    concurrency: int = 10,
    allowed_node_ids: set[str] | None = None,
    candidate_instruction: str = "",
) -> list[dict[str, Any]]:
    """Inspect every non-empty anchor and return only LLM-confirmed findings."""
    records = _anchor_records(index, allowed_node_ids=allowed_node_ids)
    batches = _build_batches(records, batch_tokens)
    if not batches:
        return []

    def inspect(entry: tuple[int, list[tuple[str, dict[str, Any], dict[str, Any], str]]]) -> list[dict[str, Any]]:
        with logical_call("docxindex.llm", client.settings.model):
            return inspect_batch(entry)

    def inspect_batch(entry: tuple[int, list[tuple[str, dict[str, Any], dict[str, Any], str]]]) -> list[dict[str, Any]]:
        batch_index, batch = entry
        items = [{"anchor_id": anchor_id, "text": text} for anchor_id, _, _, text in batch]
        prompt = scan_prompt(query, items, candidate_instruction=candidate_instruction)
        text_tokens = sum(estimate_tokens(item[3]) for item in batch)
        prompt_tokens = estimate_tokens(prompt)
        response = None
        for attempt in range(1, 3):
            stage_token = set_call_stage(f"scan.batch-{batch_index:03d}")
            started = time.monotonic()
            append_route_event(
                "scan_batch_started",
                {
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "attempt": attempt,
                    "anchor_count": len(batch),
                    "text_tokens": text_tokens,
                    "prompt_tokens": prompt_tokens,
                },
            )
            try:
                response = client.complete_model(prompt, ScanResponse)
            except Exception as exc:
                append_route_event(
                    "scan_batch_failed",
                    {
                        "batch_index": batch_index,
                        "batch_count": len(batches),
                        "attempt": attempt,
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                if attempt == 2:
                    raise
                time.sleep(1)
            else:
                append_route_event(
                    "scan_batch_succeeded",
                    {
                        "batch_index": batch_index,
                        "batch_count": len(batches),
                        "attempt": attempt,
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "finding_count": len(response.findings),
                    },
                )
                break
            finally:
                reset_call_stage(stage_token)
        if response is None:
            raise RuntimeError(f"Scan batch {batch_index} did not produce a response.")
        by_anchor = {anchor_id: (record, owner, text) for anchor_id, record, owner, text in batch}
        findings: list[dict[str, Any]] = []
        seen: set[str] = set()
        for finding in response.findings:
            anchor_id = finding.anchor_id.strip()
            if anchor_id in seen or anchor_id not in by_anchor:
                continue
            seen.add(anchor_id)
            record, owner, text = by_anchor[anchor_id]
            candidate = _candidate(owner, "scan")
            body_index = record.get("body_child_index")
            candidate.update(
                {
                    "node_id": f"scan/{anchor_id}",
                    "parent_node_id": owner.get("node_id"),
                    "text_override": text,
                    "start_index": body_index,
                    "end_index": body_index,
                    "start_anchor": anchor_id,
                    "end_anchor": anchor_id,
                    "reason": finding.reason.strip(),
                }
            )
            findings.append(candidate)
        return findings

    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(batches)))) as executor:
        groups = map_with_context(executor, inspect, list(enumerate(batches, 1)))
    return [finding for group in groups for finding in group]


def _anchor_records(
    index: dict[str, Any],
    allowed_node_ids: set[str] | None = None,
) -> list[tuple[str, dict[str, Any], dict[str, Any], str]]:
    by_id = {node.get("node_id"): node for node in index.get("nodes") or []}
    records: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
    anchors = sorted(
        (index.get("anchor_map") or {}).items(),
        key=lambda item: item[1].get("body_child_index") or 0,
    )
    for anchor_id, record in anchors:
        owner = by_id.get(record.get("node_id"))
        text = str(record.get("raw_text") or record.get("text") or "").strip()
        if owner and text and (
            allowed_node_ids is None or owner.get("node_id") in allowed_node_ids
        ):
            records.append((anchor_id, record, owner, text))
    return records


def _build_batches(
    records: list[tuple[str, dict[str, Any], dict[str, Any], str]],
    batch_tokens: int,
) -> list[list[tuple[str, dict[str, Any], dict[str, Any], str]]]:
    batches: list[list[tuple[str, dict[str, Any], dict[str, Any], str]]] = []
    batch: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
    used = 0
    for item in records:
        tokens = estimate_tokens(item[3])
        if batch and used + tokens > batch_tokens:
            batches.append(batch)
            batch = []
            used = 0
        batch.append(item)
        used += tokens
    if batch:
        batches.append(batch)
    return batches
