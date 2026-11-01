"""Rebuild a branch's CHANGELOG.md onto main's, never by textual merge (#2771).

``.gitattributes`` puts ``CHANGELOG.md`` on git's ``union`` merge driver, so a
local rebase of a pipeline branch never conflicts on it: it keeps both sides'
lines. After a release has renamed ``## [Unreleased]`` on main, that union
duplicates the released bullets under the new ``[Unreleased]`` and can file
the branch's own bullets under the released version. GitHub ignores merge
drivers, so the PR still reports ``CONFLICTING``.

:func:`rebuild_changelog` replaces that merge with a structural one: the
result is main's text verbatim plus the branch's own ``[Unreleased]`` bullets
(the tip's items minus the merge-base's), each appended to the matching
``### Subsection`` of main's ``[Unreleased]``. Anything it cannot place
unambiguously raises :class:`ChangelogRebuildError` with a
:class:`ChangelogRepairDisposition`, so a caller can park instead of pushing a
guess.

Heading and section semantics mirror ``.claude/scripts/check_changelog_frozen.py``
(``parse_headings`` / ``section_text``). That script is excluded from the
``cw`` wheel, so it cannot be imported here; ``tests/test_changelog_rebuild.py``
holds a parity test against it instead, the ``plan_files.py`` precedent.

Stdlib plus :class:`cw.exceptions.CwError` only: pure, no I/O.
"""

from __future__ import annotations

import re
from collections import Counter
from enum import StrEnum
from typing import NamedTuple

from cw.exceptions import CwError

_HEADING_RE = re.compile(r"^## \[(?P<ver>[^\]]+)\]")
_SECTION_BOUNDARY = "## "
_SUBSECTION_PREFIX = "### "
_BULLET_PREFIX = "- "
_UNRELEASED = "Unreleased"
_CRLF = "\r\n"
_LF = "\n"
_CR = "\r"

# Keep-a-Changelog subsection order. A subsection the branch adds that main's
# [Unreleased] lacks is created once, before the first existing subsection of
# greater rank; names outside this list rank after every known one.
_SUBSECTION_ORDER: tuple[str, ...] = (
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
)


class ChangelogRepairDisposition(StrEnum):
    """Every named outcome of the ``repair_changelog_conflict`` recipe.

    The single source of truth for the disposition vocabulary carried on
    ``pr.action_failed`` (and, for ``unarmed_review_fence``, on the
    ``session.needs_attention`` page). ``docs/events.md`` and
    ``docs/dispatch-runbook.md`` list every value; a doc-drift test pins that.
    """

    NON_CHANGELOG_CONFLICT = "non_changelog_conflict"
    CONFLICT_UNEXPLAINED = "conflict_unexplained"
    CHANGELOG_UNPARSEABLE = "changelog_unparseable"
    CHANGELOG_RELEASED_SECTION_TOUCHED = "changelog_released_section_touched"
    CHANGELOG_AMBIGUOUS = "changelog_ambiguous"
    REBASE_FAILED = "rebase_failed"
    VERIFY_FAILED = "verify_failed"
    GATE_FAILED = "gate_failed"
    PUSH_REJECTED = "push_rejected"
    PUSH_OUTCOME_UNKNOWN = "push_outcome_unknown"
    REARM_FAILED = "rearm_failed"
    ARM_SCRIPT_MISSING = "arm_script_missing"
    PR_HEAD_MISMATCH = "pr_head_mismatch"
    FETCH_FAILED = "fetch_failed"
    SCRATCH_WORKTREE_FAILED = "scratch_worktree_failed"
    DISARM_FAILED = "disarm_failed"
    POST_PUSH_FAILED = "post_push_failed"
    UNARMED_REVIEW_FENCE = "unarmed_review_fence"


class ChangelogRebuildError(CwError):
    """The CHANGELOG could not be rebuilt unambiguously; nothing was produced."""

    __slots__ = ("detail", "disposition")

    def __init__(self, disposition: ChangelogRepairDisposition, detail: str) -> None:
        super().__init__(f"{disposition}: {detail}")
        self.disposition = disposition
        self.detail = detail


