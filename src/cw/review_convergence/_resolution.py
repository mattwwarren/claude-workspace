"""Whether a conflict-resolution round can review a move, and over what (#2980).

A clean-mode ledger tolerates a merge of ``origin/<default>`` that git itself
would write (:mod:`._merge_tolerance`, ADR-0022). A merge that carries a
conflict resolution, or a hand edit of a clean merge, is not one of those: its
tree differs from the tree ``git merge-tree --write-tree`` writes for its two
parents. The only part of it nobody has reviewed is that difference, so
``cw review converge delta-confirm`` reviews just it, as a ``delta`` round on
the ledger's independent confirm backend.

:func:`assess_resolution_round` decides whether such a round may run and what
it reviews. It shares the span, own-commit and per-merge checks with
:func:`~cw.review_convergence.default_merge_rejection` through
:func:`~._merge_tolerance.checked_span` and
:func:`~._merge_tolerance.merge_outcomes`, so the two cannot disagree about
which merges git wrote. It owns only the ledger gate, choosing the one
resolution merge, and the tree diff and branch base of that merge. Every git
call goes through ``_merge_tolerance``'s bounded readers, and every failure
direction is a refusal: the full review round is the fallback.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.review_convergence._merge_tolerance import (
    REJECT_GIT_UNREADABLE,
    REJECT_TOLERANCE_OFF,
    MergeResolution,
    checked_span,
    git_stdout_or_none,
    merge_outcomes,
    rev_parse_or_none,
)
from cw.review_convergence._tip import finalize_ready, has_independent_confirm

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.review_convergence._merge_tolerance import MergeTolerance, _Span
    from cw.review_convergence._models import ConvergenceLedger

#: :func:`assess_resolution_round` refusals of its own. The span and per-merge
#: refusals are :func:`~cw.review_convergence.default_merge_rejection`'s
#: reasons, unchanged.
REFUSE_LEDGER_NOT_CONFIRMABLE: Final = "ledger_not_confirmable"
REFUSE_NO_RESOLUTION: Final = "no_resolution"
REFUSE_MULTIPLE_RESOLUTIONS: Final = "multiple_resolutions"
REFUSE_EMPTY_RESOLUTION: Final = "empty_resolution"
REFUSE_MARKERS_LEFT: Final = "markers_left"

#: A conflict marker git writes: seven ``<``, ``=`` or ``>`` then a space or
#: the end of the line.
_CONFLICT_MARKER_RE: Final = re.compile(r"^(<{7}|={7}|>{7})( |$)")


@dataclass(frozen=True)
class ResolutionSpec:
    """What a conflict-resolution round reviews (#2980).

    The diff runs from *conflicted_tree* (the tree ``git merge-tree`` wrote
    for *merge*'s two parents, conflict markers left in) to *resolved_tree*
    (the merge commit's own tree). *paths* is the sorted list of repo-relative
    paths that diff changes. The branch context is ``branch_base..branch_tip``
    (the merge-base of the two parents to the merge's first parent) in those
    paths. *reviewed_sha* is HEAD, the sha the round records.
    """

    merge: str
    conflicted_tree: str
    resolved_tree: str
    reviewed_sha: str
    branch_base: str
    branch_tip: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionPlan:
    """A round that may run: its *spec* and every merge of the span, oldest first."""

    spec: ResolutionSpec
    merges: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionAssessment:
    """Exactly one of *plan* (the round may run) and *refusal* (why not)."""

    plan: ResolutionPlan | None
    refusal: str | None


def _refused(reason: str) -> ResolutionAssessment:
    return ResolutionAssessment(None, reason)


def _changed_paths(worktree: Path, conflicted: str, resolved: str) -> list[str] | None:
    argv = ["diff", "--name-only", "-z", "--no-renames", conflicted, resolved]
    out = git_stdout_or_none(worktree, argv)
    if out is None:
        return None
    return sorted({path for path in out.split("\0") if path})


def has_left_in_marker(diff_text: str) -> bool:
    """Whether a line *diff_text* adds is a conflict marker (#2980)."""
    return any(
        _CONFLICT_MARKER_RE.match(line[1:])
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _resolution_paths(
    worktree: Path, conflicted: str, resolved: str
) -> tuple[str, ...] | str:
    """The paths the resolution changes, or ``empty_resolution`` / ``markers_left``."""
    paths = _changed_paths(worktree, conflicted, resolved)
    if paths is None:
        return REJECT_GIT_UNREADABLE
    if not paths:
        return REFUSE_EMPTY_RESOLUTION
    argv = ["diff", "--no-color", "--no-renames", conflicted, resolved]
    diff_text = git_stdout_or_none(worktree, argv)
    if diff_text is None:
        return REJECT_GIT_UNREADABLE
    if has_left_in_marker(diff_text):
        return REFUSE_MARKERS_LEFT
    return tuple(paths)


def _branch_endpoints(worktree: Path, merge: str) -> tuple[str, str] | None:
    """``(merge-base of the two parents, first parent)``; ``None`` if unreadable."""
    out = git_stdout_or_none(worktree, ["merge-base", f"{merge}^1", f"{merge}^2"])
    base = out.strip() if out is not None else ""
    tip = rev_parse_or_none(worktree, f"{merge}^1")
    if not base or tip is None:
        return None
    return base, tip


def _spec_or_refusal(
    worktree: Path, found: MergeResolution, head_sha: str
) -> ResolutionSpec | str:
    """The spec for the one resolution merge *found*, or why it cannot be reviewed."""
    conflicted, resolved = found.conflicted_tree, found.resolved_tree
    if conflicted is None or resolved is None:
        return REJECT_GIT_UNREADABLE
    paths = _resolution_paths(worktree, conflicted, resolved)
    if isinstance(paths, str):
        return paths
    endpoints = _branch_endpoints(worktree, found.merge)
    if endpoints is None:
        return REJECT_GIT_UNREADABLE
    return ResolutionSpec(
        merge=found.merge,
        conflicted_tree=conflicted,
        resolved_tree=resolved,
        reviewed_sha=head_sha,
        branch_base=endpoints[0],
        branch_tip=endpoints[1],
        paths=paths,
    )


def _ledger_gate(
    ledger: ConvergenceLedger, head_sha: str, tolerance: MergeTolerance | None
) -> tuple[MergeTolerance, str] | str:
    """``(tolerance, clean sha)``, or the ledger-only refusal (no git call)."""
    if tolerance is None:
        return REJECT_TOLERANCE_OFF
    clean = ledger.last_clean_sha
    if (
        clean is None
        or not finalize_ready(ledger)
        or not has_independent_confirm(ledger)
        or head_sha == clean
    ):
        return REFUSE_LEDGER_NOT_CONFIRMABLE
    return tolerance, clean


def _one_resolution(
    worktree: Path, span: _Span, clock: Callable[[], float]
) -> MergeResolution | str:
    """The span's one resolution merge, or the first refusal its merges give."""
    resolutions: list[MergeResolution] = []
    for outcome in merge_outcomes(worktree, span, clock=clock):
        if isinstance(outcome, str):
            return outcome
        if isinstance(outcome, MergeResolution):
            resolutions.append(outcome)
    if not resolutions:
        return REFUSE_NO_RESOLUTION
    if len(resolutions) > 1:
        return REFUSE_MULTIPLE_RESOLUTIONS
    return resolutions[0]


def assess_resolution_round(
    ledger: ConvergenceLedger,
    worktree: Path,
    *,
    head_sha: str,
    tolerance: MergeTolerance | None,
    clock: Callable[[], float] = time.monotonic,
) -> ResolutionAssessment:
    """Whether ``delta-confirm`` may review the move to *head_sha*, and over what.

    Ledger first, with no git call: ``tolerance_off`` (``None`` *tolerance*),
    then ``ledger_not_confirmable`` (not
    :func:`~cw.review_convergence._tip.finalize_ready`, no clean sha, no
    independent confirm, or *head_sha* is the clean sha). Then git, sharing
    :func:`~cw.review_convergence.default_merge_rejection`'s checks and
    reasons: ``no_default_ref``, ``clean_not_ancestor``, ``no_merge``,
    ``git_unreadable``, ``non_merge_commit <sha7>``, then per merge
    ``not_two_parent``, ``second_parent_not_on_default`` and
    ``merge_tree_unavailable`` (each with ``<sha7>``). Of the merges git did
    not write itself there must be exactly one (``no_resolution``,
    ``multiple_resolutions``). Its tree diff must change something
    (``empty_resolution``) and add no conflict marker (``markers_left``), and
    its branch base must resolve (``git_unreadable``).
    """
    gate = _ledger_gate(ledger, head_sha, tolerance)
    if isinstance(gate, str):
        return _refused(gate)
    open_tolerance, clean = gate
    span = checked_span(worktree, clean, head_sha, open_tolerance.default_branch)
    if isinstance(span, str):
        return _refused(span)
    found = _one_resolution(worktree, span, clock)
    if isinstance(found, str):
        return _refused(found)
    spec = _spec_or_refusal(worktree, found, head_sha)
    if isinstance(spec, str):
        return _refused(spec)
    merges = tuple(merge for merge, _ in span.merges)
    return ResolutionAssessment(ResolutionPlan(spec, merges), None)
