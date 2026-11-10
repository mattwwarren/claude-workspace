"""Shared markdown fence helpers: the fence widener and the line classifier.

``fence_for`` sizes a backtick fence from the body it wraps: a fenced code
block breaks if its body holds a backtick run as long as the fence, so each
renderer that fences text it does not control sizes the fence from the body.

``classify_lines`` labels each line of a markdown document as ``text``,
``fence`` (inside a fenced code block, opener and closer included) or
``table`` (a ``|`` row outside any fence). It tracks the fence character and
length, so a nested longer fence holds a shorter inner one. Leaf module:
imports nothing from :mod:`cw`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["LineKind", "classify_lines", "fence_for"]

_MIN_FENCE_LENGTH = 3
_BACKTICK_RUN = re.compile(r"`+")
_FENCE_OPENER = re.compile(r"(`{3,}|~{3,})(.*)$")
_TABLE_LEAD = "|"

LineKind = Literal["text", "fence", "table"]


def fence_for(text: str) -> str:
    """A backtick fence one longer than the longest run in *text*, at least 3."""
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    return "`" * max(_MIN_FENCE_LENGTH, longest + 1)


def _is_closer(stripped: str, opener: str) -> bool:
    """Whether *stripped* closes a fence opened by *opener*.

    A closer uses the opener's character, is at least as long, and carries no
    info string.
    """
    match = _FENCE_OPENER.match(stripped)
    if match is None:
        return False
    run, rest = match.group(1), match.group(2)
    return run[0] == opener[0] and len(run) >= len(opener) and not rest.strip()


def classify_lines(lines: Sequence[str]) -> list[LineKind]:
    """Label every line of *lines* as ``text``, ``fence`` or ``table``.

    An opener is a line whose ``lstrip()`` starts with three or more backticks
    or tildes, at any indent and with an optional info string. An unterminated
    fence swallows the rest of the document.
    """
    kinds: list[LineKind] = []
    opener: str | None = None
    for line in lines:
        stripped = line.strip()
        if opener is not None:
            kinds.append("fence")
            if _is_closer(stripped, opener):
                opener = None
            continue
        match = _FENCE_OPENER.match(stripped)
        if match is not None:
            opener = match.group(1)
            kinds.append("fence")
        elif stripped.startswith(_TABLE_LEAD):
            kinds.append("table")
        else:
            kinds.append("text")
    return kinds
