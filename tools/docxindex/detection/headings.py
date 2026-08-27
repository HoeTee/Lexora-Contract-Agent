from __future__ import annotations

from tools.docxindex.schema import BodyItem

from .heading_profiles import CompiledHeadingProfile
from .patterns import (
    ATTACHMENT_RE,
    LEVEL2_RE,
    LEVEL3_RE,
    MAIN_SECTION_RE,
)


def is_level1(item: BodyItem, profile: CompiledHeadingProfile | None = None) -> bool:
    if item.kind != "p":
        return False
    if profile is not None:
        return profile.level_for(item.text) == 1
    return bool(MAIN_SECTION_RE.match(item.text))


def heading_level(item: BodyItem, profile: CompiledHeadingProfile | None = None) -> int | None:
    if item.kind != "p" or not item.text:
        return None
    if profile is not None:
        return profile.level_for(item.text)
    if is_level1(item):
        return 1
    if LEVEL2_RE.match(item.text):
        return 2
    if LEVEL3_RE.match(item.text):
        return 3
    return None


def is_title_excluded(item: BodyItem) -> bool:
    return bool(heading_level(item) or ATTACHMENT_RE.match(item.text))
