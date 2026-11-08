"""A clean merge of the default branch never stales a clean-mode ledger (#2979).

A clean-mode ledger records the sha its last clean round reviewed. HEAD may
move past it only by merges of ``origin/<default>`` that git itself would have
written: each merge in the span is a two-parent merge whose second parent is on
the default branch, ``git merge-tree --write-tree`` of its two parents exits 0
(no conflict) and writes exactly the merge commit's own tree (no hand edit),
and the span holds no commit of the branch's own. Which files the merge touched
does not matter: a textually clean merge of reviewed code with already-merged
code is not a change anyone needs to review (ADR-0022).

:func:`default_merge_only` is the one git predicate for that question, and
:func:`default_merge_rejection` names why a move is not tolerated (the first
offending commit and a short reason), which ``cw review converge verify-tip``
puts in its ``detail``. ``verify-tip`` / ``finalize``, ``converge init`` /
``status`` and the dispatch REVIEW staleness gate all ask it (#2861), so they
cannot disagree about which moves are tolerated. The rule is the same for every
loop backend.

A merge that carries a conflict resolution, a hand edit, an octopus merge, a
git older than 2.38 (no ``--write-tree``), a ``merge-tree`` timeout and every
unreadable git state stay strict: the tip reads stale. A move whose one
resolution merge is the only thing git did not write can be reviewed over the
resolution alone (:func:`classify_merge` and :func:`merge_outcomes` feed
``cw review converge delta-confirm``, #2980); everything else gets a normal
review round. ``git merge-tree`` applies the ``.gitattributes`` merge drivers, so a
``CHANGELOG.md merge=union`` merge is reproduced and tolerated.

The tolerance itself is a :class:`MergeTolerance` (the default branch) or
``None`` (strict). The CLI reads it from the worktree's cw-context
(:func:`tolerance_from_context`, given the already-parsed context, so this
package never imports :mod:`cw.cli`); the dispatch gate reads it from the live
client config (:func:`tolerance_for_client`). Both readers return ``None`` for
the strict opt-out, ``review.default_merge_tolerance: false``.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NamedTuple

from cw._git import run_git
from cw.merge_probe import PROBE_TIMEOUT_SECONDS, MergeTreeStatus, merge_tree
from cw.review_convergence._store import LEDGER_RELATIVE_PATH, load_ledger
from cw.review_convergence._tip import finalize_ready

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.review_convergence._models import ConvergenceLedger

#: Wall-clock budget, in seconds, for the merge loop of one
#: :func:`default_merge_rejection` call (the house figure of
#: ``dispatch/branch_freshness``'s ``_SWEEP_BUDGET_SECONDS``). Checked only
#: before each merge: a merge that starts inside it may run past it (up to three
#: git calls of 10 s each), so the worst case is the four prelude git calls plus
#: the budget plus one merge, not the budget. The REVIEW staleness gate asks
#: under ``dev_queue_lock``; once spent, the next merge is
#: ``merge_tree_unavailable`` (fail closed: the tip reads stale).
TIP_CHECK_BUDGET_SECONDS: Final = 30.0

#: Length of the abbreviated sha a rejection reason names.
_SHORT_SHA: Final = 7

#: :func:`default_merge_rejection` reasons. A reason that names a commit is
#: followed by a space and that commit's 7-character sha.
REJECT_TOLERANCE_OFF: Final = "tolerance_off"
REJECT_NO_DEFAULT_REF: Final = "no_default_ref"
REJECT_CLEAN_NOT_ANCESTOR: Final = "clean_not_ancestor"
REJECT_NO_MERGE: Final = "no_merge"
REJECT_GIT_UNREADABLE: Final = "git_unreadable"
REJECT_NON_MERGE_COMMIT: Final = "non_merge_commit"
REJECT_NOT_TWO_PARENT: Final = "not_two_parent"
REJECT_SECOND_PARENT_NOT_ON_DEFAULT: Final = "second_parent_not_on_default"
REJECT_NOT_TEXTUALLY_CLEAN: Final = "not_textually_clean"
REJECT_MERGE_TREE_UNAVAILABLE: Final = "merge_tree_unavailable"

#: Rejections a stale ``refs/remotes/origin/<default>`` can cause. A merge of
#: newer default-branch commits (a human, GitHub's "Update branch") has a
#: second parent the local tracking ref does not contain yet
#: (``second_parent_not_on_default``), and the same newer commits are
#: reachable from the span but not from the stale ref, so they read as the
#: branch's own (``non_merge_commit``, which is checked first and is the usual
#: symptom). A missing tracking ref (a single-branch clone) is
#: ``no_default_ref``. A genuine own commit gets the same answer.
_STALE_DEFAULT_REF_REASONS: Final = (
    REJECT_SECOND_PARENT_NOT_ON_DEFAULT,
    REJECT_NON_MERGE_COMMIT,
    REJECT_NO_DEFAULT_REF,
)


def is_stale_default_ref_symptom(rejection: str) -> bool:
    """Whether a :func:`default_merge_rejection` reason may just mean a stale ref.

    Matches the three reasons in :data:`_STALE_DEFAULT_REF_REASONS`, with or
    without the ``<sha7>`` suffix a commit-naming reason carries; those are the
    only rejections a fetch of ``origin/<default>`` can repair.
    """
    return rejection.startswith(_STALE_DEFAULT_REF_REASONS)


@dataclass(frozen=True)
class MergeTolerance:
    """The default branch a tolerated merge comes from."""

    default_branch: str


def tolerance_from_context(
    context: dict[str, object] | None, review_obj: dict[str, object] | None
) -> MergeTolerance | None:
    """The tolerance a cw-context names, or ``None`` (strict).

    *context* is the parsed ``.claude/cw-context.json`` and *review_obj* its
    ``review`` object (``None`` when unreadable or below schema 15). ``None``
    for either of them, a ``review.default_merge_tolerance`` that is present
    and not exactly ``True`` (``False`` is the opt-out; any other value is
    malformed and never widens the tolerance), or a missing or empty
    ``world_state_snapshot.origin_main_branch``. An absent tolerance key is the
    documented default, tolerant.
    """
    if context is None or review_obj is None:
        return None
    if review_obj.get("default_merge_tolerance", True) is not True:
        return None
    snapshot = context.get("world_state_snapshot")
    branch = snapshot.get("origin_main_branch") if isinstance(snapshot, dict) else None
    if not isinstance(branch, str) or not branch:
        return None
    return MergeTolerance(branch)


def tolerance_for_client(client: ClientConfig) -> MergeTolerance | None:
    """The tolerance the live client config names, or ``None`` (strict).

    The same fields the cw-context producer writes (``default_branch`` and
    ``review.default_merge_tolerance``), read from the cw-owned config rather
    than the worker-writable context file. ``None`` unless the client is in
    clean mode with the tolerance on.
    """
    review = client.review
    if not review.is_clean or review.default_merge_tolerance is not True:
        return None
    return MergeTolerance(client.default_branch)


def git_stdout_or_none(worktree: Path, argv: list[str]) -> str | None:
    """Stdout of ``git *argv``; ``None`` when git cannot run, times out or fails.

    Bounded by :data:`~cw.merge_probe.PROBE_TIMEOUT_SECONDS`: a hung git reads
    as unreadable (strict), never as an unbounded wait under ``dev_queue_lock``.
    """
    try:
        result = run_git(
            argv, cwd=worktree, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def rev_parse_or_none(worktree: Path, ref: str) -> str | None:
    """The sha *ref* resolves to, ``None`` when it does not (or git cannot run)."""
    out = git_stdout_or_none(worktree, ["rev-parse", ref])
    if out is None:
        return None
    return out.strip() or None


_MergeParents = list[tuple[str, list[str]]]
#: ``rev-list --first-parent --parents --merges`` prints
#: ``<merge> <parent1> <parent2> ...``.
_MIN_MERGE_FIELDS = 3


def _merge_parents(worktree: Path, span: str) -> _MergeParents | None:
    """``(merge, parents after the first)`` per branch merge in *span*, or ``None``.

    The one place the "extra parents" of a merge are defined: parent 1 is the
    branch side, every later parent is the side merged in. Only the branch's
    own first-parent chain is listed, so the default branch's own merge
    commits (a ``--no-ff`` PR merge synced in through a sync merge's second
    parent) are not the branch's merges; they stay covered by the own-commit
    and ancestry checks. A line that is not a merge with at least one extra
    parent is unreadable, so the caller stays strict. No merge in *span* is
    the empty list. The list is oldest first, so a rejection names the first
    offending merge.
    """
    argv = ["rev-list", "--reverse", "--first-parent", "--parents", "--merges", span]
    out = git_stdout_or_none(worktree, argv)
    if out is None:
        return None
    merges: _MergeParents = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < _MIN_MERGE_FIELDS:
            return None
        merges.append((fields[0], fields[2:]))
    return merges


def _named(reason: str, sha: str) -> str:
    return f"{reason} {sha[:_SHORT_SHA]}"


def _own_commit_rejection(worktree: Path, span: str, default_sha: str) -> str | None:
    """``non_merge_commit <sha7>`` for the oldest own commit in *span*, else ``None``.

    A non-merge commit reachable from the span's tip but not from the default
    branch is a change after the clean round.
    """
    argv = ["rev-list", "--reverse", "--no-merges", span, f"^{default_sha}"]
    own = git_stdout_or_none(worktree, argv)
    if own is None:
        return REJECT_GIT_UNREADABLE
    commits = own.split()
    return _named(REJECT_NON_MERGE_COMMIT, commits[0]) if commits else None


@dataclass(frozen=True)
class MergeClean:
    """A merge git itself would write: ``merge-tree`` reproduces its tree."""

    merge: str


@dataclass(frozen=True)
class MergeResolution:
    """A default-branch merge whose tree is not the one ``merge-tree`` writes (#2980).

    *conflicted_tree* is the tree ``git merge-tree --write-tree`` wrote for the
    merge's two parents (conflict markers left in when *status* is
    ``conflict``); *resolved_tree* is the merge commit's own tree, ``None``
    when it did not resolve. The difference between the two is exactly what
    the resolver did: a conflict resolution, or a hand edit of a clean merge.
    """

    merge: str
    conflicted_tree: str | None
    resolved_tree: str | None
    status: MergeTreeStatus


#: One merge's outcome: clean, a resolution, or a rejection reason naming it.
MergeOutcome = MergeClean | MergeResolution | str


def classify_merge(
    worktree: Path, merge: str, others: list[str], default_sha: str
) -> MergeOutcome:
    """Whether *merge* is one git would write from the default branch (#2980).

    It must have exactly two parents (else ``not_two_parent <sha7>``), and its
    second parent must be an ancestor of the default branch's tip (else
    ``second_parent_not_on_default <sha7>``). ``git merge-tree --write-tree``
    of its two parents must answer (else ``merge_tree_unavailable <sha7>``).
    When it exits 0 with exactly the merge commit's own tree the merge is a
    :class:`MergeClean`; any other answer is a :class:`MergeResolution`. The
    git calls run in that order and stop at the first rejection.
    """
    if len(others) != 1:
        return _named(REJECT_NOT_TWO_PARENT, merge)
    ancestry = ["merge-base", "--is-ancestor", others[0], default_sha]
    if git_stdout_or_none(worktree, ancestry) is None:
        return _named(REJECT_SECOND_PARENT_NOT_ON_DEFAULT, merge)
    written = merge_tree(worktree, f"{merge}^1", f"{merge}^2")
    if written.status == "unavailable":
        return _named(REJECT_MERGE_TREE_UNAVAILABLE, merge)
    own_tree = rev_parse_or_none(worktree, f"{merge}^{{tree}}")
    if written.status == "clean" and written.tree == own_tree:
        return MergeClean(merge)
    return MergeResolution(merge, written.tree, own_tree, written.status)


class _Span(NamedTuple):
    """The default branch's tip sha, the ``clean..head`` span and its merges."""

    default_sha: str
    span: str
    merges: _MergeParents


