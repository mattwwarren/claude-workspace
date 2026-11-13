"""Re-home a CHANGELOG hunk that a release cut left in a released section (#3190).

A squash merge is a 3-way merge of the PR diff onto the current default
branch, and GitHub ignores the ``CHANGELOG.md merge=union`` driver. A release
cut inserts ``## [X.Y.Z]`` right under ``## [Unreleased]``; a PR bullet
appended lower in a subsection does not touch those lines, so the PR stays
``MERGEABLE`` and the bullet lands inside the freshly released section.

:func:`sync_changelog` fixes the branch before that merge happens. When
merging ``origin/<base>`` into HEAD would change a released section, it merges
``origin/<base>`` (``--no-ff``, one merge commit) with ``CHANGELOG.md``
replaced by :func:`cw.changelog_rebuild.rebuild_changelog`'s result: main's
text plus the branch's own ``[Unreleased]`` bullets. It has to be a merge,
not a plain commit on the old merge-base: the merge makes main an ancestor,
so the squash diff is just the bullets under ``[Unreleased]``.

It never fetches, never rebases and never pushes. Anything it cannot do
safely (a rebuild refusal, a conflict, a dirty tree, an in-progress merge, a git
failure) writes nothing; a merge it started and could not finish is aborted.

CHANGELOG blobs are read byte-exact (``text=False``, decoded as UTF-8), the
``repair_git._show_blob`` discipline: text mode would translate CRLF and bare
CR before the rebuild's mixed-line-ending guard saw them, so the rebuilt text
keeps main's line endings and a mixed-ending file is refused.

Never spawns ``git merge-tree`` itself: :func:`cw.merge_probe.merge_tree` is
the one spawner (ADR-0022 invariant 1). Imports only :mod:`cw._git`,
:mod:`cw.merge_probe`, :mod:`cw.changelog_rebuild` and
:mod:`cw.repair_disposition`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, cast

from cw._git import run_git
from cw.changelog_rebuild import (
    ChangelogRebuildError,
    rebuild_changelog,
    released_sections_changed,
)
from cw.merge_probe import (
    PROBE_TIMEOUT_SECONDS,
    GitFailedError,
    already_merged,
    failure_detail,
    git_bounded,
    merge_tree,
    resolve_base,
)
from cw.repair_disposition import RepairDisposition

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "COMMIT_MESSAGE",
    "COMMIT_TIMEOUT_SECONDS",
    "DISPOSITION_DIRTY_WORKTREE",
    "ChangelogSync",
    "SyncState",
    "sync_changelog",
]


class SyncState(StrEnum):
    """The states returned by :func:`sync_changelog`."""

    CLEAN = "clean"
    MOVED = "moved"
    CONFLICT = "conflict"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


#: The command-local disposition for a move refused over uncommitted changes.
DISPOSITION_DIRTY_WORKTREE: Final = "dirty_worktree"

#: The merge commit's message.
COMMIT_MESSAGE: Final = (
    "chore(changelog): merge the default branch and re-home CHANGELOG entries"
    " after a release cut (#3190)"
)

#: Bound on the merge and the commit: the commit runs the repo's own
#: pre-commit hook, which is not bypassed.
COMMIT_TIMEOUT_SECONDS: Final = 120.0

_CHANGELOG: Final = "CHANGELOG.md"
_DETAIL_LIMIT: Final = 300


@dataclass(frozen=True)
class ChangelogSync:
    """:func:`sync_changelog`'s answer (the ``cw worktree changelog-sync`` JSON).

    ``head_sha`` is HEAD after the call (the new merge commit when ``state``
    is ``moved``, else HEAD as it was), ``None`` if unreadable.
    ``disposition`` is ``None``, a ``RepairDisposition`` value from the
    rebuild, or :data:`DISPOSITION_DIRTY_WORKTREE`. ``inserted`` holds the
    verbatim bullets placed under ``[Unreleased]`` (``moved`` only).
    """

    state: SyncState
    base_sha: str | None
    head_sha: str | None
    disposition: str | None
    conflicted_files: tuple[str, ...]
    inserted: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class _Move:
    """A move :func:`_apply` makes: merge *base_sha*, write *text*, commit."""

    base_sha: str
    head_sha: str
    text: str
    inserted: tuple[str, ...]


def _answer(
    state: SyncState,
    base_sha: str | None,
    head_sha: str | None,
    *,
    disposition: str | None = None,
    conflicted_files: tuple[str, ...] = (),
    inserted: tuple[str, ...] = (),
    detail: str = "",
) -> ChangelogSync:
    return ChangelogSync(
        state,
        base_sha,
        head_sha,
        disposition,
        conflicted_files,
        inserted,
        detail[:_DETAIL_LIMIT],
    )


def _checked(
    worktree: Path, argv: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """:func:`git_bounded`, raising :class:`GitFailedError` on a non-zero exit."""
    result = git_bounded(worktree, argv, timeout)
    if result.returncode != 0:
        raise GitFailedError(failure_detail(argv[0], result))
    return result


def _head_sha(worktree: Path, timeout: float) -> str | None:
    try:
        return _checked(
            worktree, ["rev-parse", "--verify", "HEAD"], timeout
        ).stdout.strip()
    except GitFailedError:
        return None


def _show_changelog(worktree: Path, rev: str, timeout: float) -> str | None:
    """``CHANGELOG.md`` at *rev*, byte-exact and UTF-8; ``None`` if absent."""
    what = f"git show {rev}:{_CHANGELOG}"
    try:
        exists = run_git(
            ["rev-parse", "--verify", "--quiet", f"{rev}:{_CHANGELOG}"],
            cwd=worktree,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
        if exists.returncode == 1:
            return None
        if exists.returncode != 0:
            raise GitFailedError(failure_detail("rev-parse", exists))
        # ``run_git`` is annotated for the text default; ``text=False`` yields bytes.
        shown = cast(
            "subprocess.CompletedProcess[bytes]",
            run_git(
                ["show", f"{rev}:{_CHANGELOG}"],
                cwd=worktree,
                check=False,
                capture_output=True,
                text=False,
                timeout=timeout,
            ),
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"{what} timed out after {timeout:g}s"
        raise GitFailedError(msg) from exc
    except (OSError, ValueError) as exc:
        msg = f"{what} could not run: {exc}"
        raise GitFailedError(msg) from exc
    if shown.returncode != 0:
        stderr = shown.stderr or b""
        if isinstance(stderr, bytes):
            stderr_text = stderr.decode("utf-8", errors="replace")
        else:
            stderr_text = str(stderr)
        reason = stderr_text.strip().splitlines()
        detail = reason[0] if reason else "no output"
        message = f"{what} exited {shown.returncode}: {detail}"
        raise GitFailedError(message)
    try:
        return shown.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChangelogRebuildError(
            RepairDisposition.CHANGELOG_UNPARSEABLE, f"{what} is not UTF-8: {exc}"
        ) from exc


def _plan_move(
    worktree: Path, base: str, base_sha: str, head: str, tree: str, timeout: float
) -> ChangelogSync | _Move:
    """Steps 3-5: compare, check ancestry, rebuild. May raise for the caller."""
    main = _show_changelog(worktree, base_sha, timeout)
    merged = _show_changelog(worktree, tree, timeout)
    changed = (
        released_sections_changed(main, merged)
        if main is not None and merged is not None
        else ()
    )
    if main is None or not changed:
        return _answer(SyncState.CLEAN, base_sha, head)
    if already_merged(worktree, base_sha, timeout):
        return _answer(
            SyncState.REFUSED,
            base_sha,
            head,
            disposition=RepairDisposition.CHANGELOG_RELEASED_SECTION_TOUCHED.value,
            detail=(
                f"origin/{base} is already an ancestor of HEAD, yet HEAD's"
                f" {_CHANGELOG} changes released section(s) {', '.join(changed)};"
                " a changelog-sync merge cannot fix that"
            ),
        )
    merge_base = _checked(worktree, ["merge-base", "HEAD", base_sha], timeout)
    base_text = _show_changelog(worktree, merge_base.stdout.strip(), timeout)
    branch_text = _show_changelog(worktree, "HEAD", timeout)
    rebuilt = rebuild_changelog(
        base=base_text or "", branch=branch_text or "", main=main
    )
    return _Move(base_sha, head, rebuilt.text, tuple(i.text for i in rebuilt.inserted))


def _settle(
    worktree: Path, base: str, base_sha: str, head: str, timeout: float
) -> ChangelogSync | _Move:
    """Steps 2-5 once ``origin/<base>`` and HEAD resolved."""
    found = merge_tree(worktree, "HEAD", base_sha, timeout=timeout)
    if found.status == "unavailable":
        return _answer(SyncState.UNAVAILABLE, base_sha, head, detail=found.detail)
    if found.status == "conflict" or found.tree is None:
        return _answer(
            SyncState.CONFLICT,
            base_sha,
            head,
            conflicted_files=found.conflicted_files,
            detail=(
                f"merging origin/{base} conflicts; this is not a misplaced-hunk"
                " case: take finalize Step 4c.5's conflict path"
            ),
        )
    try:
        return _plan_move(worktree, base, base_sha, head, found.tree, timeout)
    except GitFailedError as exc:
        return _answer(SyncState.UNAVAILABLE, base_sha, head, detail=str(exc))
    except ChangelogRebuildError as exc:
        return _answer(
            SyncState.REFUSED,
            base_sha,
            head,
            disposition=exc.disposition.value,
            detail=exc.detail,
        )


def _analyse(
    worktree: Path, base: str, head: str | None, timeout: float
) -> ChangelogSync | _Move:
    """Steps 1-5: a finished answer, or the :class:`_Move` to make."""
    try:
        base_sha = resolve_base(worktree, base, timeout)
    except GitFailedError as exc:
        return _answer(SyncState.UNAVAILABLE, None, head, detail=str(exc))
    if head is None:
        return _answer(
            SyncState.UNAVAILABLE, base_sha, None, detail="HEAD does not resolve"
        )
    return _settle(worktree, base, base_sha, head, timeout)


def _merge_in_progress(worktree: Path, timeout: float) -> bool:
    """Return whether a merge is in progress; raise if the probe failed."""
    argv = ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"]
    result = git_bounded(worktree, argv, timeout)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitFailedError(failure_detail("rev-parse", result))


def _abort_merge(worktree: Path, detail: str, timeout: float) -> str:
    """Run ``git merge --abort``; *detail* grows a note when the abort fails."""
    try:
        _checked(worktree, ["merge", "--abort"], timeout)
    except GitFailedError as exc:
        return (
            f"{detail}; git merge --abort failed ({exc}): the worktree is"
            " mid-merge, run `git merge --abort` by hand"
        )
    return detail


def _abort(worktree: Path, move: _Move, detail: str, timeout: float) -> ChangelogSync:
    """Abort any merge this call started; the answer is ``unavailable``."""
    try:
        in_progress = _merge_in_progress(worktree, timeout)
    except GitFailedError as exc:
        detail = f"{detail}; could not determine merge state ({exc})"
        in_progress = True  # abort anyway: an unreadable state may be mid-merge
    if in_progress:
        detail = _abort_merge(worktree, detail, timeout)
    return _answer(SyncState.UNAVAILABLE, move.base_sha, move.head_sha, detail=detail)


def _apply(worktree: Path, move: _Move, timeout: float) -> ChangelogSync:
    """Steps 6-7: dirty check, merge, write, commit; abort on any failure."""
    try:
        existing_merge = _merge_in_progress(worktree, timeout)
    except GitFailedError as exc:
        return _answer(
            SyncState.UNAVAILABLE, move.base_sha, move.head_sha, detail=str(exc)
        )
    if existing_merge:
        return _answer(
            SyncState.REFUSED,
            move.base_sha,
            move.head_sha,
            detail=(
                "worktree already has an in-progress merge; resolve or abort it"
                " before running changelog-sync"
            ),
        )
    argv = ["status", "--porcelain", "--untracked-files=no"]
    try:
        dirty = _checked(worktree, argv, timeout).stdout.strip()
    except GitFailedError as exc:
        return _answer(
            SyncState.UNAVAILABLE, move.base_sha, move.head_sha, detail=str(exc)
        )
    if dirty:
        return _answer(
            SyncState.REFUSED,
            move.base_sha,
            move.head_sha,
            disposition=DISPOSITION_DIRTY_WORKTREE,
            detail="tracked files have uncommitted changes; commit or stash them first",
        )
    try:
        _checked(
            worktree,
            ["merge", "--no-ff", "--no-commit", move.base_sha],
            COMMIT_TIMEOUT_SECONDS,
        )
        (worktree / _CHANGELOG).write_bytes(move.text.encode("utf-8"))
        _checked(worktree, ["add", "--", _CHANGELOG], timeout)
        _checked(worktree, ["commit", "-m", COMMIT_MESSAGE], COMMIT_TIMEOUT_SECONDS)
    except (GitFailedError, OSError) as exc:
        return _abort(worktree, move, str(exc), timeout)
    return _answer(
        SyncState.MOVED,
        move.base_sha,
        _head_sha(worktree, timeout),
        inserted=move.inserted,
    )


def sync_changelog(
    worktree: Path,
    base: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ChangelogSync:
    """Merge ``origin/<base>`` with a rebuilt CHANGELOG when a cut misfiled it.

    ``clean`` when merging ``origin/<base>`` keeps every released section as
    ``origin/<base>`` has it (or either side lacks a ``CHANGELOG.md``);
    ``moved`` after one merge commit; ``conflict`` when ``origin/<base>``
    does not merge cleanly; ``refused`` for a rebuild refusal, a branch that
    already contains ``origin/<base>``, an in-progress merge, or uncommitted
    tracked changes (the ancestor refusal carries disposition
    ``changelog_released_section_touched``; the in-progress one carries none);
    ``unavailable`` for a git failure. Never raises and never fetches; every
    git call is bounded by *timeout* (the merge and commit by
    :data:`COMMIT_TIMEOUT_SECONDS`).
    """
    head = _head_sha(worktree, timeout)
    analysed = _analyse(worktree, base, head, timeout)
    if isinstance(analysed, ChangelogSync):
        return analysed
    return _apply(worktree, analysed, timeout)
