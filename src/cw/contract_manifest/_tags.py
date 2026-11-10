"""The ``<!-- el: <id> -->`` tag grammar, the doc scanner and the tag strip.

A tag is a whole column-0 line matching :data:`STRICT_TAG`. A *tag-shaped*
line is anything matching :data:`TAG_SHAPED`; one that is not strict is
``tag_malformed``. Tags never sit inside a fence or a table.

An element body may be bracketed by a range (RFC 0018 A3, #3058): a
``<!-- el-begin: <id> -->`` line directly above the element's ``el:`` tag, and
a ``<!-- el-end: <id> -->`` line after a blank line, directly before the next
tag. Both are whole column-0 lines matching :data:`STRICT_RANGE_TAG`. A
``el-end`` line is exempt from the anchor rule; a range never sits in a fence
or table, never nests, and its begin and end carry the id of the tag between
them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cw.contract_manifest._models import (
    ContractViolation,
    DocRange,
    DocScan,
    DocTag,
)
from cw.markdown_fence import classify_lines

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cw.contract_manifest._models import ViolationKind
    from cw.markdown_fence import LineKind

    #: A violation reporter: ``(kind, line, detail, element_id)``.
    type _Flag = Callable[[ViolationKind, int, str, str | None], None]

__all__ = [
    "DOC_SCOPES",
    "ELEMENT_ID",
    "SCOPES",
    "STRICT_RANGE_TAG",
    "STRICT_TAG",
    "TAG_SHAPED",
    "scan_doc_tags",
    "strip_doc_tags",
]

#: The eight element scopes. The scope is the stage, not the file, so a core
#: doc and its appendix share one.
SCOPES: tuple[str, ...] = (
    "orch",
    "intake",
    "plan",
    "impl",
    "review",
    "finalize",
    "prep-pr",
    "ship-it",
)

#: The thirteen in-scope command docs and the scope their tags must carry.
DOC_SCOPES: dict[str, str] = {
    "auto-dev.md": "orch",
    "auto-dev-intake.md": "intake",
    "auto-dev-intake-appendix.md": "intake",
    "auto-dev-plan.md": "plan",
    "auto-dev-plan-appendix.md": "plan",
    "auto-dev-impl.md": "impl",
    "auto-dev-impl-appendix.md": "impl",
    "auto-dev-review.md": "review",
    "auto-dev-review-appendix.md": "review",
    "auto-dev-finalize.md": "finalize",
    "auto-dev-finalize-appendix.md": "finalize",
    "prep-pr.md": "prep-pr",
    "ship-it.md": "ship-it",
}

_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
#: ``<scope>.<kebab-slug>``; the scope must also be one of :data:`SCOPES`.
ELEMENT_ID = re.compile(rf"{_SLUG}\.{_SLUG}")
STRICT_TAG = re.compile(rf"^<!-- el: ({_SLUG}\.{_SLUG}) -->$")
#: ``<!-- el-begin: <id> -->`` or ``<!-- el-end: <id> -->``; group 1 is the
#: form (``begin`` / ``end``), group 2 the id.
STRICT_RANGE_TAG = re.compile(rf"^<!-- el-(begin|end): ({_SLUG}\.{_SLUG}) -->$")
TAG_SHAPED = re.compile(r"^\s*<!--\s*el(-begin|-end)?\s*:")

_HEADING = re.compile(r"#{1,6} ")
_LIST_ITEM = re.compile(r"(?:[-*] |\d+(?:\.\d+)*\. )")
_BOLD_LEAD = re.compile(r"\s*(?:(?:[-*] |\d+(?:\.\d+)*\. ))?\*\*")
_BOLD = "**"


def _is_strict_tag(line: str) -> bool:
    """Whether *line* (newline already removed) is a strict ``el`` or range tag."""
    return bool(STRICT_TAG.match(line) or STRICT_RANGE_TAG.match(line))


def strip_doc_tags(text: str) -> str:
    """Drop every whole strict-tag line of *text*, newline included.

    Both the ``el:`` tag and the strict ``el-begin`` / ``el-end`` range forms
    are dropped; a non-strict tag-shaped line is kept.
    """
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not _is_strict_tag(line.rstrip("\r\n"))
    )


def _is_block_start(line: str) -> bool:
    return bool(
        _HEADING.match(line) or _LIST_ITEM.match(line) or line.startswith(_BOLD)
    )


def _is_prose(line: str, kind: LineKind) -> bool:
    """A non-blank column-0 text line that is not a heading, list item or tag."""
    if kind != "text" or not line.strip() or line[0].isspace():
        return False
    return not (
        _HEADING.match(line) or _LIST_ITEM.match(line) or TAG_SHAPED.match(line)
    )


def _above_is_prose(
    lines: Sequence[str], kinds: Sequence[LineKind], index: int
) -> bool:
    """Whether the line above the tag at *index* is prose.

    An ``el-begin`` line directly above is stepped over: it belongs to the tag.
    """
    above = index - 1
    if above >= 0:
        begin = STRICT_RANGE_TAG.match(lines[above])
        if begin is not None and begin.group(1) == "begin":
            above -= 1
    return above >= 0 and _is_prose(lines[above], kinds[above])


def _placement_kind(
    lines: Sequence[str], kinds: Sequence[LineKind], index: int
) -> ViolationKind | None:
    """The one placement violation of the tag at *index*, or ``None``.

    Mid-paragraph is checked first: prose above and a non-block-start line
    below means the tag would join the paragraph. Otherwise the anchor (the
    next line) must be a heading or a bold-lead, optionally indented and
    optionally after a list marker.
    """
    anchor = lines[index + 1] if index + 1 < len(lines) else None
    if (
        _above_is_prose(lines, kinds, index)
        and anchor is not None
        and anchor.strip()
        and not _is_block_start(anchor)
    ):
        return "tag_mid_paragraph"
    if anchor is None or not (_HEADING.match(anchor) or _BOLD_LEAD.match(anchor)):
        return "tag_bad_anchor"
    return None


def _between_table_rows(kinds: Sequence[LineKind], index: int) -> bool:
    """Whether line *index* sits between two table rows (inside a table)."""
    return (
        0 < index < len(kinds) - 1
        and kinds[index - 1] == "table"
        and kinds[index + 1] == "table"
    )


def _shape_kind(
    shaped: re.Match[str], kinds: Sequence[LineKind], index: int
) -> ViolationKind | None:
    """The fence or table violation of a tag-shaped line.

    Applies whatever the line's strictness; a non-strict line with none of
    these is ``tag_malformed`` (decided by the caller). A tag-shaped line is
    never itself a ``|`` row, so "in a table" means between two rows.
    """
    is_range = bool(shaped.group(1))
    if kinds[index] == "fence":
        return "range_in_fence" if is_range else "tag_in_fence"
    if _between_table_rows(kinds, index):
        return "range_in_fence" if is_range else "tag_in_table"
    return None


class _RangeTracker:
    """Pairs ``el-begin`` / ``el-end`` lines of one doc and checks their shape.

    The open range is ``(id, begin line, valid)``; ``valid`` is false once the
    begin was flagged, so its end and the tag below add no cascade violation.
    *ranged* is shared across docs so a second range for one id is a duplicate.
    """

    def __init__(
        self,
        doc: str,
        flag: _Flag,
        ranges: list[DocRange],
        ranged: dict[str, DocRange],
    ) -> None:
        self._doc = doc
        self._flag = flag
        self._ranges = ranges
        self._ranged = ranged
        self._open: tuple[str, int, bool] | None = None

    def begin(self, number: int, element_id: str, lines: Sequence[str]) -> None:
        if self._open is not None:
            detail = f"range for {self._open[0]!r} is still open"
            self._flag("range_nested", number, detail, element_id)
            return
        below = lines[number] if number < len(lines) else ""
        tag = STRICT_TAG.match(below)
        valid = tag is not None and tag.group(1) == element_id
        if not valid:
            detail = f"el-begin must sit directly above `el: {element_id}`"
            self._flag("range_id_mismatch", number, detail, element_id)
        self._open = (element_id, number, valid)

    def end(self, number: int, element_id: str) -> None:
        if self._open is None:
            self._flag("range_unpaired", number, "el-end without el-begin", element_id)
            return
        open_id, begin_line, valid = self._open
        self._open = None
        if open_id != element_id:
            detail = f"el-end id differs from el-begin {open_id!r}"
            self._flag("range_id_mismatch", number, detail, element_id)
            return
        if valid:
            self._record(element_id, begin_line, number)

    def _record(self, element_id: str, begin_line: int, end_line: int) -> None:
        first = self._ranged.get(element_id)
        if first is not None:
            detail = f"range already defined at {first.doc}:{first.begin_line}"
            self._flag("tag_duplicate", end_line, detail, element_id)
            return
        found = DocRange(
            element_id=element_id,
            doc=self._doc,
            begin_line=begin_line,
            end_line=end_line,
        )
        self._ranged[element_id] = found
        self._ranges.append(found)

    def element_tag(self, number: int, element_id: str) -> None:
        """Flag an ``el:`` tag of another id inside a valid open range."""
        if self._open is None:
            return
        open_id, _begin, valid = self._open
        if valid and open_id != element_id:
            detail = f"tag inside the open range of {open_id!r}"
            self._flag("range_nested", number, detail, element_id)

    def finish(self) -> None:
        """Flag a range still open at the end of the doc."""
        if self._open is not None:
            open_id, begin_line, _valid = self._open
            detail = "el-begin without el-end"
            self._flag("range_unpaired", begin_line, detail, open_id)


class _Scanner:
    """Accumulates tags and violations across every doc of one scan."""

    def __init__(self) -> None:
        self.tags: list[DocTag] = []
        self.violations: list[ContractViolation] = []
        self.ranges: list[DocRange] = []
        self._first_seen: dict[str, DocTag] = {}
        self._ranged: dict[str, DocRange] = {}

    def _flag(
        self,
        kind: ViolationKind,
        doc: str,
        line: int,
        detail: str,
        element_id: str | None = None,
    ) -> None:
        self.violations.append(
            ContractViolation(
                kind=kind, element_id=element_id, doc=doc, line=line, detail=detail
            )
        )

    def scan(self, doc: str, text: str) -> None:
        lines = text.splitlines()
        kinds = classify_lines(lines)

        def flag(
            kind: ViolationKind, line: int, detail: str, element_id: str | None
        ) -> None:
            self._flag(kind, doc, line, detail, element_id)

        tracker = _RangeTracker(doc, flag, self.ranges, self._ranged)
        for index, line in enumerate(lines):
            shaped = TAG_SHAPED.match(line)
            if shaped is not None:
                self._scan_shaped(doc, index, shaped, lines, kinds, tracker)
        tracker.finish()

    def _scan_shaped(
        self,
        doc: str,
        index: int,
        shaped: re.Match[str],
        lines: Sequence[str],
        kinds: Sequence[LineKind],
        tracker: _RangeTracker,
    ) -> None:
        line = lines[index]
        number = index + 1
        strict = STRICT_TAG.match(line)
        range_tag = STRICT_RANGE_TAG.match(line)
        shape = _shape_kind(shaped, kinds, index)
        if shape is not None or (strict is None and range_tag is None):
            self._flag(
                shape or "tag_malformed",
                doc,
                number,
                f"tag-shaped line {line.strip()!r}",
            )
        elif range_tag is not None and range_tag.group(1) == "begin":
            tracker.begin(number, range_tag.group(2), lines)
        elif range_tag is not None:
            tracker.end(number, range_tag.group(2))
        elif strict is not None:
            tracker.element_tag(number, strict.group(1))
            self._take(doc, number, strict.group(1), lines, kinds)

    def _take(
        self,
        doc: str,
        number: int,
        element_id: str,
        lines: Sequence[str],
        kinds: Sequence[LineKind],
    ) -> None:
        scope = element_id.split(".", 1)[0]
        if scope not in SCOPES:
            self._flag(
                "unknown_scope",
                doc,
                number,
                f"scope {scope!r} is not one of {SCOPES}",
                element_id,
            )
            return
        expected = DOC_SCOPES.get(doc)
        if expected is not None and scope != expected:
            self._flag(
                "tag_wrong_scope",
                doc,
                number,
                f"{doc} tags carry scope {expected!r}, not {scope!r}",
                element_id,
            )
        first = self._first_seen.get(element_id)
        if first is not None:
            self._flag(
                "tag_duplicate",
                doc,
                number,
                f"already defined at {first.doc}:{first.line}",
                element_id,
            )
            return
        placement = _placement_kind(lines, kinds, number - 1)
        if placement is not None:
            self._flag(placement, doc, number, "tag placement", element_id)
        tag = DocTag(element_id=element_id, scope=scope, doc=doc, line=number)
        self._first_seen[element_id] = tag
        self.tags.append(tag)


def scan_doc_tags(docs: Mapping[str, str]) -> DocScan:
    """Scan every doc in *docs* (name to raw text) for tags and tag errors.

    Ids are global: a second tag for one id, in the same doc or another, is
    ``tag_duplicate``. A doc missing from :data:`DOC_SCOPES` carries no scope
    constraint.
    """
    scanner = _Scanner()
    for doc, text in docs.items():
        scanner.scan(doc, text)
    return DocScan(
        tags=tuple(scanner.tags),
        violations=tuple(scanner.violations),
        ranges=tuple(scanner.ranges),
    )
