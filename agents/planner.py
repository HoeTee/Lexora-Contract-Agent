"""
PlannerAgent splits criteria DOCX deterministically and extracts per-criterion
retrieval routes and placement targets via one batched LLM call per review run.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from agents.base_agent import Agent
from agents.json_utils import chat_until_valid_json
from agents.prompts.cn_prompts import PLANNER_STRATEGY_PROMPT
from agents.schemas import StrategyExtractionOutput


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SECTION_RE = re.compile(r"^[一二三四五六七八九十]+\s*、")
NUMBER_RE = re.compile(r"^\s*(\d{1,2})\s*[.、)]\s*(.+)$", re.S)

STRATEGY_EXPECTED_JSON = """
{
  "strategies": [
    {
      "id": "C1",
      "review_instruction": "",
      "route": {
        "steps": [
          {"method": "rule", "terms": ["关键词"], "regions": [], "all_titles": false, "anchor_terms": [], "coverage": "relevant"}
        ],
        "fallback": false
      },
      "placement": {"target": "inline", "note": ""}
    }
  ]
}
"""


def _read_docx_paragraphs(docx_path: Path) -> list[str]:
    """Read non-deleted paragraphs from a criteria DOCX, in document order."""
    with zipfile.ZipFile(docx_path) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    parents = {child: parent for parent in root.iter() for child in parent}
    paragraphs: list[str] = []
    for paragraph in root.findall(f".//{{{W_NS}}}body/{{{W_NS}}}p"):
        text_parts = []
        for text_node in paragraph.iter(f"{{{W_NS}}}t"):
            if any(ancestor.tag == f"{{{W_NS}}}del" for ancestor in _ancestors(parents, text_node)):
                continue
            text_parts.append(text_node.text or "")
        text = "".join(text_parts).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def _ancestors(parents: dict, target: ET.Element) -> list[ET.Element]:
    result = []
    current = target
    while current in parents:
        current = parents[current]
        result.append(current)
    return result


def split_criteria_from_docx(docx_path: str | Path) -> list[dict]:
    """Split a criteria DOCX into numbered skeletons: id/number/section/criterion.

    Deterministic splitting rules:
    - numbered paragraphs (``1.``/``2、``) always start a new criterion;
    - chapter lines (``一、`` prefix or a short heading) update the section;
    - with numbered paragraphs present, other paragraphs join the previous
      criterion; otherwise every content paragraph is its own criterion;
    - the first non-empty paragraph without a number is treated as the
      document title and skipped.
    """
    path = Path(docx_path)
    paragraphs = _read_docx_paragraphs(path)
    if not paragraphs:
        raise ValueError(f"criteria DOCX has no readable paragraphs: {path}")
    has_numbered = any(NUMBER_RE.match(text) for text in paragraphs)

    criteria: list[dict] = []
    section = ""
    for index, text in enumerate(paragraphs):
        match = NUMBER_RE.match(text)
        if match:
            criteria.append({
                "number": int(match.group(1)),
                "section": section,
                "criterion": match.group(2).strip(),
            })
            continue
        if index == 0:
            # Document title line (e.g. 审核要点), not a criterion.
            continue
        if SECTION_RE.match(text):
            section = SECTION_RE.sub("", text).strip()
            continue
        if not has_numbered and len(text) <= 10 and not text.endswith(("。", "；", ";", ":" , "：")):
            # Short bare heading (e.g. 合同主体) acts as a section line.
            section = text
            continue
        if has_numbered:
            if criteria:
                criteria[-1]["criterion"] += "\n" + text
            continue
        criteria.append({"number": None, "section": section, "criterion": text})

    if not criteria:
        raise ValueError(f"no criteria could be split from DOCX: {path}")
    for position, criterion in enumerate(criteria, 1):
        criterion["id"] = f"C{position}"
        criterion["section"] = criterion["section"] or "其他"
        if criterion["number"] is None:
            criterion["number"] = position
    return criteria


class PlannerAgent(Agent):
    """Extracts per-criterion route/placement strategies from split skeletons."""

    def __init__(self, **kwargs):
        super().__init__(
            system_prompt=PLANNER_STRATEGY_PROMPT,
            name="Planner",
            **kwargs,
        )

    async def extract_strategies(self, skeletons: list[dict]) -> dict[str, dict]:
        """Infer route/placement for every criterion in one batched LLM call.

        Returns a mapping from criterion id to its strategy dict.
        """
        listing = [
            {"id": item["id"], "section": item["section"], "criterion": item["criterion"]}
            for item in skeletons
        ]
        prompt = (
            "请为以下每一条审查标准生成执行策略（route 与 placement）。\n"
            "输入条目：\n"
            f"{json.dumps(listing, ensure_ascii=False, indent=2)}"
        )
        parsed, _ = await chat_until_valid_json(
            self,
            prompt,
            StrategyExtractionOutput,
            STRATEGY_EXPECTED_JSON,
        )
        return {item["id"]: item for item in parsed.get("strategies", [])}


def assemble_tasks(skeletons: list[dict], strategies: dict[str, dict]) -> list[dict]:
    """Merge skeletons and strategies into the runtime criteria list."""
    criteria_list: list[dict] = []
    for item in skeletons:
        strategy = strategies.get(item["id"]) or {}
        route = strategy.get("route")
        placement = strategy.get("placement")
        criteria_list.append({
            "id": item["id"],
            "number": item["number"],
            "section": item["section"],
            "criterion": item["criterion"],
            "check_points": [item["criterion"]],
            "review_instruction": (strategy.get("review_instruction") or "").strip(),
            "route": route,
            "placement": placement,
            "fallback_llm_route": route is None,
        })
    return criteria_list
