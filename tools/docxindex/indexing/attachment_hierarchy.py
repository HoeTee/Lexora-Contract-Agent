from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from loggers.call_log import call_stage, logical_call

from tools.docxindex.detection import heading_level, heading_score, is_plain_label, is_visual_title
from tools.docxindex.llm import LLMClient
from tools.docxindex.llm.client import LLMResponseValidationError
from tools.docxindex.llm.cache import JsonlCache, cache_key, text_hash
from tools.docxindex.llm.prompts import ATTACHMENT_HIERARCHY_PROMPT_VERSION, attachment_hierarchy_prompt
from tools.docxindex.llm.schemas import AttachmentHeading, AttachmentHierarchyResponse, AttachmentHierarchySegment
from tools.docxindex.schema import BodyItem, DocumentNode

from .node_factory import make_node
from .splitter import split_long_leaves
from .token_budget import estimate_tokens


CN_NUM = "一二三四五六七八九十百零〇两壹贰叁肆伍陆柒捌玖拾"
MAX_CANDIDATE_TEXT_CHARS = 240
MAX_NEIGHBOR_TEXT_CHARS = 100

NUMBER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subattachment", re.compile(rf"^附件\s*[{CN_NUM}0-9]+[-.]\d+")),
    ("chapter", re.compile(rf"^第([{CN_NUM}0-9]+)章")),
    ("article", re.compile(rf"^第([{CN_NUM}0-9]+)条")),
    ("section", re.compile(rf"^第([{CN_NUM}0-9]+)节")),
    ("cn_comma", re.compile(rf"^[{CN_NUM}]+[、.]")),
    ("cn_paren", re.compile(rf"^[（(][{CN_NUM}]+[）)]")),
    ("arabic_paren", re.compile(r"^[（(]\d+[）)]")),
    ("arabic_multilevel", re.compile(r"^\d+(?:\.\d+)+[.、）)]?")),
    ("arabic_comma", re.compile(r"^\d+、")),
    ("arabic_dot", re.compile(r"^\d+[.]")),
    ("arabic_close", re.compile(r"^\d+[）)]")),
    ("letter", re.compile(r"^[A-Za-z][.、）)]")),
)


@dataclass(frozen=True)
class AttachmentCandidate:
    item_index: int
    anchor: str
    text: str
    number_family: str
    ordinal: int | None
    required: bool
    signals: tuple[str, ...]
    next_text: str


@dataclass(frozen=True)
class ValidatedHeading:
    candidate: AttachmentCandidate
    level: int


def infer_attachment_children(
    node: DocumentNode,
    items: list[BodyItem],
    client: LLMClient,
    cache_dir: Path | None,
    *,
    input_max_tokens: int = 20000,
    batch_target_tokens: int = 16000,
    batch_overlap_tokens: int = 800,
    max_levels: int = 6,
    split_long_nodes: bool = True,
    paragraph_split_threshold_tokens: int = 1000,
    paragraph_chunk_target_tokens: int = 700,
) -> list[DocumentNode] | None:
    """Infer an attachment-local hierarchy and return nested child nodes."""
    return infer_hierarchy_children(
        node,
        items,
        client,
        cache_dir,
        inside_attachment=True,
        input_max_tokens=input_max_tokens,
        batch_target_tokens=batch_target_tokens,
        batch_overlap_tokens=batch_overlap_tokens,
        max_levels=max_levels,
        split_long_nodes=split_long_nodes,
        paragraph_split_threshold_tokens=paragraph_split_threshold_tokens,
        paragraph_chunk_target_tokens=paragraph_chunk_target_tokens,
    )


