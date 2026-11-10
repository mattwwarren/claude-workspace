"""Does merging the default branch conflict? One read-only git answer (#2979).

The default-branch merge policy (ADR-0022) has three rules: a branch that is
merely behind ``origin/<default>`` is never acted on; ``origin/<default>`` is
merged at most once, and only when the merge would conflict; and a clean merge
never causes a review. Every site that used to merge, rebase or park on
"behind" now asks this module first.

* :func:`merge_tree` is the only spawner for the probe, the tip rule and the
  REVIEW gate. The probe below uses it for "would this merge conflict?", and
  the tip rule (``cw.review_convergence._merge_tolerance``) uses it for "is
  this merge exactly the one git would write?". Two separate, pre-existing
  callers, ``repair_git._conflict_set`` (``cw.reconcile.review_recipes``) and
  the conflict listing in ``cw.pr_feedback``, each run their own
  ``git merge-tree --write-tree`` with their own parsing and are deliberately
  left alone. ``.claude/scripts/check_changelog_frozen.py`` (outside ``src/``)
  runs a third, stdlib-only ``--write-tree`` of its own and is left alone as
  well.
* :func:`probe_default_merge` answers ``up_to_date`` (``origin/<base>`` is
  already an ancestor of HEAD), ``clean`` (a merge would not conflict),
  ``conflict`` (it would; the conflicted files are named) or ``unavailable``
  (git could not tell: an unresolvable ref, a non-zero exit other than a
  conflict, an ``OSError``, a timeout, or a git older than 2.38 without
  ``--write-tree``).

Read-only: ``merge-tree --write-tree`` writes loose objects but touches
neither the index nor the worktree. **It never fetches.** A stale
``origin/<base>`` makes the probe wrongly answer ``up_to_date`` or ``clean``,
so every caller fetches first. Every git call is bounded by
:data:`PROBE_TIMEOUT_SECONDS`.

``merge_tree`` must run with ``cwd`` set to a non-bare worktree: the
``.gitattributes`` merge drivers (``CHANGELOG.md merge=union``) are read from
the working tree, so a bare repository would report a union-merged file as a
conflict.

The module is a leaf: it imports only :mod:`cw._git`. :func:`resolve_base`,
:func:`git_bounded` and :class:`GitFailedError` are public for
``cw.changelog_sync``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from cw._git import FULL_COMMIT_ID_RE, run_git

if TYPE_CHECKING:
    from pathlib import Path

#: Bound, in seconds, on every git call this module makes (the figure
#: ``dispatch/pr_gate`` uses for ``gh``). A timeout reads as ``unavailable``.
PROBE_TIMEOUT_SECONDS: Final = 10.0

#: ``git merge-tree --write-tree`` exit codes: 0 is a clean merge, 1 is a
#: conflict. Anything else (129 for an unknown option on git < 2.38, 128 for an
#: unknown object) is an error.
_MERGE_TREE_CLEAN_RC: Final = 0
_MERGE_TREE_CONFLICT_RC: Final = 1

#: ``git merge-base --is-ancestor`` exit codes.
_IS_ANCESTOR_RC: Final = 0
_NOT_ANCESTOR_RC: Final = 1

#: Cap on the stderr text carried in a ``detail``.
_DETAIL_LIMIT: Final = 200

MergeTreeStatus = Literal["clean", "conflict", "unavailable"]
ProbeState = Literal["up_to_date", "clean", "conflict", "unavailable"]


@dataclass(frozen=True)
class MergeTreeResult:
    """One ``git merge-tree --write-tree`` answer.

    ``tree`` is the written tree oid (set for ``clean`` and ``conflict``),
    ``conflicted_files`` the sorted conflicted paths (``conflict`` only), and
    ``detail`` a short human-readable reason (``unavailable`` only).
    """

    status: MergeTreeStatus
    tree: str | None
    conflicted_files: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class MergeProbe:
    """:func:`probe_default_merge`'s answer.

    ``base_sha`` is the ``origin/<base>`` commit that was probed (``None`` when
    it did not resolve), so a transcript shows which tip the answer is about.
    """

    state: ProbeState
    base_sha: str | None
    conflicted_files: tuple[str, ...]
    detail: str


class GitFailedError(Exception):
    """A git call that raised or timed out; its message is the detail."""


def git_bounded(
    worktree: Path, argv: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run git bounded by *timeout*; raise :class:`GitFailedError` on any failure."""
    try:
        return run_git(argv, cwd=worktree, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        msg = f"git {argv[0]} timed out after {timeout:g}s"
        raise GitFailedError(msg) from exc
    except (OSError, ValueError) as exc:
        msg = f"git {argv[0]} could not run: {exc}"
        raise GitFailedError(msg) from exc


def _failure_detail(argv0: str, result: subprocess.CompletedProcess[str]) -> str:
    """``git <argv0> exited <rc>: <first stderr line>``, capped."""
    lines = (result.stderr or "").strip().splitlines()
    reason = lines[0] if lines else "no output"
    return f"git {argv0} exited {result.returncode}: {reason}"[:_DETAIL_LIMIT]


def _unavailable_tree(detail: str) -> MergeTreeResult:
    return MergeTreeResult("unavailable", None, (), detail)


def merge_tree(
    worktree: Path,
    ours: str,
    theirs: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> MergeTreeResult:
    """The tree a merge of *theirs* into *ours* would write, and its conflicts.

    Runs ``git merge-tree --write-tree --no-messages --name-only -z``. Its
    output is ``<tree oid> NUL`` then one ``<path> NUL`` per conflicted file;
    anything else is ``unavailable``, as is every exit other than 0 or 1.
    """
    argv = ["merge-tree", "--write-tree", "--no-messages", "--name-only", "-z"]
    try:
        result = git_bounded(worktree, [*argv, ours, theirs], timeout)
    except GitFailedError as exc:
        return _unavailable_tree(str(exc))
    if result.returncode not in {_MERGE_TREE_CLEAN_RC, _MERGE_TREE_CONFLICT_RC}:
        return _unavailable_tree(_failure_detail("merge-tree", result))
    entries = result.stdout.split("\0")
    tree = entries[0].strip()
    if not FULL_COMMIT_ID_RE.fullmatch(tree) or entries[-1]:
        return _unavailable_tree("git merge-tree printed unreadable output")
    files = tuple(sorted({path for path in entries[1:-1] if path}))
    if result.returncode == _MERGE_TREE_CLEAN_RC:
        return MergeTreeResult("clean", tree, (), "")
    return MergeTreeResult("conflict", tree, files, "")


def resolve_base(worktree: Path, base: str, timeout: float) -> str:
    """The sha ``origin/<base>`` names; raise :class:`GitFailedError` if none."""
    ref = f"refs/remotes/origin/{base}^{{commit}}"
    result = git_bounded(worktree, ["rev-parse", "--verify", "--quiet", ref], timeout)
    sha = result.stdout.strip()
    if result.returncode != 0 or not FULL_COMMIT_ID_RE.fullmatch(sha):
        msg = f"origin/{base} does not resolve"
        raise GitFailedError(msg)
    return sha


def _already_merged(worktree: Path, base_sha: str, timeout: float) -> bool:
    """Whether *base_sha* is an ancestor of HEAD; raise on a git error."""
    result = git_bounded(
        worktree, ["merge-base", "--is-ancestor", base_sha, "HEAD"], timeout
    )
    if result.returncode == _IS_ANCESTOR_RC:
        return True
    if result.returncode == _NOT_ANCESTOR_RC:
        return False
    raise GitFailedError(_failure_detail("merge-base", result))


def probe_default_merge(
    worktree: Path,
    base: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> MergeProbe:
    """Whether merging ``origin/<base>`` into *worktree*'s HEAD would conflict.

    At most three git calls (``rev-parse``, ``merge-base --is-ancestor``,
    ``merge-tree``), each bounded by *timeout*. Never raises: every git
    failure is ``unavailable`` with a ``detail``. Never fetches.
    """
    try:
        base_sha = resolve_base(worktree, base, timeout)
    except GitFailedError as exc:
        return MergeProbe("unavailable", None, (), str(exc))
    try:
        merged = _already_merged(worktree, base_sha, timeout)
    except GitFailedError as exc:
        return MergeProbe("unavailable", base_sha, (), str(exc))
    if merged:
        return MergeProbe("up_to_date", base_sha, (), "")
    found = merge_tree(worktree, "HEAD", base_sha, timeout=timeout)
    if found.status == "unavailable":
        return MergeProbe("unavailable", base_sha, (), found.detail)
    return MergeProbe(found.status, base_sha, found.conflicted_files, "")