class Heading(NamedTuple):
    """One ``## [<version>]`` heading and its 0-based line index."""

    version: str
    line: int


class OwnItem(NamedTuple):
    """One bullet item: its ``###`` subsection and verbatim text."""

    subsection: str
    text: str

    @property
    def normalized(self) -> str:
        """The text with all whitespace runs collapsed (the identity key)."""
        return " ".join(self.text.split())

    @property
    def key(self) -> tuple[str, str]:
        return self.subsection, self.normalized


class RebuildResult(NamedTuple):
    """The rebuilt text plus the branch items inserted and already present."""

    text: str
    inserted: tuple[OwnItem, ...]
    skipped_present: tuple[OwnItem, ...]


def parse_headings(lines: list[str]) -> list[Heading]:
    """Every ``## [...]`` heading in *lines* (mirrors check_changelog_frozen)."""
    headings: list[Heading] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is not None:
            headings.append(Heading(version=match["ver"], line=index))
    return headings


def _section_end(lines: list[str], start: int) -> int:
    end = start + 1
    while end < len(lines) and not lines[end].startswith(_SECTION_BOUNDARY):
        end += 1
    return end


def section_text(lines: list[str], start: int) -> str:
    """The section at *start*, up to the next ``## `` line, minus trailing blanks."""
    body = lines[start : _section_end(lines, start)]
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def _fail(
    disposition: ChangelogRepairDisposition, detail: str
) -> ChangelogRebuildError:
    return ChangelogRebuildError(disposition, detail)


class _Doc(NamedTuple):
    lines: list[str]
    newline: str
    trailing_newline: bool
    headings: list[Heading]
    unreleased_start: int
    unreleased_end: int


def _split_lines(text: str, label: str) -> tuple[list[str], str, bool]:
    newline = _CRLF if _CRLF in text else _LF
    bare_cr = text.count(_CR) != text.count(_CRLF)
    mixed = newline == _CRLF and text.count(_LF) != text.count(_CRLF)
    if bare_cr or mixed:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_UNPARSEABLE,
            f"{label} CHANGELOG.md has mixed line endings",
        )
    lines = text.split(newline)
    trailing = text.endswith(newline)
    if trailing:
        lines = lines[:-1]
    return lines, newline, trailing


def _parse_doc(text: str, label: str) -> _Doc:
    if not text.strip():
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_UNPARSEABLE,
            f"{label} CHANGELOG.md is missing or empty",
        )
    lines, newline, trailing = _split_lines(text, label)
    headings = parse_headings(lines)
    unreleased = [h for h in headings if h.version == _UNRELEASED]
    if len(unreleased) != 1:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_UNPARSEABLE,
            f"{label} CHANGELOG.md has {len(unreleased)} '## [Unreleased]'"
            " headings, expected exactly one",
        )
    start = unreleased[0].line
    return _Doc(
        lines=lines,
        newline=newline,
        trailing_newline=trailing,
        headings=headings,
        unreleased_start=start,
        unreleased_end=_section_end(lines, start),
    )


def _body(doc: _Doc) -> list[str]:
    return doc.lines[doc.unreleased_start + 1 : doc.unreleased_end]


def _outside(doc: _Doc) -> tuple[list[str], list[str]]:
    return doc.lines[: doc.unreleased_start], doc.lines[doc.unreleased_end :]


class _Block(NamedTuple):
    items: list[OwnItem]
    prose: list[str]


