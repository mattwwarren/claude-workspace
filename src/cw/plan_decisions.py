"""Parse an approved plan's decided sections into row entries (GitHub #2984).

The plan stage records three sections of ``.cw/plan.md`` on the dev-queue row
as decided constraints for the review lane (``cw dev-queue
record-plan-decisions``):

- ``## Adopted Assumptions`` -> ``adopted_assumption``
- ``## Non-Goals`` -> ``non_goal``
- ``## Decisions`` -> ``plan_decision``

Each top-level list item of a section is one entry: its marker is stripped
and its indented continuation lines (nested bullets included) are kept
verbatim. A section with no list items is one entry. An inert body (``None.``,
``N/A ...``, ``NO_AMBIGUITIES``) yields nothing, as does a missing section.

Fence-aware like :func:`cw.plan_files.parse_plan_files_modified`: a fenced
illustration of a section is never read as one. The fence and indent helpers
are imported from :mod:`cw.plan_files` directly rather than re-implemented;
that module is mirrored by a stdlib-only gate script and is not edited here.
Only ``cw`` consumes this module, so it has no ``.claude/scripts`` mirror.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from cw.plan_files import _indent_width, _strip_fenced_lines

if TYPE_CHECKING:
    from cw.models.operator_constraint import PlanConstraintKind

# Normalized heading text -> kind. Normalization lowercases and drops
# everything but letters, so "Non-Goals", "Non Goals" and "non-goals:" agree.
_SECTION_KINDS: dict[str, PlanConstraintKind] = {
    "adoptedassumptions": "adopted_assumption",
    "nongoals": "non_goal",
    "decisions": "plan_decision",
}
# A level-1 or level-2 ATX heading; deeper headings stay inside a section.
_SECTION_HEADING = re.compile(r"^\s{0,3}(#{1,2})\s+(.*?)\s*#*\s*$")
_SUB_HEADING = re.compile(r"^\s{0,3}#{3,6}\s")
_NON_LETTERS = re.compile(r"[^a-z]")
# A list-item marker: `-`, `*`, `+`, or an ordered `1.` / `1)`.
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])(?:\s+|$)")
_INERT = re.compile(r"^(?:none\.?|n/a\b.*|no_ambiguities)$", re.IGNORECASE)


class PlanDecision(NamedTuple):
    """One decided item: its kind and verbatim text."""

    kind: PlanConstraintKind
    text: str


def parse_plan_decisions(plan_text: str) -> list[PlanDecision]:
    """The plan's decided items in document order (see the module docstring)."""
    decisions: list[PlanDecision] = []
    kind: PlanConstraintKind | None = None
    body: list[str] = []
    for line in _strip_fenced_lines(plan_text.splitlines()):
        heading = _SECTION_HEADING.match(line)
        if heading is None:
            body.append(line)
            continue
        if kind is not None:
            decisions.extend(PlanDecision(kind, text) for text in _split_items(body))
        kind = _SECTION_KINDS.get(_NON_LETTERS.sub("", heading.group(2).lower()))
        body = []
    if kind is not None:
        decisions.extend(PlanDecision(kind, text) for text in _split_items(body))
    return decisions


def _split_items(body: list[str]) -> list[str]:
    """One text per top-level list item or sub-heading of *body*.

    Top-level means the minimum list-marker indent in the section. A deeper
    (``###``+) heading starts its own item, heading line included, so a
    section written as headed paragraphs keeps one entry per heading. Lines
    before the first item form an item of their own.
    """
    markers = [_indent_width(line) for line in body if _LIST_MARKER.match(line)]
    top = min(markers) if markers else None
    items: list[list[str]] = [[]]
    for line in body:
        if _SUB_HEADING.match(line):
            items.append([line])
        elif top is not None and _is_top_marker(line, top):
            items.append([_LIST_MARKER.sub("", line, count=1)])
        else:
            items[-1].append(line)
    return _kept(items)


def _is_top_marker(line: str, top: int) -> bool:
    return bool(_LIST_MARKER.match(line)) and _indent_width(line) == top


def _kept(items: list[list[str]]) -> list[str]:
    """Join each item's lines, dropping blank and inert items."""
    texts = ("\n".join(lines).strip() for lines in items)
    return [text for text in texts if text and not _is_inert(text)]


def _is_inert(text: str) -> bool:
    """Whether *text* is a placeholder body that records nothing."""
    return bool(_INERT.match(text.strip("*_` ")))
