"""Claim-time large-tier review-approval gate (#2773).

Finalize Step 4.0 parks a ``large``-tier ticket whose review approval is
missing or stale, but only once a finalize worker is already running. This
gate applies the same predicate (:mod:`cw.review_approval`, the one behind
``cw review check-approval``) at claim time, so no worker is started for a row
Step 4.0 would only park. It is the dispatch-side backstop; the worker fence
stays authoritative for tiers the row cannot show.

Precomputed lock-free once per client per tick from :mod:`cw.dispatch.lanes`
(the claim path runs under ``dev_queue_lock`` and does no I/O), carried in
``_PreClaimScreens`` and turned into a park by
``cw.dispatch.claim.screening``, on the adopted-head-drift pattern.

Which rows: PENDING, ``Stage.FINALIZE``, and ``large`` on the row
(:func:`cw.review_approval.resolve_row_scope_tier`: ``scope_hint`` or the
stamped ``computed_scope_tier``). A row whose tier the row cannot show is NOT
gated here (the worker resolves the tier from the plan marker or the diff,
which the row-only resolver cannot see; Step 4.0 still fails closed on it) and
logs one warning. HEAD is the branch ref in the client git dir, falling back to
``refs/remotes/origin/<branch>`` only when the local ref is absent.

A git timeout or error is a *deferral*, not a park: the row stays PENDING and
the next tick re-reads. After
:data:`REVIEW_APPROVAL_MAX_DEFERRED_TICKS` consecutive transient ticks the hold
becomes a real ``unreadable`` park so an operator is paged. Per tick at most
:data:`REVIEW_APPROVAL_MAX_PROBES_PER_TICK` rows are probed; the rest are
deferred (never claimed unevaluated), and the first transient verdict of a tick
defers every remaining candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cw.auto_dev_result import SCOPE_TIER_LARGE
from cw.merge_probe import PROBE_TIMEOUT_SECONDS
from cw.models import QueueItemStatus, Stage, resolve_feature_branch
from cw.review_approval import (
    APPROVAL_UNREADABLE,
    ApprovalInputs,
    ApprovalVerdict,
    evaluate_review_approval,
    resolve_commit,
    resolve_row_scope_tier,
)
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, DevQueueStore, TicketTask

_log = logging.getLogger("cw.dispatch")

#: Max rows one tick probes per client; the rest are deferred a tick.
REVIEW_APPROVAL_MAX_PROBES_PER_TICK: Final = 8
#: Consecutive transient ticks after which a deferral becomes a real park.
REVIEW_APPROVAL_MAX_DEFERRED_TICKS: Final = 5

_TierKey = tuple[str, str]
# Process-lifetime state; tests reset it. ``_DEFER_STREAK`` counts consecutive
# transient ticks per (client, ticket); ``_WARNED_DEFER`` and
# ``_WARNED_TIER_UNKNOWN`` are warn-once maps (the dispatch loop re-resolves
# every tick, so an unchanged condition would otherwise log every tick). All
# three are pruned of rows no longer scanned.
_DEFER_STREAK: dict[_TierKey, int] = {}
_WARNED_DEFER: dict[_TierKey, str] = {}
_WARNED_TIER_UNKNOWN: dict[_TierKey, str] = {}

_CAP_DEFER_DETAIL = "deferred: per-tick probe cap reached or a git fault this tick"


@dataclass(frozen=True)
class ReviewApprovalHold:
    """One large-tier row the claim must not start a worker for this tick.

    ``approver_kind``, ``approved_sha`` and ``approved_at_set`` are the row's
    approval fields at snapshot time, so the claim can ignore a hit the row
    outran. ``defer`` holds the claim without a park (row stays PENDING).
    """

    branch: str
    head_sha: str | None
    token: str
    detail: str
    approver_kind: str | None
    approved_sha: str | None
    approved_at_set: bool
    defer: bool


def _hold(
    task: TicketTask,
    branch: str,
    head_sha: str | None,
    verdict: ApprovalVerdict,
    *,
    defer: bool,
) -> ReviewApprovalHold:
    kind = task.review_approver_kind
    return ReviewApprovalHold(
        branch=branch,
        head_sha=head_sha,
        token=verdict.token,
        detail=verdict.detail,
        approver_kind=None if kind is None else str(kind),
        approved_sha=task.review_approved_sha,
        approved_at_set=task.review_approved_at is not None,
        defer=defer,
    )


def _candidates(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> tuple[list[TicketTask], set[_TierKey]]:
    """Explicit-``large`` PENDING FINALIZE rows, and every key the scan saw.

    A row whose row-only tier is None logs its warn-once warning here; small
    rows cost nothing.
    """
    candidates: list[TicketTask] = []
    seen: set[_TierKey] = set()
    for task in queue_snapshot.tasks:
        if (
            task.client != client.name
            or task.status != QueueItemStatus.PENDING
            or task.stage != Stage.FINALIZE
        ):
            continue
        key = (client.name, task.ticket_id)
        tier = resolve_row_scope_tier(task)
        if tier == SCOPE_TIER_LARGE:
            candidates.append(task)
            seen.add(key)
        elif tier is None:
            seen.add(key)
            _warn_tier_unknown_once(key)
    return candidates, seen


def _warn_tier_unknown_once(key: _TierKey) -> None:
    if key in _WARNED_TIER_UNKNOWN:
        return
    _WARNED_TIER_UNKNOWN[key] = "tier_unknown"
    _log.warning(
        "dispatch: review-approval gate not gating %s/%s: scope tier unresolved"
        " on the row; Step 4.0 in the worker still applies",
        key[0],
        key[1],
    )


def _head_of(repo: Path, branch: str) -> tuple[str | None, bool]:
    """``(head sha, exec_failed)``: the local branch ref, else ``origin/<branch>``."""
    local = resolve_commit(repo, f"refs/heads/{branch}", timeout=PROBE_TIMEOUT_SECONDS)
    if local.sha is not None or local.exec_failed:
        return local.sha, local.exec_failed
    remote = resolve_commit(
        repo, f"refs/remotes/origin/{branch}", timeout=PROBE_TIMEOUT_SECONDS
    )
    return remote.sha, remote.exec_failed


def _evaluate(
    client: ClientConfig, task: TicketTask, branch: str
) -> tuple[ApprovalVerdict, str | None]:
    """The verdict for one row, plus the head it judged (None when unread)."""
    repo = _git_dir(client)
    head_sha, exec_failed = _head_of(repo, branch)
    if exec_failed:
        verdict = ApprovalVerdict(
            APPROVAL_UNREADABLE, f"git could not read branch {branch}", transient=True
        )
        return verdict, None
    # Channel parity with cw-context: it shows no approval when approved_at is
    # None, so a row with a kind but no approved_at reads as no approval.
    kind = task.review_approver_kind if task.review_approved_at is not None else None
    inputs = ApprovalInputs(
        tier=SCOPE_TIER_LARGE,
        approver_kind=None if kind is None else str(kind),
        approved_sha=task.review_approved_sha,
        default_branch=client.default_branch,
    )
    verdict = evaluate_review_approval(
        inputs, repo=repo, head_sha=head_sha, timeout=PROBE_TIMEOUT_SECONDS
    )
    return verdict, head_sha


def _transient_hold(
    key: _TierKey, task: TicketTask, branch: str, verdict: ApprovalVerdict
) -> ReviewApprovalHold:
    """The hold for a transient fault: a deferral, then a park at the streak cap."""
    streak = _DEFER_STREAK.get(key, 0) + 1
    _DEFER_STREAK[key] = streak
    if streak >= REVIEW_APPROVAL_MAX_DEFERRED_TICKS:
        return _hold(task, branch, None, verdict, defer=False)
    if _WARNED_DEFER.get(key) != verdict.detail:
        _WARNED_DEFER[key] = verdict.detail
        _log.warning(
            "dispatch: review-approval gate deferring %s/%s: %s (tick %d of %d)",
            key[0],
            key[1],
            verdict.detail,
            streak,
            REVIEW_APPROVAL_MAX_DEFERRED_TICKS,
        )
    return _hold(task, branch, None, verdict, defer=True)


def _prune(client_name: str, seen: set[_TierKey]) -> None:
    """Drop this client's state for rows the scan no longer sees."""
    for state in (_DEFER_STREAK, _WARNED_DEFER, _WARNED_TIER_UNKNOWN):
        for key in [k for k in state if k[0] == client_name and k not in seen]:
            del state[key]