class _ItemParser:
    """Line-at-a-time bullet parser for one block of CHANGELOG lines.

    An item starts at a column-0 ``- `` and runs until the next column-0
    bullet, ``###``, ``##``, or a blank line followed by a non-indented line;
    continuation lines (and blanks before an indented continuation) are kept
    verbatim. Any other non-blank line is prose.
    """

    def __init__(self) -> None:
        self.subsection = ""
        self.items: list[OwnItem] = []
        self.prose: list[str] = []
        self._current: list[str] | None = None
        self._pending_blanks = 0

    def _close(self) -> None:
        if self._current is not None:
            self.items.append(OwnItem(self.subsection, "\n".join(self._current)))
        self._current = None
        self._pending_blanks = 0

    def feed(self, line: str) -> None:
        if line.startswith(_SUBSECTION_PREFIX):
            self._close()
            self.subsection = line.removeprefix(_SUBSECTION_PREFIX).strip()
        elif line.startswith(_SECTION_BOUNDARY):
            self._close()
            self.subsection = ""
        elif line.startswith(_BULLET_PREFIX):
            self._close()
            self._current = [line]
        elif not line.strip():
            if self._current is not None:
                self._pending_blanks += 1
        elif self._current is not None and line[0].isspace():
            self._current.extend([""] * self._pending_blanks)
            self._pending_blanks = 0
            self._current.append(line)
        else:
            self._close()
            self.prose.append(line.strip())

    def finish(self) -> _Block:
        self._close()
        return _Block(items=self.items, prose=self.prose)


def _parse_block(lines: list[str]) -> _Block:
    parser = _ItemParser()
    for line in lines:
        parser.feed(line)
    return parser.finish()


def _check_branch_shape(base: _Doc, tip: _Doc) -> None:
    """Fail closed on any branch edit outside its own [Unreleased] additions."""
    base_versions = [h.version for h in base.headings]
    tip_versions = [h.version for h in tip.headings]
    if tip_versions != base_versions:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS,
            f"branch changed the '## [..]' headings: {base_versions} -> {tip_versions}",
        )
    if _outside(tip) != _outside(base):
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_RELEASED_SECTION_TOUCHED,
            "branch edited the preamble or a released section",
        )


def _own_items(base: _Doc, tip: _Doc) -> list[OwnItem]:
    """The tip's [Unreleased] items absent from the merge-base's, in tip order."""
    base_block = _parse_block(_body(base))
    tip_block = _parse_block(_body(tip))
    added_prose = set(tip_block.prose) - set(base_block.prose)
    if added_prose:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_UNPARSEABLE,
            f"branch added non-bullet text under [Unreleased]: {sorted(added_prose)}",
        )
    if set(base_block.prose) - set(tip_block.prose):
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS,
            "branch removed or edited text under the merge-base's [Unreleased]",
        )
    tip_keys = {item.key for item in tip_block.items}
    missing = [item.text for item in base_block.items if item.key not in tip_keys]
    if missing:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS,
            f"branch removed or edited merge-base [Unreleased] bullet(s): {missing}",
        )
    base_keys = {item.key for item in base_block.items}
    own = [item for item in tip_block.items if item.key not in base_keys]
    counts = Counter(item.key for item in own)
    repeated = [key for key, n in counts.items() if n > 1]
    if repeated:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS,
            f"branch added the same bullet more than once: {repeated}",
        )
    loose = [item.text for item in own if not item.subsection]
    if loose:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS,
            f"branch bullet(s) outside any '### ' subsection: {loose}",
        )
    return own


def _rank(name: str) -> int:
    if name in _SUBSECTION_ORDER:
        return _SUBSECTION_ORDER.index(name)
    return len(_SUBSECTION_ORDER)


def _last_nonblank(body: list[str], start: int, end: int) -> int:
    """Index of the last non-blank line in ``body[start:end]``, else ``start - 1``."""
    for index in range(end - 1, start - 1, -1):
        if body[index].strip():
            return index
    return start - 1


def _insert_item(body: list[str], item: OwnItem) -> None:
    """Append *item* to its subsection of *body*, creating it once if absent."""
    item_lines = item.text.split("\n")
    subs = [
        (index, line.removeprefix(_SUBSECTION_PREFIX).strip())
        for index, line in enumerate(body)
        if line.startswith(_SUBSECTION_PREFIX)
    ]
    for position, (index, name) in enumerate(subs):
        if name == item.subsection:
            end = subs[position + 1][0] if position + 1 < len(subs) else len(body)
            last = _last_nonblank(body, index, end)
            body[last + 1 : last + 1] = ["", *item_lines]
            return
    rank = _rank(item.subsection)
    for index, name in subs:
        if _rank(name) > rank:
            body[index:index] = [
                f"{_SUBSECTION_PREFIX}{item.subsection}",
                "",
                *item_lines,
                "",
            ]
            return
    last = _last_nonblank(body, 0, len(body))
    body[last + 1 : last + 1] = [
        "",
        f"{_SUBSECTION_PREFIX}{item.subsection}",
        "",
        *item_lines,
    ]


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    remaining = iter(haystack)
    return all(line in remaining for line in needle)