def _span_or_rejection(
    worktree: Path, clean_sha: str, head_sha: str, default_branch: str
) -> _Span | str:
    """The span after *clean_sha* when it holds a branch merge, else a reason."""
    ref = f"refs/remotes/origin/{default_branch}^{{commit}}"
    default_sha = rev_parse_or_none(worktree, ref)
    if default_sha is None:
        return REJECT_NO_DEFAULT_REF
    ancestry = ["merge-base", "--is-ancestor", clean_sha, head_sha]
    if git_stdout_or_none(worktree, ancestry) is None:
        return REJECT_CLEAN_NOT_ANCESTOR
    span = f"{clean_sha}..{head_sha}"
    merges = _merge_parents(worktree, span)
    if merges is None:
        return REJECT_GIT_UNREADABLE
    if not merges:
        return REJECT_NO_MERGE
    return _Span(default_sha, span, merges)


def checked_span(
    worktree: Path, clean_sha: str, head_sha: str, default_branch: str
) -> _Span | str:
    """The span after *clean_sha*, or the first span-level rejection (#2980).

    The span checks of :func:`default_merge_rejection`, cheapest first:
    ``no_default_ref``, ``clean_not_ancestor``, ``git_unreadable``,
    ``no_merge``, then ``non_merge_commit <sha7>`` (a commit of the branch's
    own after the clean sha). The per-merge checks are
    :func:`merge_outcomes`.
    """
    found = _span_or_rejection(worktree, clean_sha, head_sha, default_branch)
    if isinstance(found, str):
        return found
    own = _own_commit_rejection(worktree, found.span, found.default_sha)
    if own is not None:
        return own
    return found


