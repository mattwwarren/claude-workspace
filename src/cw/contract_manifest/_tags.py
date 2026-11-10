"""The ``<!-- el: <id> -->`` tag grammar, the doc scanner and the tag strip.

A tag is a whole column-0 line matching :data:`STRICT_TAG`. A *tag-shaped*
line is anything matching :data:`TAG_SHAPED`; one that is not strict is
``tag_malformed``. Tags never sit inside a fence or a table, and the
``el-begin`` / ``el-end`` range forms are reserved for RFC 0018 A3 (#3058).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cw.contract_manifest._models import ContractViolation, DocScan, DocTag
from cw.markdown_fence import classify_lines

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cw.contract_manifest._models import ViolationKind
    from cw.markdown_fence import LineKind

__all__ = [
    "DOC_SCOPES",
    "ELEMENT_ID",
    "SCOPES",
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
TAG_SHAPED = re.compile(r"^\s*<!--\s*el(-begin|-end)?\s*:")

_HEADING = re.compile(r"#{1,6} ")
_LIST_ITEM = re.compile(r"(?:[-*] |\d+(?:\.\d+)*\. )")
_BOLD_LEAD = re.compile(r"\s*(?:(?:[-*] |\d+(?:\.\d+)*\. ))?\*\*")
_BOLD = "**"


def strip_doc_tags(text: str) -> str:
    """Drop every whole strict-tag line of *text*, newline included."""
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if STRICT_TAG.match(line.rstrip("\r\n")) is None
    )


def _is_block_start(line: str) -> bool:
    return bool(
        _HEADING.match(line) or _LIST_ITEM.match(line) or line.startswith(_BOLD)
    )


def _is_prose(line: str, kind: LineKind) -> bool:
    """A non-blank column-0 text line that is not a heading or a list item."""
    if kind != "text" or not line.strip() or line[0].isspace():
        return False
    return not (_HEADING.match(line) or _LIST_ITEM.match(line))


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
    above_is_prose = index > 0 and _is_prose(lines[index - 1], kinds[index - 1])
    if (
        above_is_prose
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
    """The fence, table or reserved-range violation of a tag-shaped line.

    Applies whatever the line's strictness; a non-strict line with none of
    these is ``tag_malformed`` (decided by the caller). A tag-shaped line is
    never itself a ``|`` row, so "in a table" means between two rows.
    """
    if kinds[index] == "fence":
        return "tag_in_fence"
    if _between_table_rows(kinds, index):
        return "tag_in_table"
    if shaped.group(1):
        return "tag_reserved_range"
    return None


class _Scanner:
    """Accumulates tags and violations across every doc of one scan."""

    def __init__(self) -> None:
        self.tags: list[DocTag] = []
        self.violations: list[ContractViolation] = []
        self._first_seen: dict[str, DocTag] = {}

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
        for index, line in enumerate(lines):
            shaped = TAG_SHAPED.match(line)
            if shaped is None:
                continue
            number = index + 1
            shape = _shape_kind(shaped, kinds, index)
            strict = STRICT_TAG.match(line)
            if shape is not None or strict is None:
                self._flag(
                    shape or "tag_malformed",
                    doc,
                    number,
                    f"tag-shaped line {line.strip()!r}",
                )
                continue
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
    return DocScan(tags=tuple(scanner.tags), violations=tuple(scanner.violations))