def _check_postconditions(main: _Doc, result: _Doc, inserted: list[OwnItem]) -> None:
    """Self-check the rebuilt doc; any violation is ``changelog_ambiguous``."""
    problems: list[str] = []
    if _outside(result) != _outside(main):
        problems.append("preamble or a released section changed")
    main_lines = [line for line in main.lines if line.strip()]
    result_lines = [line for line in result.lines if line.strip()]
    if not _is_subsequence(main_lines, result_lines):
        problems.append("result is not additive over main")
    whole = Counter(item.normalized for item in _parse_block(result.lines).items)
    problems.extend(
        f"inserted bullet occurs {whole[item.normalized]} times: {item.text!r}"
        for item in inserted
        if whole[item.normalized] != 1
    )
    subs = [line for line in _body(result) if line.startswith(_SUBSECTION_PREFIX)]
    if len(subs) != len(set(subs)):
        problems.append(f"duplicate subsection under [Unreleased]: {subs}")
    versions = [h.version for h in result.headings]
    if len(versions) != len(set(versions)):
        problems.append(f"duplicate '## [..]' heading: {versions}")
    if problems:
        raise _fail(ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS, "; ".join(problems))


def _rebuild(base: str, branch: str, main: str) -> RebuildResult:
    base_doc = _parse_doc(base, "merge-base")
    tip_doc = _parse_doc(branch, "branch")
    main_doc = _parse_doc(main, "main")
    _check_branch_shape(base_doc, tip_doc)
    own = _own_items(base_doc, tip_doc)
    main_present = {item.normalized for item in _parse_block(main_doc.lines).items}
    inserted = [item for item in own if item.normalized not in main_present]
    skipped = tuple(item for item in own if item.normalized in main_present)
    if not inserted:
        return RebuildResult(text=main, inserted=(), skipped_present=skipped)
    body = _body(main_doc)
    for item in inserted:
        _insert_item(body, item)
    has_following = main_doc.unreleased_end < len(main_doc.lines)
    if has_following and (not body or body[-1].strip()):
        body.append("")
    lines = [
        *main_doc.lines[: main_doc.unreleased_start + 1],
        *body,
        *main_doc.lines[main_doc.unreleased_end :],
    ]
    text = main_doc.newline.join(lines)
    if main_doc.trailing_newline:
        text += main_doc.newline
    _check_postconditions(main_doc, _parse_doc(text, "rebuilt"), inserted)
    return RebuildResult(text=text, inserted=tuple(inserted), skipped_present=skipped)


def rebuild_changelog(*, base: str, branch: str, main: str) -> RebuildResult:
    """Main's CHANGELOG plus the branch's own [Unreleased] bullets.

    *base* is the merge-base copy, *branch* the PR tip's, *main* the default
    branch's. The branch's own bullets are its [Unreleased] items missing from
    *base* (compared by subsection and whitespace-normalised text). Each is
    appended to the matching ``### `` subsection of main's [Unreleased],
    created once in Keep-a-Changelog order when main lacks it, unless its
    text already appears anywhere in main (then it lands in
    ``skipped_present``). With nothing to insert, *main* is returned as is.

    Raises :class:`ChangelogRebuildError` (``changelog_unparseable``,
    ``changelog_released_section_touched`` or ``changelog_ambiguous``) rather
    than guess. The result is self-checked: main's preamble and released
    sections are byte-identical, the result is additive over main, every
    inserted bullet occurs once, there is no duplicate heading, and a second
    rebuild over the result changes nothing.
    """
    result = _rebuild(base, branch, main)
    if result.inserted and _rebuild(base, branch, result.text).text != result.text:
        raise _fail(
            ChangelogRepairDisposition.CHANGELOG_AMBIGUOUS,
            "rebuild is not idempotent over its own result",
        )
    return result
