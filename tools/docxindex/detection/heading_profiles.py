from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from tools.docxindex.schema import BodyItem

from .number_patterns import compile_example_patterns


DEFAULT_PROFILE_EXAMPLES = {
    "primary": {
        "level_1_examples": ["第一条 合同内容"],
        "level_2_examples": ["一、合同总价"],
        "level_3_examples": ["（一）付款条件"],
    },
    "variant_1": {
        "level_1_examples": ["一、合作背景"],
        "level_2_examples": ["（一）通信业务合作", "（3）新技术与新业务合作"],
        "level_3_examples": ["1. 基础服务", "2、增值服务"],
    },
    "chapter_decimal": {
        "level_1_examples": ["第一章、合同内容"],
        "level_2_examples": ["1.1 供货范围"],
        "level_3_examples": ["（1）付款条件", "1）其他要求"],
    },
}


@dataclass(frozen=True)
class CompiledHeadingProfile:
    name: str
    level_patterns: dict[int, re.Pattern[str]]
    pattern_kinds: dict[int, tuple[str, ...]]

    def level_for(self, text: str) -> int | None:
        for level in (1, 2, 3):
            if self.level_patterns[level].match(text):
                return level
        return None


@dataclass(frozen=True)
class HeadingProfileMatch:
    profile: CompiledHeadingProfile
    score: int
    level_counts: dict[int, int]
    orphan_count: int


def compile_heading_profile(
    name: str,
    level_1_examples: list[str],
    level_2_examples: list[str],
    level_3_examples: list[str],
) -> CompiledHeadingProfile:
    patterns = {}
    kinds = {}
    for level, examples in {
        1: level_1_examples,
        2: level_2_examples,
        3: level_3_examples,
    }.items():
        patterns[level], kinds[level] = compile_example_patterns(examples)

    for expected_level, examples in {
        1: level_1_examples,
        2: level_2_examples,
        3: level_3_examples,
    }.items():
        for example in examples:
            matched = [level for level, pattern in patterns.items() if pattern.match(example.strip())]
            if matched != [expected_level]:
                raise ValueError(
                    f"heading profile {name!r} has ambiguous example {example!r}: matched levels {matched}"
                )
    return CompiledHeadingProfile(name=name, level_patterns=patterns, pattern_kinds=kinds)


def default_heading_profiles() -> tuple[CompiledHeadingProfile, ...]:
    return tuple(compile_heading_profile(name, **examples) for name, examples in DEFAULT_PROFILE_EXAMPLES.items())


def select_heading_profile(
    items: Iterable[BodyItem],
    profiles: tuple[CompiledHeadingProfile, ...],
) -> HeadingProfileMatch:
    if not profiles:
        raise ValueError("at least one heading profile is required")
    materialized_items = tuple(items)
    matches = [_score_profile(materialized_items, profile) for profile in profiles]
    for match in matches:
        if match.level_counts[1] >= 2:
            return match
    return max(matches, key=lambda item: item.score)


def _score_profile(items: Iterable[BodyItem], profile: CompiledHeadingProfile) -> HeadingProfileMatch:
    counts = {1: 0, 2: 0, 3: 0}
    valid_counts = {1: 0, 2: 0, 3: 0}
    orphan_count = 0
    has_level_1 = False
    has_level_2 = False
    for item in items:
        if item.kind != "p" or not item.text:
            continue
        level = profile.level_for(item.text)
        if level is None:
            continue
        counts[level] += 1
        if level == 1:
            valid_counts[1] += 1
            has_level_1 = True
            has_level_2 = False
        elif level == 2 and has_level_1:
            valid_counts[2] += 1
            has_level_2 = True
        elif level == 3 and has_level_1 and has_level_2:
            valid_counts[3] += 1
        else:
            orphan_count += 1
    score = valid_counts[1] * 9 + valid_counts[2] * 4 + valid_counts[3] * 2 - orphan_count * 8
    return HeadingProfileMatch(profile=profile, score=score, level_counts=counts, orphan_count=orphan_count)
