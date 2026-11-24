"""The tree-level proof behind the clean-sha carry (#2921, #3375).

A ``repair_changelog_conflict`` repair rewrites the branch: it rebases every
commit onto the default branch and adds one rebuilt-CHANGELOG commit. A clean
review covered the cumulative diff of the old head, never the commit shape, so
the carry may move ``last_clean_sha`` to the new head when the new head is
exactly what git itself would write for the reviewed head plus the default
branch, with the CHANGELOG confined. :func:`prove_tree_unchanged` checks:

1. the non-CHANGELOG tree of the new head equals the tree
   ``git merge-tree --write-tree`` writes for the pinned default commit and the
   reviewed head (a CHANGELOG-only conflict is expected without the union
   driver and allowed);
2. the CHANGELOG delta is confined: the default branch's released sections are
   byte-identical, no line is deleted, every added line sits inside the default
   branch's ``[Unreleased]`` span, and every added non-blank line is one the
   reviewed head itself added (blank lines and a ``### <Name>`` heading the
   reviewed head carried are exempt from that provenance check);
3. the recipe added at most one CHANGELOG-only commit on the rebased series,
   and at least one branch commit survives.

A refusal raises :class:`RepairStopError`. Its disposition is not load-bearing:
``changelog_repair._prove_remap`` catches any ``RepairStopError`` and only
logs and surfaces its ``.detail``, so the failures of this module and of the
reused ``repair_git`` helpers are not re-wrapped. The detail text is the
contract. Writes nothing; runs post-lock in the scratch tree.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from typing import TYPE_CHECKING, NamedTuple, NoReturn, cast

from cw._git import run_git
from cw.changelog_rebuild import parse_headings, released_sections_changed
from cw.merge_probe import merge_tree
from cw.reconcile.review_recipes.repair_git import (
    _T_LOCAL,
    CHANGELOG,
    RepairStopError,
    _changelog_only_delta,
    _commit_count,
    _failed,
    _git,
    _show_blob,
    _split_z,
    _tail,
)
from cw.repair_disposition import RepairDisposition

if TYPE_CHECKING:
    from pathlib import Path

_UNRELEASED = "Unreleased"
_SUBSECTION_PREFIX = "### "
_HUNK_RE = re.compile(r"^@@ -(?P<start>\d+)(?:,(?P<count>\d+))? \+\d+(?:,\d+)? @@")
_VERIFY = RepairDisposition.VERIFY_FAILED


class _Hunk(NamedTuple):
    """One ``git diff -U0`` hunk: where it sits in the old file and what it adds."""

    old_start: int
    old_count: int
    added: list[str]


def _refuse(detail: str) -> NoReturn:
    """Refuse the carry: raise :class:`RepairStopError` carrying *detail*."""
    raise RepairStopError(_VERIFY, detail)


def stray_paths(scratch: Path, tree: str, new_sha: str) -> list[str]:
    """Every path other than CHANGELOG.md that differs between *tree* and *new_sha*."""
    diffed = _git(
        scratch,
        "diff",
        "--no-renames",
        "--name-only",
        "-z",
        tree,
        new_sha,
        timeout=_T_LOCAL,
        stop=_VERIFY,
    )
    if diffed.returncode != 0:
        _refuse(_failed(diffed, "git diff --name-only"))
    return sorted(path for path in _split_z(diffed.stdout) if path != CHANGELOG)


def _diff_text(scratch: Path, since: str, until: str) -> str:
    """``git diff -U0`` of CHANGELOG.md between two commits, byte-exact.

    ``run_git(text=True)`` would turn CRLF into LF and split a bare CR into two
    lines before the proof could compare them with the byte-exact blobs
    :func:`~cw.reconcile.review_recipes.repair_git._show_blob` reads, so the
    output is read as bytes and decoded the way that helper decodes (UTF-8).
    Every failure refuses with the ``_git`` wording.
    """
    argv = ("diff", "-U0", "--no-renames", since, until, "--", CHANGELOG)
    try:
        # ``run_git`` is annotated for the text default; ``text=False`` yields bytes.
        diffed = cast(
            "subprocess.CompletedProcess[bytes]",
            run_git(
                argv,
                cwd=scratch,
                check=False,
                capture_output=True,
                text=False,
                timeout=_T_LOCAL,
            ),
        )
    except subprocess.TimeoutExpired as exc:
        detail = f"git {' '.join(argv)} timed out after {_T_LOCAL:g}s"
        raise RepairStopError(_VERIFY, detail) from exc
    except OSError as exc:
        detail = f"git {' '.join(argv)} could not run: {exc}"
        raise RepairStopError(_VERIFY, detail) from exc
    if diffed.returncode != 0:
        failure = (diffed.stderr or diffed.stdout).decode("utf-8", errors="replace")
        _refuse(f"git diff -U0 exit {diffed.returncode}: {_tail(failure)}")
    try:
        return diffed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        _refuse(f"git diff -U0 output is not UTF-8: {exc}")


def _zero_context_hunks(scratch: Path, since: str, until: str) -> list[_Hunk]:
    """The ``git diff -U0`` hunks of CHANGELOG.md between two commits.

    The diff is read byte-exactly and split on ``\\n`` only, so a line keeps a
    trailing ``\\r`` (CRLF) and an embedded bare ``\\r`` verbatim, the way the
    blobs it is compared with are split. Output that carries no ``@@`` header (a
    binary or unreadable diff) refuses.
    """
    text = _diff_text(scratch, since, until)
    hunks: list[_Hunk] = []
    for line in text.split("\n"):
        header = _HUNK_RE.match(line)
        if header is not None:
            count = header["count"]
            hunks.append(
                _Hunk(int(header["start"]), 1 if count is None else int(count), [])
            )
        elif hunks and line.startswith("+"):
            hunks[-1].added.append(line[1:])
    if not hunks and text.strip():
        _refuse(f"unreadable CHANGELOG diff: {_tail(text)}")
    return hunks


def _lines(text: str) -> list[str]:
    """*text* split on ``\\n`` with the trailing empty element dropped (git's count)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _unreleased_span(default_text: str) -> tuple[int, int]:
    """``(h, n)``: the ``[Unreleased]`` heading's index and the span's end index.

    ``n`` is the next ``## [`` heading's index, or the line count when none follows.
    """
    lines = _lines(default_text)
    headings = parse_headings(lines)
    unreleased = [heading for heading in headings if heading.version == _UNRELEASED]
    if len(unreleased) != 1:
        _refuse(
            f"the default branch's CHANGELOG.md has {len(unreleased)}"
            " [Unreleased] headings, expected exactly one"
        )
    start = unreleased[0].line
    later = [heading.line for heading in headings if heading.line > start]
    return start, min(later, default=len(lines))


def _check_position(hunks: list[_Hunk], span: tuple[int, int]) -> None:
    """(a): every hunk is a pure insertion inside the ``[Unreleased]`` span."""
    start, end = span
    for hunk in hunks:
        if hunk.old_count != 0:
            _refuse(
                "the CHANGELOG rebuild deleted or modified a line of the default"
                f" branch's CHANGELOG.md at line {hunk.old_start}"
            )
        if not start + 1 <= hunk.old_start <= end:
            shown = _tail(next((line for line in hunk.added if line.strip()), ""))
            _refuse(
                f"CHANGELOG line {shown!r} is outside the default branch's"
                " [Unreleased] span"
            )


def _check_provenance(
    hunks: list[_Hunk], attested_added: list[str], attested_text: str
) -> None:
    """(b): every added non-blank line is one the reviewed head itself added."""
    allowed = Counter(attested_added)
    carried = set(attested_text.split("\n"))
    for hunk in hunks:
        for line in hunk.added:
            if not line.strip():
                continue
            if allowed[line] > 0:
                allowed[line] -= 1
            elif not (line.startswith(_SUBSECTION_PREFIX) and line in carried):
                _refuse(
                    f"CHANGELOG line {_tail(line)!r} was never added by the reviewed"
                    " head"
                )


def _prove_changelog_confined(
    scratch: Path, expected_sha: str, default_sha: str, new_sha: str, merge_base: str
) -> None:
    """The CHANGELOG delta against the default branch is confined (see module doc)."""
    default_text = _show_blob(scratch, default_sha)
    new_text = _show_blob(scratch, new_sha)
    attested_text = _show_blob(scratch, expected_sha)
    changed = released_sections_changed(default_text, new_text)
    if changed:
        _refuse(f"the rebuilt CHANGELOG changed released section(s): {changed}")
    hunks = _zero_context_hunks(scratch, default_sha, new_sha)
    _check_position(hunks, _unreleased_span(default_text))
    attested = _zero_context_hunks(scratch, merge_base, expected_sha)
    _check_provenance(
        hunks, [line for hunk in attested for line in hunk.added], attested_text
    )


def prove_tree_unchanged(
    scratch: Path,
    expected_sha: str,
    default_sha: str,
    rebased: str,
    new_sha: str,
    merge_base: str,
) -> int:
    """Prove *new_sha* carries the review of *expected_sha*; the commits carried.

    *default_sha* is the pinned default-branch commit the rebase ran onto,
    *rebased* the series head before the CHANGELOG commit (equal to *new_sha*
    when none was made) and *merge_base* the reviewed head's fork point.
    Raises :class:`RepairStopError` on any failure to prove.
    """
    merged = merge_tree(scratch, default_sha, expected_sha)
    if merged.status == "unavailable" or merged.tree is None:
        _refuse(f"git merge-tree unavailable: {merged.detail}")
    others = [path for path in merged.conflicted_files if path != CHANGELOG]
    if merged.status == "conflict" and others:
        _refuse(f"merging the default branch conflicts on: {others}")
    stray = stray_paths(scratch, merged.tree, new_sha)
    if stray:
        _refuse(f"the new head differs from the merge on: {stray}")
    _prove_changelog_confined(scratch, expected_sha, default_sha, new_sha, merge_base)
    _changelog_only_delta(scratch, rebased, new_sha)
    count = _commit_count(scratch, default_sha, rebased)
    if count == 0:
        _refuse("no branch commit survives the rebase")
    return count