def resolve_review_approval_holds(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> dict[str, ReviewApprovalHold]:
    """Ticket id -> hold for each large PENDING FINALIZE row not to be claimed.

    Lock-free: all git runs here, never under ``dev_queue_lock``. A row with a
    valid approval (or any tier other than an explicit ``large``) has no entry.
    """
    candidates, seen = _candidates(client, queue_snapshot)
    _prune(client.name, seen)
    holds: dict[str, ReviewApprovalHold] = {}
    stalled = False
    deferred = 0
    for index, task in enumerate(candidates):
        key = (client.name, task.ticket_id)
        branch = resolve_feature_branch(task.ticket_id, client, task)
        if stalled or index >= REVIEW_APPROVAL_MAX_PROBES_PER_TICK:
            cap = ApprovalVerdict(APPROVAL_UNREADABLE, _CAP_DEFER_DETAIL, True)
            holds[task.ticket_id] = _hold(task, branch, None, cap, defer=True)
            deferred += 1
            continue
        verdict, head_sha = _evaluate(client, task, branch)
        if verdict.passed:
            _DEFER_STREAK.pop(key, None)
            _WARNED_DEFER.pop(key, None)
            continue
        if verdict.transient:
            stalled = True
            hold = _transient_hold(key, task, branch, verdict)
            deferred += hold.defer
            holds[task.ticket_id] = hold
            continue
        _DEFER_STREAK.pop(key, None)
        _WARNED_DEFER.pop(key, None)
        holds[task.ticket_id] = _hold(task, branch, head_sha, verdict, defer=False)
    _log_summary(client.name, holds, deferred)
    return holds


def _log_summary(
    client_name: str, holds: dict[str, ReviewApprovalHold], deferred: int
) -> None:
    if deferred:
        _log.info(
            "dispatch: review-approval gate deferred %d claim(s) for %s",
            deferred,
            client_name,
        )
    parked = [t for t, hold in holds.items() if not hold.defer]
    if parked:
        _log.warning(
            "dispatch: review-approval gate holds %d task(s) for %s: %s",
            len(parked),
            client_name,
            sorted(parked),
        )
