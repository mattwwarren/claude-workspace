"""Reconciling a persisted ledger with the world at ``init`` time (#2787).

``converge init`` runs at the start of every REVIEW session, including the
session a requeue resumes after a stop. :func:`reconcile_ledger_for_init`
decides whether the existing ledger is discarded (it belongs to another
ticket, fork point or rewritten branch), reopened as a new epoch (a stop with
new commits), released without a commit (the three operator-resolvable stops),
kept terminal (every other stop), or simply resumed.

Import these names from :mod:`cw.review_convergence`, not from this private
submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_convergence._models import LedgerReset, Reconciled, StopRelease
from cw.review_convergence._policy import release_cause
from cw.review_convergence._tip import demote_for_rereview

if TYPE_CHECKING:
    from cw.review_convergence._models import ConvergenceLedger, ReleaseCause

_UNKNOWN_STOP = "unknown"


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
        "status": "in_progress",
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
       below, and ``confirm_failures`` is carried unchanged.
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
        reset_entry = LedgerReset(
            at=now,
            reason=reset,
            previous_ledger=existing.model_dump(mode="json"),
        )
        reset_ledger = fresh.model_copy(update={"reset_history": [reset_entry]})
        return Reconciled(reset_ledger, reset, None)
    ledger = existing.model_copy(update={"max_cycles": max_cycles})
    if ledger.status == "stopped":
        return _reconcile_stopped(ledger, head_sha=head_sha, now=now)
    if ledger.status == "converged" and head_sha != ledger.last_clean_sha:
        return Reconciled(demote_for_rereview(ledger), None, None)
    return Reconciled(ledger, None, None)
