"""Reconciling a persisted ledger with the world at ``init`` time (#2787).

``converge init`` runs at the start of every REVIEW session, including the
session a requeue resumes after a stop. :func:`reconcile_ledger_for_init`
decides whether the existing ledger is discarded (it belongs to another
ticket, fork point or rewritten branch), reopened as a new epoch (a stop with
new commits), released without a commit (the three operator-resolvable stops),
kept terminal (every other stop), or simply resumed.

The two git-reading functions live here too; they are the package's only git
I/O, alongside the ledger-file I/O confined to ``_store``:
:func:`history_rewritten` (the ``history_rewritten`` input
:func:`reconcile_ledger_for_init` takes) and :func:`worktree_state` (the
``(HEAD, git status)`` snapshot a confirm round compares before and after, #2791
G4). Both were CLI-private; they moved here so core code (the codex-lane
clean-mode gate, the Claude confirm runner) never imports :mod:`cw.cli`.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from cw._git import git_output, run_git
from cw.exceptions import CwError
from cw.review_convergence._models import (
    LEDGER_STATUS_CONVERGED,
    LEDGER_STATUS_IN_PROGRESS,
    LEDGER_STATUS_STOPPED,
    LedgerReset,
    Reconciled,
    StopRelease,
)
from cw.review_convergence._policy import release_cause
from cw.review_convergence._render import one_line
from cw.review_convergence._tip import demote_for_rereview

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_convergence._models import ConvergenceLedger, ReleaseCause

_UNKNOWN_STOP = "unknown"
#: ``git merge-base --is-ancestor`` exit for the confirmed "not an ancestor".
_NOT_ANCESTOR_EXIT = 1


def history_rewritten(
    existing: ConvergenceLedger | None, worktree: Path, head_sha: str
) -> bool:
    """True when the last recorded round's sha is no longer an ancestor of HEAD.

    ``git merge-base --is-ancestor`` exits 1 only for the confirmed "not an
    ancestor" answer, which means the branch history moved under the ledger
    (more review, never a skipped one). Any other nonzero exit (128 for an
    unknown object, a broken repository) is a git failure that says nothing
    about the history, so it raises :class:`CwError` rather than discarding a
    valid ledger. Callers evaluate it only when the ledger's ticket and fork
    point already match (the ownership guard).
    """
    if existing is None or not existing.rounds:
        return False
    last_sha = existing.rounds[-1].reviewed_sha
    try:
        result = run_git(
            ["merge-base", "--is-ancestor", last_sha, head_sha],
            cwd=worktree,
            capture_output=True,
        )
    except OSError as exc:
        msg = f"git merge-base could not run in {worktree}: {exc}"
        raise CwError(msg) from exc
    if result.returncode == 0:
        return False
    if result.returncode == _NOT_ANCESTOR_EXIT:
        return True
    msg = (
        f"git merge-base --is-ancestor failed in {worktree} with exit "
        f"{result.returncode}: {one_line(result.stderr)}"
    )
    raise CwError(msg)


def worktree_state(worktree: Path) -> tuple[str, str]:
    """``(HEAD sha, git status --porcelain)`` of *worktree*, or a ``CwError``.

    Compared before and after a confirm round (``cw review confirm``'s
    whole-call check, and the Claude role runner's per-role check, #2791 G4):
    a difference means something the reviewer launched wrote to the worktree.
    Gitignored paths are invisible to it by construction.
    """
    try:
        head = git_output(["rev-parse", "HEAD"], cwd=worktree).strip()
        status = git_output(["status", "--porcelain"], cwd=worktree)
    except (subprocess.CalledProcessError, OSError) as exc:
        msg = f"Cannot read the git state of worktree '{worktree}': {exc}"
        raise CwError(msg) from exc
    return head, status


def _reset_reason(
    existing: ConvergenceLedger,
    *,
    ticket_id: str,
    fork_point: str,
    history_rewritten: bool,
) -> str | None:
    if existing.ticket_id != ticket_id:
        return "ticket_mismatch"
    if existing.fork_point != fork_point:
        return "fork_point_mismatch"
    if history_rewritten:
        return "history_rewritten"
    return None


def _released(
    ledger: ConvergenceLedger,
    cause: ReleaseCause,
    *,
    head_sha: str,
    now: str,
    new_epoch: bool,
) -> Reconciled:
    """The ledger reopened as ``in_progress`` plus its audit entry.

    A new epoch (``new_commits``) restarts the no-progress window and the
    fix-cycle budget at the current round. A same-tip release keeps the epoch
    and ``fix_cycles_used`` (so a raised cap buys exactly the extra cycles it
    names) and starts a fresh no-progress window instead.
    """
    release = StopRelease(
        at=now,
        round=len(ledger.rounds),
        reason=ledger.stop_reason or _UNKNOWN_STOP,
        cause=cause,
        sha=head_sha,
    )
    update: dict[str, object] = {
        "status": LEDGER_STATUS_IN_PROGRESS,
        "stop_reason": None,
        "stop_identities": [],
        "releases": [*ledger.releases, release],
    }
    if new_epoch:
        update["epoch_start_round"] = len(ledger.rounds)
        update["fix_cycles_used"] = 0
    else:
        update["window_start_round"] = len(ledger.rounds)
    return Reconciled(ledger.model_copy(update=update), None, release)


def _reconcile_stopped(
    ledger: ConvergenceLedger, *, head_sha: str, now: str
) -> Reconciled:
    last_sha = ledger.rounds[-1].reviewed_sha if ledger.rounds else None
    if head_sha != last_sha:
        return _released(
            ledger, "new_commits", head_sha=head_sha, now=now, new_epoch=True
        )
    cause = release_cause(ledger)
    if cause is None:
        return Reconciled(ledger, None, None)
    return _released(ledger, cause, head_sha=head_sha, now=now, new_epoch=False)


def reconcile_ledger_for_init(
    existing: ConvergenceLedger | None,
    *,
    fresh: ConvergenceLedger,
    ticket_id: str,
    fork_point: str,
    history_rewritten: bool,
    head_sha: str,
    max_cycles: int,
    now: str,
) -> Reconciled:
    """Decide what ``init`` does with *existing*; *fresh* is the ledger to start from.

    1. No ledger, or one for another ``ticket_id`` / ``fork_point``, or
       ``history_rewritten`` (the last round's sha is no longer an ancestor of
       HEAD): return *fresh* with a ``reset_reason`` (``None`` when there was
       no ledger).
    2. Otherwise ``max_cycles`` is refreshed from the argument on every path
       below, ``confirm_backend`` and ``confirm_not_independent`` are refreshed
       from *fresh* on every path except a ``converged`` ledger already clean
       at *head_sha* (it records no further round), and ``confirm_failures`` is
       carried unchanged.
    3. A ``converged`` ledger whose *head_sha* is not its ``last_clean_sha`` is
       reopened as ``in_progress`` with no :class:`StopRelease` (it was not a
       stop): a commit landed after the last clean round, so another full
       round runs.
    4. A ``stopped`` ledger on a new tip is reopened as a new epoch
       (``new_commits``).
    5. A ``stopped`` ledger on the SAME tip is released iff
       :func:`~cw.review_convergence.release_cause` is not ``None``; otherwise
       it is returned still stopped (terminal), so ``init`` can re-emit it.
    6. Any other ledger is resumed.
    """
    if existing is None:
        return Reconciled(fresh, None, None)
    reset = _reset_reason(
        existing,
        ticket_id=ticket_id,
        fork_point=fork_point,
        history_rewritten=history_rewritten,
    )
    if reset is not None:
        previous_ledger = existing.model_copy(update={"reset_history": []})
        reset_entry = LedgerReset(
            at=now,
            reason=reset,
            previous_ledger=previous_ledger.model_dump(mode="json"),
        )
        reset_ledger = fresh.model_copy(update={"reset_history": [reset_entry]})
        return Reconciled(reset_ledger, reset, None)
    ledger = existing.model_copy(update={"max_cycles": max_cycles})
    if ledger.status == LEDGER_STATUS_CONVERGED and head_sha == ledger.last_clean_sha:
        return Reconciled(ledger, None, None)
    # A ledger that can still record a round follows the operator's current
    # confirm choice, not the one stamped when it was created (an opt-out set
    # after an outage must be honoured by the requeue).
    ledger = ledger.model_copy(
        update={
            "confirm_backend": fresh.confirm_backend,
            "confirm_not_independent": fresh.confirm_not_independent,
        }
    )
    if ledger.status == LEDGER_STATUS_STOPPED:
        return _reconcile_stopped(ledger, head_sha=head_sha, now=now)
    if ledger.status == LEDGER_STATUS_CONVERGED:
        return Reconciled(demote_for_rereview(ledger), None, None)
    return Reconciled(ledger, None, None)
