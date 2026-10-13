from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from loggers.call_log import call_stage

from routing.config import RouteConfig
from routing.criteria import split_criteria_from_docx
from routing.planner import plan_route as plan_dynamic_route
from routing.schemas import RoutePlan
from tools.docxindex.llm import LLMClient
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash


ROUTE_PROMPT_VERSION = "docx_route_v9"
ROUTE_TITLE_MAX_DEPTH = 2
ROUTE_TITLE_ATTACHMENT_MAX_DEPTH = 4


def plan_route(
    index: dict[str, Any],
    query: str,
    client: LLMClient,
    config: RouteConfig,
    cache_dir: Path | None,
    criterion_text: str | None = None,
) -> RoutePlan:
    criterion = criterion_text or query
    titles = _titles(index.get("structure_tree") or [])
    cache = JsonlCache(cache_dir / "route.jsonl" if cache_dir else None)
    key = cache_key(
        ROUTE_PROMPT_VERSION,
        client.settings.model,
        query,
        text_hash(criterion),
        text_hash(json.dumps(config.methods, ensure_ascii=False, sort_keys=True)),
        text_hash(json.dumps(titles, ensure_ascii=False, sort_keys=True)),
    )
    cached = cache.get(key)
    if cached is None:
        with call_stage("route-plan"):
            response = plan_dynamic_route(
                query=query,
                criterion=criterion,
                titles=titles,
                methods=config.methods,
                client=client,
            )
        cached = response.model_dump(mode="json")
        cache.set(key, cached)
    return RoutePlan.model_validate(cached)


def load_criteria(path: Path, expected_count: int) -> dict[int, str]:
    """Compatibility helper used by the existing ablation suite."""
    criteria = split_criteria_from_docx(path)
    result = {item["number"]: f"{item['number']}. {item['criterion']}" for item in criteria}
    if sorted(result) != list(range(1, expected_count + 1)):
        raise ValueError(f"criteria DOCX must contain numbered criteria 1..{expected_count}: {path}")
    return result


def _titles(
    nodes: list[dict[str, Any]],
    depth: int = 0,
    parent_id: str | None = None,
    region: str | None = None,
) -> list[dict[str, Any]]:
    max_depth = ROUTE_TITLE_ATTACHMENT_MAX_DEPTH if region == "attachments" else ROUTE_TITLE_MAX_DEPTH
    if depth > max_depth:
        return []
    result: list[dict[str, Any]] = []
    for node in nodes:
        title = str(node.get("title") or "").strip()
        node_type = str(node.get("node_type") or "")
        node_id = str(node.get("node_id") or "")
        local_region = node_id if node_id in {"frontmatter", "body", "tail", "attachments"} else region
        if title and node_type not in {"body", "attachments"}:
            result.append(
                {
                    "node_id": node_id,
                    "title": title,
                    "node_type": node_type,
                    "depth": depth,
                    "parent_id": parent_id,
                    "region": local_region,
                }
            )
        result.extend(
            _titles(
                node.get("nodes") or node.get("children") or [],
                depth + 1,
                node_id or parent_id,
                local_region,
            )
        )
    return result
