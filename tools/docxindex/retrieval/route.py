from __future__ import annotations

import json
import re
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from tools.docxindex.llm import LLMClient
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash
from tools.docxindex.llm.config import docxindex_config, load_yaml
from tools.docxindex.llm.prompts import route_prompt
from tools.docxindex.llm.schemas import RouteResponse


DEFAULT_ROUTE_CONFIG = Path(__file__).resolve().parents[4] / "config.yaml"
ROUTE_PROMPT_VERSION = "docx_route_v6"
ROUTE_TITLE_MAX_DEPTH = 2
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NUMBERED_CRITERION = re.compile(r"^\s*(\d{1,2})(?:\s*[.、．]\s*|\s+)(.+)$", re.S)


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: Path
    expected_criteria_count: int = Field(default=18, ge=1)
    fallback_enabled: bool = True
    methods: dict[str, str]

    @classmethod
    def load(cls, path: Path | None = None) -> "RouteConfig":
        config_path = (path or DEFAULT_ROUTE_CONFIG).resolve()
        raw = load_yaml(config_path)
        data = dict(docxindex_config(raw).get("routing") or {})
        for legacy_key in ("llm_candidates", "vector_candidates", "vector_threshold", "max_nodes"):
            data.pop(legacy_key, None)
        source = Path(str(data.get("criteria") or ""))
        if not source.is_absolute():
            source = (config_path.parent / source).resolve()
        data["criteria"] = source
        config = cls.model_validate(data)
        if set(config.methods) != {"title", "region", "rule", "scan", "hybrid"}:
            raise ValueError("route methods must be exactly: title, region, rule, scan, hybrid")
        return config


def plan_route(
    index: dict[str, Any],
    query: str,
    client: LLMClient,
    config: RouteConfig,
    cache_dir: Path | None,
    criterion_text: str | None = None,
) -> RouteResponse:
    if criterion_text is None:
        criteria = load_criteria(config.criteria, config.expected_criteria_count)
        criterion = _criterion_for_query(query, criteria)
    else:
        criterion = criterion_text
    titles = _titles(index.get("structure_tree") or [])
    prompt = route_prompt(query, criterion, titles, config.methods)
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
        response = client.complete_model(prompt, RouteResponse)
        cached = response.model_dump(mode="json")
        cache.set(key, cached)
    return RouteResponse.model_validate(cached)


@lru_cache(maxsize=8)
def _cached_criteria(path_text: str, modified_ns: int, expected_count: int) -> dict[int, str]:
    del modified_ns
    path = Path(path_text)
    with zipfile.ZipFile(path) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.findall(f".//{{{W_NS}}}body/{{{W_NS}}}p"):
        text_parts = []
        for text_node in paragraph.iter(f"{{{W_NS}}}t"):
            if any(ancestor.tag == f"{{{W_NS}}}del" for ancestor in _ancestors(paragraph, text_node)):
                continue
            text_parts.append(text_node.text or "")
        text = "".join(text_parts).strip()
        if text:
            paragraphs.append(text)
    result = {}
    for text in paragraphs:
        match = NUMBERED_CRITERION.match(text)
        if match and 1 <= int(match.group(1)) <= expected_count:
            result[int(match.group(1))] = f"{match.group(1)}. {match.group(2).strip()}"
    if sorted(result) != list(range(1, expected_count + 1)):
        raise ValueError(f"criteria DOCX must contain numbered criteria 1..{expected_count}: {path}")
    return result


def load_criteria(path: Path, expected_count: int) -> dict[int, str]:
    resolved = path.resolve()
    return _cached_criteria(str(resolved), resolved.stat().st_mtime_ns, expected_count)


def _ancestors(root: ET.Element, target: ET.Element) -> list[ET.Element]:
    parents = {child: parent for parent in root.iter() for child in parent}
    result = []
    current = target
    while current in parents:
        current = parents[current]
        result.append(current)
    return result


def _criterion_for_query(query: str, criteria: dict[int, str]) -> str:
    match = re.match(r"^\s*(\d{1,2})(?:\s*[.、．]\s*|\s+)", query)
    return criteria.get(int(match.group(1)), query) if match else query


def _titles(
    nodes: list[dict[str, Any]],
    depth: int = 0,
    parent_id: str | None = None,
    region: str | None = None,
) -> list[dict[str, Any]]:
    if depth > ROUTE_TITLE_MAX_DEPTH:
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