def infer_hierarchy_children(
    node: DocumentNode,
    items: list[BodyItem],
    client: LLMClient,
    cache_dir: Path | None,
    *,
    inside_attachment: bool,
    input_max_tokens: int = 20000,
    batch_target_tokens: int = 16000,
    batch_overlap_tokens: int = 800,
    max_levels: int = 6,
    split_long_nodes: bool = True,
    paragraph_split_threshold_tokens: int = 1000,
    paragraph_chunk_target_tokens: int = 700,
) -> list[DocumentNode] | None:
    """Infer a validated local hierarchy for an attachment or an oversized body leaf."""
    if node.source_start is None or node.source_end is None:
        return None
    candidates = collect_hierarchy_candidates(
        items,
        node.source_start + 1,
        node.source_end,
        inside_attachment=inside_attachment,
    )
    if len(candidates) < 2:
        return None

    cache = JsonlCache(cache_dir / "llm_hierarchy.jsonl" if cache_dir else None)
    headings = _infer_profile(
        node,
        candidates,
        _pattern_summary(candidates),
        client,
        cache,
        input_max_tokens,
        batch_target_tokens,
        batch_overlap_tokens,
        max_levels,
        inside_attachment,
    )
    if headings is None or len(headings) < 2:
        return None
    children = _build_hierarchy_nodes(node, items, headings, inside_attachment=inside_attachment)
    if split_long_nodes:
        split_long_leaves(children, items, paragraph_split_threshold_tokens, paragraph_chunk_target_tokens)
    return children or None


def collect_attachment_candidates(items: list[BodyItem], start: int, end: int) -> list[AttachmentCandidate]:
    return collect_hierarchy_candidates(items, start, end, inside_attachment=True)


def collect_hierarchy_candidates(
    items: list[BodyItem],
    start: int,
    end: int,
    *,
    inside_attachment: bool,
) -> list[AttachmentCandidate]:
    result: list[AttachmentCandidate] = []
    paragraph_position = 0
    for index in range(start, end):
        item = items[index]
        if item.kind != "p" or not item.text.strip():
            continue
        paragraph_position += 1
        family = number_family(item.text)
        outline_valid = item.effective_outline is not None and 0 <= item.effective_outline <= 8
        numbered = family != "unnumbered"
        visual = is_visual_title(item, items[index + 1 : end], paragraph_position - 1)
        label = is_plain_label(item)
        formatted = item.alignment == "center" or item.bold_fraction >= 0.5 or (item.max_font_size or 0) >= 28
        opening_short = inside_attachment and paragraph_position <= 8 and len(item.text) <= 80
        if not (outline_valid or numbered or visual or label or (formatted and len(item.text) <= 120) or opening_short):
            continue

        signals: list[str] = []
        if outline_valid:
            signals.append(f"outline:{item.effective_outline}")
        deterministic_level = heading_level(item)
        if deterministic_level is not None:
            signals.append(f"deterministic_level:{deterministic_level}")
        if numbered:
            signals.append(f"number:{family}")
        if visual:
            signals.append("visual_title")
        if label:
            signals.append("plain_label")
        if item.alignment == "center":
            signals.append("center")
        if item.bold_fraction >= 0.5:
            signals.append("bold")
        if (item.max_font_size or 0) >= 28:
            signals.append("large_font")
        if opening_short:
            signals.append("attachment_opening")

        result.append(
            AttachmentCandidate(
                item_index=index,
                anchor=item.anchor,
                text=item.text.strip(),
                number_family=family,
                ordinal=number_ordinal(item.text, family),
                required=family == "subattachment" or (outline_valid and numbered),
                signals=tuple(signals),
                next_text=_next_text(items, index + 1, end),
            )
        )
    return _mark_continuous_sequences_required(result)