def merge_outcomes(
    worktree: Path,
    span: _Span,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[MergeOutcome]:
    """Each merge of *span*'s :func:`classify_merge` outcome, oldest first (#2980).

    Lazy, so a caller that stops at the first outcome it cannot accept makes
    no git call for a later merge. The :data:`TIP_CHECK_BUDGET_SECONDS`
    wall-clock budget (read from *clock*) is checked before each merge: once
    spent, the next merge yields ``merge_tree_unavailable <sha7>`` and the
    iteration ends. It also ends after any rejection string.
    """
    started = clock()
    for merge, others in span.merges:
        if clock() - started >= TIP_CHECK_BUDGET_SECONDS:
            yield _named(REJECT_MERGE_TREE_UNAVAILABLE, merge)
            return
        outcome = classify_merge(worktree, merge, others, span.default_sha)
        yield outcome
        if isinstance(outcome, str):
            return


def default_merge_rejection(
    worktree: Path,
    clean_sha: str,
    head_sha: str,
    tolerance: MergeTolerance | None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Why the move from *clean_sha* to *head_sha* is not tolerated; ``None`` if it is.

    ``None`` *tolerance* is the strict rule: ``tolerance_off`` without a git
    call. Otherwise, cheapest first: ``no_default_ref`` (``origin/<default>``
    does not resolve), ``clean_not_ancestor``, ``no_merge`` (a span with no
    branch merge; a fast-forward is not a merge), ``non_merge_commit <sha7>``
    (the span holds a commit of the branch's own), then per merge, oldest
    first, ``not_two_parent <sha7>``, ``second_parent_not_on_default <sha7>``,
    ``merge_tree_unavailable <sha7>`` (git cannot answer: an error exit, a
    timeout, git older than 2.38) and ``not_textually_clean <sha7>`` (a
    conflict resolution, a hand edit, or a merge git cannot reproduce). Any
    other unreadable git state is ``git_unreadable``. Every git call is bounded
    by :data:`~cw.merge_probe.PROBE_TIMEOUT_SECONDS`, and the per-merge loop
    runs under a :data:`TIP_CHECK_BUDGET_SECONDS` wall-clock budget (read from
    *clock*) checked before each merge, not during one: once spent, the next
    merge is ``merge_tree_unavailable``. The worst case is the four git calls
    before the loop, plus the budget, plus one merge (up to three git calls of
    10 s each), not the budget alone. The
    default branch's tracking ref is not fetched here: a stale ref can only
    reject more merges. The refresh happens in the lock-free CLI caller,
    ``cw.cli.review.converge_tip`` (one bounded fetch, then one re-ask, for
    ``non_merge_commit`` / ``second_parent_not_on_default`` /
    ``no_default_ref`` only); the in-lock
    dispatch gate never fetches.
    """
    if tolerance is None:
        return REJECT_TOLERANCE_OFF
    found = checked_span(worktree, clean_sha, head_sha, tolerance.default_branch)
    if isinstance(found, str):
        return found
    for outcome in merge_outcomes(worktree, found, clock=clock):
        if isinstance(outcome, str):
            return outcome
        if isinstance(outcome, MergeResolution):
            return _named(REJECT_NOT_TEXTUALLY_CLEAN, outcome.merge)
    return None


def default_merge_only(
    worktree: Path,
    clean_sha: str,
    head_sha: str,
    tolerance: MergeTolerance | None,
) -> bool:
    """True when everything after *clean_sha* is a clean default-branch merge (#2979).

    Exactly :func:`default_merge_rejection` returning ``None``. ``None``
    *tolerance* returns ``False`` without a git call, and every unreadable git
    state is ``False`` (strict).
    """
    return default_merge_rejection(worktree, clean_sha, head_sha, tolerance) is None


def converged_across_tolerated_merge(
    ledger: ConvergenceLedger,
    *,
    worktree: Path,
    head_sha: str,
    tolerance: MergeTolerance | None,
) -> bool:
    """True when *ledger* stays converged although HEAD moved past its clean sha.

    The one keep-converged seam ``converge init``, ``converge status`` and the
    REVIEW staleness gate share (#2861). Requires a
    :func:`~cw.review_convergence._tip.finalize_ready` ledger with a clean sha,
    a HEAD that is not that clean sha, and :func:`default_merge_only` over the
    move. Git is consulted only when every ledger condition already holds. The
    rule does not depend on the loop backend (#2979).
    """
    clean = ledger.last_clean_sha
    if not finalize_ready(ledger) or clean is None or head_sha == clean:
        return False
    return default_merge_only(worktree, clean, head_sha, tolerance)


def tolerates_review_move(
    client: ClientConfig | None,
    worktree: Path,
    *,
    reviewed_sha: str,
    head_sha: str,
) -> bool:
    """True when the move from *reviewed_sha* to *head_sha* is a tolerated merge.

    The REVIEW staleness gate's entry point (#2861). Derived by cw at gate
    time from the worktree ledger (``.cw/review-convergence.json``), the
    worktree's git state and the live client config, never from the sentinel.
    ``False`` (strict) for no client, a client not in clean mode, an unreadable
    or missing ledger, a ledger whose ``last_clean_sha`` is not *reviewed_sha*,
    or anything :func:`converged_across_tolerated_merge` refuses.
    """
    if client is None or not client.review.is_clean:
        return False
    try:
        ledger = load_ledger(worktree / LEDGER_RELATIVE_PATH)
    except (OSError, ValueError):
        return False
    if ledger is None or ledger.last_clean_sha != reviewed_sha:
        return False
    return converged_across_tolerated_merge(
        ledger,
        worktree=worktree,
        head_sha=head_sha,
        tolerance=tolerance_for_client(client),
    )
