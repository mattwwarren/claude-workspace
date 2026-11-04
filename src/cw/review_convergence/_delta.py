"""The delta confirm round for an overlapping default-branch merge (#2918).

A clean-mode ledger converges at ``last_clean_sha``. A later merge of
``origin/<default>`` is tolerated (``_merge_tolerance``) only when it changes
none of the branch's own files. One that does overlap the branch is rejected,
which used to force a whole new review round. When the client opts in
(``review.default_merge_delta_confirm``), :func:`assess_delta_round` decides
whether a much smaller round may record the move instead: a cw-run confirm
round over just what the merge brought into the overlapping files
(``last_clean_sha..HEAD`` limited to them), with the branch's own hunks in
those files as context. A clean delta round moves ``last_clean_sha`` to HEAD
(``record_delta_round`` in ``_engine``); findings reopen the loop.

Eligibility, cheapest check first, so a ledger refusal costs no git call:

1. a tolerance (``tolerance_off``);
2. a ``finalize_ready`` ledger with a ``claude-native`` loop (the same
   allow-list as ``converged_across_tolerated_merge``, through the one shared
   ``tolerable_clean_sha``), an independent
   ``codex`` or ``claude`` confirm, and a HEAD that is not its clean sha
   (``ledger_not_confirmable``; a HEAD at the clean sha is
   ``not_overlap_merge``);
3. :func:`~cw.review_convergence._merge_tolerance.overlapping_default_merge`
   names a non-empty overlap (``not_overlap_merge``);
4. ``MergeTolerance.delta_confirm`` (``delta_disabled``; the overlap is still
   reported);
5. every branch merge in the span is a two-parent merge that ``git merge-tree
   --write-tree`` reproduces exactly from its parents (``not_textual_merge``):
   an octopus merge, a conflict, a hand-resolved or hand-edited merge, an old
   git without ``--write-tree`` and every unreadable git state stay strict.

Every git call goes through ``_merge_tolerance``'s ``git_stdout_or_none`` /
``rev_parse_or_none``, so the default-deny test fake covers this module too.
Nothing here fetches: a stale ``origin/<default>`` ref only rejects more.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cw.review_convergence import _merge_tolerance as tolerance_git
from cw.review_convergence._tip import has_independent_confirm

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence._merge_tolerance import MergeSpan, MergeTolerance
    from cw.review_convergence._models import ConvergenceLedger

DeltaRefusal = Literal[
    "tolerance_off",
    "delta_disabled",
    "ledger_not_confirmable",
    "not_overlap_merge",
    "not_textual_merge",
]


@dataclass(frozen=True)
class DeltaSpec:
    """What a delta round reviews.

    The diff is ``from_sha..to_sha`` (the clean sha to HEAD) limited to
    *paths* (the overlapping files). ``branch_base..from_sha`` in the same
    paths is the branch's own, already-reviewed hunks, shown as context;
    ``branch_base`` is ``git merge-base <clean> origin/<default>``.
    """

    from_sha: str
    to_sha: str
    paths: tuple[str, ...]
    branch_base: str


@dataclass(frozen=True)
class DeltaPlan:
    """An eligible delta round: its :class:`DeltaSpec` and the merges it covers."""

    spec: DeltaSpec
    merges: tuple[str, ...]


@dataclass(frozen=True)
class DeltaAssessment:
    """:func:`assess_delta_round`'s answer.

    Exactly one of *plan* and *refusal* is set. ``overlapping_files`` is the
    overlap computed or supplied, ``()`` when a refusal came before it was
    known.
    """

    overlapping_files: tuple[str, ...]
    plan: DeltaPlan | None
    refusal: DeltaRefusal | None


def _ledger_refusal(ledger: ConvergenceLedger, head_sha: str) -> DeltaRefusal | None:
    """The ledger-only refusal of :func:`assess_delta_round`, or ``None``."""
    clean = tolerance_git.tolerable_clean_sha(ledger)
    if clean is None or not has_independent_confirm(ledger):
        return "ledger_not_confirmable"
    if head_sha == clean:
        return "not_overlap_merge"
    return None


def _first_line(text: str | None) -> str | None:
    """The stripped first line of *text*; ``None`` for no text or a blank line."""
    lines = (text or "").splitlines()
    first = lines[0].strip() if lines else ""
    return first or None


def _textually_clean(worktree: Path, merge: str, others: list[str]) -> bool:
    """True when *merge* is the two-parent merge git itself would produce.

    ``git merge-tree --write-tree`` of the merge's two parents must exit 0
    (no conflict) with exactly the merge commit's own tree (no hand edit).
    """
    if len(others) != 1:
        return False
    argv = ["merge-tree", "--write-tree", "--no-messages", f"{merge}^1", f"{merge}^2"]
    written = _first_line(tolerance_git.git_stdout_or_none(worktree, argv))
    if written is None:
        return False
    return written == tolerance_git.rev_parse_or_none(worktree, f"{merge}^{{tree}}")


def _textual_plan(
    worktree: Path, span: MergeSpan, clean: str, head: str, files: tuple[str, ...]
) -> DeltaPlan | None:
    """The plan when every merge in *span* is textually clean, else ``None``."""
    if not all(
        _textually_clean(worktree, merge, others) for merge, others in span.merges
    ):
        return None
    base = _first_line(
        tolerance_git.git_stdout_or_none(
            worktree, ["merge-base", clean, span.default_sha]
        )
    )
    if base is None:
        return None
    spec = DeltaSpec(from_sha=clean, to_sha=head, paths=files, branch_base=base)
    return DeltaPlan(spec=spec, merges=tuple(merge for merge, _ in span.merges))


def _plan_or_none(
    worktree: Path,
    clean: str,
    head: str,
    tolerance: MergeTolerance,
    files: tuple[str, ...],
) -> DeltaPlan | None:
    span = tolerance_git.merge_span(worktree, clean, head, tolerance)
    if span is None:
        return None
    return _textual_plan(worktree, span, clean, head, files)


def assess_delta_round(
    ledger: ConvergenceLedger,
    *,
    worktree: Path,
    head_sha: str,
    tolerance: MergeTolerance | None,
    overlap: tuple[str, ...] | None = None,
) -> DeltaAssessment:
    """Whether a delta confirm round may record HEAD for *ledger* (#2918).

    See the module docstring for the check order and the refusal tokens.
    *overlap* is an already-computed
    :func:`~cw.review_convergence._merge_tolerance.overlapping_default_merge`
    result (``verify-tip`` passes the one it named); when given it is reused
    and not recomputed, and ``()`` is ``not_overlap_merge``. A ledger refusal
    reports *overlap* (or ``()``) and makes no git call.
    """
    if tolerance is None:
        return DeltaAssessment((), None, "tolerance_off")
    refusal = _ledger_refusal(ledger, head_sha)
    clean = ledger.last_clean_sha
    if refusal is not None or clean is None:
        return DeltaAssessment(overlap or (), None, refusal or "ledger_not_confirmable")
    files = (
        overlap
        if overlap is not None
        else tolerance_git.overlapping_default_merge(
            worktree, clean, head_sha, tolerance
        )
    )
    if not files:
        return DeltaAssessment((), None, "not_overlap_merge")
    if not tolerance.delta_confirm:
        return DeltaAssessment(files, None, "delta_disabled")
    plan = _plan_or_none(worktree, clean, head_sha, tolerance, files)
    return DeltaAssessment(files, plan, None if plan else "not_textual_merge")


def pending_delta(
    ledger: ConvergenceLedger,
    *,
    worktree: Path,
    head_sha: str,
    tolerance: MergeTolerance | None,
    tolerated: bool,
) -> tuple[bool, tuple[str, ...]]:
    """``(pending, overlapping_files)`` for ``init`` and ``status`` (#2918).

    The one place "a delta confirm round is pending" is decided, so the two
    surfaces cannot disagree on ``next_action``. A move already *tolerated*
    needs no round (``(False, ())``, no git call), and neither does a client
    without ``delta_confirm`` (the default, strict path stays git-free here:
    ``verify-tip`` and ``finalize`` still name the overlap through
    :func:`assess_delta_round`). Otherwise it is pending exactly when
    :func:`assess_delta_round` returns a plan. ``overlapping_files`` is the
    overlap the assessment named, ``()`` when it refused before it was known,
    and is meaningful to callers only when pending.
    """
    if tolerated or tolerance is None or not tolerance.delta_confirm:
        return False, ()
    assessment = assess_delta_round(
        ledger, worktree=worktree, head_sha=head_sha, tolerance=tolerance
    )
    return assessment.plan is not None, assessment.overlapping_files