def _mark_continuous_sequences_required(candidates: list[AttachmentCandidate]) -> list[AttachmentCandidate]:
    required_anchors = {candidate.anchor for candidate in candidates if candidate.required}
    by_family: dict[str, list[AttachmentCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.number_family != "unnumbered" and candidate.ordinal is not None:
            by_family[candidate.number_family].append(candidate)
    for family_candidates in by_family.values():
        run: list[AttachmentCandidate] = []
        previous_ordinal: int | None = None
        for candidate in family_candidates:
            if previous_ordinal is not None and candidate.ordinal == previous_ordinal + 1:
                run.append(candidate)
            else:
                if len(run) >= 2:
                    required_anchors.update(item.anchor for item in run)
                run = [candidate]
            previous_ordinal = candidate.ordinal
        if len(run) >= 2:
            required_anchors.update(item.anchor for item in run)
    return [replace(candidate, required=candidate.anchor in required_anchors) for candidate in candidates]


def number_family(text: str) -> str:
    stripped = text.strip()
    for name, pattern in NUMBER_PATTERNS:
        if pattern.match(stripped):
            return name
    return "unnumbered"


def number_ordinal(text: str, family: str) -> int | None:
    stripped = text.strip()
    if family == "subattachment":
        match = re.match(rf"^附件\s*[{CN_NUM}0-9]+[-.](\d+)", stripped)
        return int(match.group(1)) if match else None
    if family in {"chapter", "article", "section"}:
        suffix = {"chapter": "章", "article": "条", "section": "节"}[family]
        match = re.match(rf"^第([{CN_NUM}0-9]+){suffix}", stripped)
        return _number_value(match.group(1)) if match else None
    if family in {"cn_comma", "cn_paren"}:
        match = re.match(rf"^[（(]?([{CN_NUM}]+)", stripped)
        return _number_value(match.group(1)) if match else None
    if family.startswith("arabic_"):
        match = re.match(r"^[（(]?(\d+)", stripped)
        return int(match.group(1)) if match else None
    if family == "letter":
        return ord(stripped[0].upper()) - ord("A") + 1
    return None


def _number_value(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {
        "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
        "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
        "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8,
        "九": 9, "玖": 9,
    }
    if value in digits:
        return digits[value]
    if "十" in value or "拾" in value:
        parts = re.split("[十拾]", value, maxsplit=1)
        tens = digits.get(parts[0], 1) if parts[0] else 1
        ones = digits.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None


def _next_text(items: list[BodyItem], start: int, end: int) -> str:
    for item in items[start:end]:
        if item.text:
            return item.text.replace("\n", " ").strip()[:MAX_NEIGHBOR_TEXT_CHARS]
    return ""


def _pattern_summary(candidates: list[AttachmentCandidate]) -> str:
    counts = Counter(candidate.number_family for candidate in candidates)
    examples: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        if len(examples[candidate.number_family]) < 3:
            examples[candidate.number_family].append(candidate.text[:120])
    payload = {
        family: {"count": count, "examples": examples[family]}
        for family, count in sorted(counts.items())
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _candidate_row(candidate: AttachmentCandidate) -> str:
    text = candidate.text
    if len(text) > MAX_CANDIDATE_TEXT_CHARS:
        text = text[:MAX_CANDIDATE_TEXT_CHARS].rstrip() + "..."
    payload = {
        "anchor": candidate.anchor,
        "text": text,
        "number_family": candidate.number_family,
        "ordinal": candidate.ordinal,
        "required": candidate.required,
        "signals": list(candidate.signals),
    }
    # Adjacent text helps classify visual/plain titles but adds no value for numbered headings.
    if candidate.number_family == "unnumbered" and candidate.next_text:
        payload["next_text"] = candidate.next_text
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _infer_profile(
    node: DocumentNode,
    candidates: list[AttachmentCandidate],
    pattern_summary: str,
    client: LLMClient,
    cache: JsonlCache,
    input_max_tokens: int,
    batch_target_tokens: int,
    batch_overlap_tokens: int,
    max_levels: int,
    inside_attachment: bool,
) -> list[ValidatedHeading] | None:
    candidate_text = "\n".join(_candidate_row(candidate) for candidate in candidates)
    prompt = attachment_hierarchy_prompt(
        node.title,
        pattern_summary,
        candidate_text,
        max_levels,
        inside_attachment=inside_attachment,
    )
    if input_max_tokens is None or estimate_tokens(prompt) <= input_max_tokens:
        return _infer_profile_batch(
            node,
            candidates,
            pattern_summary,
            client,
            cache,
            max_levels,
            inside_attachment,
        )

    batches = _candidate_batches(
        node.title,
        candidates,
        max_levels,
        batch_target_tokens,
        batch_overlap_tokens,
        inside_attachment,
    )
    inferred_batches: list[list[ValidatedHeading]] = []
    for batch in batches:
        inferred = _infer_profile_batch(
            node,
            batch,
            _pattern_summary(batch),
            client,
            cache,
            max_levels,
            inside_attachment,
        )
        if inferred:
            inferred_batches.append(inferred)
    return _merge_heading_batches(candidates, inferred_batches, max_levels)


def _infer_profile_batch(
    node: DocumentNode,
    candidates: list[AttachmentCandidate],
    pattern_summary: str,
    client: LLMClient,
    cache: JsonlCache,
    max_levels: int,
    inside_attachment: bool,
) -> list[ValidatedHeading] | None:
    candidate_text = "\n".join(_candidate_row(candidate) for candidate in candidates)
    prompt = attachment_hierarchy_prompt(
        node.title,
        pattern_summary,
        candidate_text,
        max_levels,
        inside_attachment=inside_attachment,
    )
    key = cache_key(
        ATTACHMENT_HIERARCHY_PROMPT_VERSION,
        client.settings.model,
        "attachment" if inside_attachment else "body",
        node.node_id,
        candidates[0].anchor,
        candidates[-1].anchor,
        text_hash(candidate_text),
    )
    cached = cache.get(key)
    if cached is not None:
        response = AttachmentHierarchyResponse.model_validate(cached)
        validated, errors = _apply_profile(candidates, response, max_levels)
        return validated if not errors else None

    stage = f"index-hierarchy-{node.node_id}-{candidates[0].anchor}-{candidates[-1].anchor}"
    with call_stage(stage), logical_call("docxindex.llm", client.settings.model) as logical:
        return _infer_profile_attempts(prompt, candidates, client, cache, key, max_levels, logical)


def _infer_profile_attempts(prompt, candidates, client, cache, key, max_levels, logical):
    def validate(response):
        validated, errors = _apply_profile(candidates, response, max_levels)
        if errors:
            raise ValueError("；".join(errors))
        cache.set(key, response.model_dump(mode="json"))
        return validated

    try:
        return client.complete_model(prompt, AttachmentHierarchyResponse, validate=validate)
    except LLMResponseValidationError as exc:
        if getattr(exc, "failure_stage", None) != "output_validation":
            raise
        # Hierarchy inference is optional enrichment. Preserve the existing
        # deterministic chunking fallback without starting a new model operation.
        logical.failed(exc)
        return None


def _candidate_batches(
    node_title: str,
    candidates: list[AttachmentCandidate],
    max_levels: int,
    target_tokens: int,
    overlap_tokens: int,
    inside_attachment: bool,
) -> list[list[AttachmentCandidate]]:
    batches: list[list[AttachmentCandidate]] = []
    start = 0
    while start < len(candidates):
        end = start
        current: list[AttachmentCandidate] = []
        while end < len(candidates):
            proposed = [*current, candidates[end]]
            proposed_text = "\n".join(_candidate_row(candidate) for candidate in proposed)
            proposed_prompt = attachment_hierarchy_prompt(
                node_title,
                _pattern_summary(proposed),
                proposed_text,
                max_levels,
                inside_attachment=inside_attachment,
            )
            if current and estimate_tokens(proposed_prompt) > target_tokens:
                break
            current = proposed
            end += 1
        if not current:
            current = [candidates[start]]
            end = start + 1
        batches.append(current)
        if end >= len(candidates):
            break
        overlap_start = end
        used = 0
        while overlap_start > start + 1 and used < overlap_tokens:
            overlap_start -= 1
            used += estimate_tokens(_candidate_row(candidates[overlap_start]))
        start = overlap_start if overlap_start > start else end
    return batches


def _merge_heading_batches(
    all_candidates: list[AttachmentCandidate],
    batches: list[list[ValidatedHeading]],
    max_levels: int,
) -> list[ValidatedHeading] | None:
    if not batches:
        return None
    merged: dict[str, ValidatedHeading] = {}
    for batch in batches:
        offsets = [
            merged[item.candidate.anchor].level - item.level
            for item in batch
            if item.candidate.anchor in merged
        ]
        offset = Counter(offsets).most_common(1)[0][0] if offsets else 0
        for item in batch:
            if item.candidate.anchor in merged:
                continue
            level = min(max(item.level + offset, 1), max_levels)
            merged[item.candidate.anchor] = ValidatedHeading(item.candidate, level)
    result = sorted(merged.values(), key=lambda value: value.candidate.item_index)
    required = {candidate.anchor for candidate in all_candidates if candidate.required}
    if not required.issubset(merged):
        return None
    return result if len(result) >= 2 and not _hierarchy_errors(result, max_levels) else None


def _apply_profile(
    candidates: list[AttachmentCandidate],
    response: AttachmentHierarchyResponse,
    max_levels: int,
) -> tuple[list[ValidatedHeading], list[str]]:
    by_anchor = {candidate.anchor: candidate for candidate in candidates}
    errors: list[str] = []
    segments: list[tuple[int, AttachmentHierarchySegment]] = []
    seen_starts: set[str] = set()
    original_indexes: list[int] = []
    for segment in response.segments:
        start_candidate = by_anchor.get(segment.start_anchor)
        if start_candidate is None:
            errors.append(f"segment start_anchor不存在:{segment.start_anchor}")
            continue
        if segment.start_anchor in seen_starts:
            errors.append(f"segment start_anchor重复:{segment.start_anchor}")
            continue
        seen_starts.add(segment.start_anchor)
        original_indexes.append(start_candidate.item_index)
        segments.append((start_candidate.item_index, segment))
    if original_indexes != sorted(original_indexes):
        errors.append("segment顺序与原文不一致")
    segments.sort(key=lambda value: value[0])
    if not segments:
        return [], errors

    first_required = next((candidate for candidate in candidates if candidate.required), None)
    if first_required is not None and segments[0][0] > first_required.item_index:
        errors.append(f"首个segment晚于required候选:{first_required.anchor}")

    result_by_anchor: dict[str, ValidatedHeading] = {}
    for position, (segment_start, segment) in enumerate(segments):
        segment_end = segments[position + 1][0] if position + 1 < len(segments) else candidates[-1].item_index + 1
        segment_candidates = [candidate for candidate in candidates if segment_start <= candidate.item_index < segment_end]
        _apply_segment(
            candidates,
            segment_candidates,
            segment,
            segment_start,
            segment_end,
            max_levels,
            result_by_anchor,
            errors,
        )

    result = sorted(result_by_anchor.values(), key=lambda value: value.candidate.item_index)
    selected = set(result_by_anchor)
    missing_required = [candidate.anchor for candidate in candidates if candidate.required and candidate.anchor not in selected]
    if missing_required:
        errors.append(f"规则应用后遗漏required候选:{','.join(missing_required[:12])}")
    errors.extend(_hierarchy_errors(result, max_levels))
    return result, errors


def _apply_segment(
    all_candidates: list[AttachmentCandidate],
    segment_candidates: list[AttachmentCandidate],
    segment: AttachmentHierarchySegment,
    segment_start: int,
    segment_end: int,
    max_levels: int,
    result_by_anchor: dict[str, ValidatedHeading],
    errors: list[str],
) -> None:
    rules: dict[str, int] = {}
    for rule in segment.level_rules:
        if rule.number_family in rules:
            errors.append(f"segment编号族重复:{segment.start_anchor}:{rule.number_family}")
        rules[rule.number_family] = rule.level
        if rule.level > max_levels:
            errors.append(f"层级超过{max_levels}:{segment.start_anchor}:{rule.number_family}")
    missing_families = sorted(
        {
            candidate.number_family
            for candidate in segment_candidates
            if candidate.required and candidate.number_family not in rules
        }
    )
    if missing_families:
        errors.append(f"segment遗漏required编号族:{segment.start_anchor}:{','.join(missing_families)}")

    if segment.document_title is not None:
        resolved = _resolve_heading(all_candidates, segment.document_title)
        if resolved is None:
            errors.append(f"内部文档标题不是原文:{segment.document_title.anchor}")
        elif resolved.number_family != "unnumbered":
            errors.append(f"document_title必须是无编号标题:{resolved.anchor}")
        elif segment.document_title.level != 1:
            errors.append(f"document_title必须是level 1:{resolved.anchor}")
        elif not (segment_start <= resolved.item_index < segment_end):
            errors.append(f"内部文档标题不在segment内:{resolved.anchor}")
        else:
            result_by_anchor[resolved.anchor] = ValidatedHeading(resolved, segment.document_title.level)

    for candidate in segment_candidates:
        level = rules.get(candidate.number_family)
        if level is not None:
            result_by_anchor[candidate.anchor] = ValidatedHeading(candidate, level)

    for heading in segment.additional_headings:
        resolved = _resolve_heading(all_candidates, heading)
        if resolved is None:
            errors.append(f"附加标题不是原文:{heading.anchor}")
        elif not (segment_start <= resolved.item_index < segment_end):
            errors.append(f"附加标题不在segment内:{resolved.anchor}")
        else:
            result_by_anchor[resolved.anchor] = ValidatedHeading(resolved, heading.level)


def _resolve_heading(candidates: list[AttachmentCandidate], heading: AttachmentHeading) -> AttachmentCandidate | None:
    by_anchor = {candidate.anchor: candidate for candidate in candidates}
    candidate = by_anchor.get(heading.anchor)
    if candidate is not None and _same_title(candidate.text, heading.title):
        return candidate
    matches = [candidate for candidate in candidates if _same_title(candidate.text, heading.title)]
    return matches[0] if len(matches) == 1 else None


def _same_title(source: str, returned: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r"\s+", "", value).rstrip(".。")
    source_value = normalize(source)
    returned_value = normalize(returned)
    return source_value == returned_value or (len(returned_value) >= 12 and source_value.startswith(returned_value))


def _hierarchy_errors(headings: list[ValidatedHeading], max_levels: int) -> list[str]:
    errors: list[str] = []
    previous_level: int | None = None
    for position, heading in enumerate(headings):
        if heading.level < 1 or heading.level > max_levels:
            errors.append(f"标题层级越界:{heading.candidate.anchor}:{heading.level}")
        if position == 0 and heading.level != 1:
            errors.append("第一个标题必须从level 1开始")
        elif previous_level is not None and heading.level > previous_level + 1:
            errors.append(f"层级发生跳跃:{heading.candidate.anchor}")
        previous_level = heading.level
    return errors


def _build_hierarchy_nodes(
    parent: DocumentNode,
    items: list[BodyItem],
    headings: list[ValidatedHeading],
    *,
    inside_attachment: bool,
) -> list[DocumentNode]:
    roots: list[DocumentNode] = []
    stack: list[tuple[int, DocumentNode]] = []
    sibling_counts: dict[tuple[str, int], int] = {}
    for position, heading in enumerate(headings):
        index = heading.candidate.item_index
        level = heading.level
        next_index = parent.source_end or len(items)
        for future in headings[position + 1 :]:
            if future.level <= level:
                next_index = future.candidate.item_index
                break
        while stack and stack[-1][0] >= level:
            stack.pop()
        local_parent = stack[-1][1] if stack else parent
        key = (local_parent.node_id, level)
        sibling_counts[key] = sibling_counts.get(key, 0) + 1
        node_id = f"{local_parent.node_id}/h{level}_{sibling_counts[key]:03d}"
        score, evidence = heading_score(items[index], level, inside_attachment=inside_attachment)
        evidence.extend(
            [
                "llm_attachment_hierarchy" if inside_attachment else "llm_body_hierarchy",
                f"number_family:{heading.candidate.number_family}",
            ]
        )
        child = make_node(
            node_id,
            "attachment_heading" if inside_attachment else "semantic_section",
            heading.candidate.text,
            items,
            index,
            next_index,
            level,
            local_parent.node_id,
            score,
            evidence,
        )
        if stack:
            stack[-1][1].children.append(child)
        else:
            roots.append(child)
        stack.append((level, child))
    return roots
