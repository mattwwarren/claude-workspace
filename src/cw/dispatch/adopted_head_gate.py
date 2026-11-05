"""Adopted-PR head drift gate and session-end pin settle (RFC 0016, #2943).

An adopted row (``TicketTask.adopted_head_sha`` set) is always reviewed at its
pinned head. Two lock-free halves keep that invariant, both run once per
client per dispatch tick from :mod:`cw.dispatch.lanes`, in this order:

1. :func:`settle_adopted_pins` -- the read half of the session-end re-pin.
   ``transition_task_status`` stamps ``adopted_repin_due`` on every RUNNING
   exit of an adopted row (a pure in-memory write; the stamp point is reached
   under ``sessions_lock``, where ADR-0019 forbids a subprocess). This step
   reads the origin head, fetches the branch and decides ancestry with NO lock
   held, then calls :func:`cw.dev_queue.adoption.apply_settled_pin`, a brief
   compare-and-set under ``dev_queue_lock``. The pin advances only when the
   old pin is an ancestor of the observed head; a non-descendant head or a git
   error keeps the pin (and the scan below then parks). An unreadable origin
   head defers: the flag stays set and the next tick retries.
2. :func:`resolve_adopted_head_drifts` -- the precompute the claim path turns
   into an ``adopted_head_drift`` park, on the
   ``pr_gate.resolve_stale_pr_ticket_ids`` pattern. It compares each PENDING
   adopted row's origin head with its (settle-overlaid) pin and fails open on
   an unreadable head (A13): no park on a signal it cannot read.

Residual (documented in the runbook): a foreign push that descends from the
pin and lands during a cw session, or in the one-tick gap before the next
settle, is absorbed by the settle; a push after the settle parks.
"""

from __future__ import annotations

import logging
import subprocess
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from cw._git import commit_range_summary, is_ancestor
from cw.dev_queue import load_dev_queue
from cw.dev_queue.adoption import apply_settled_pin
from cw.gh import branch_head_sha_on_origin
from cw.models import QueueItemStatus, resolve_feature_branch
from cw.worktree import (
    FetchOutcome,
    TickFetchGate,
    _git_dir,
    active_tick_fetch,
    fetch_feature_branch,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from cw.models import ClientConfig, DevQueueStore, TicketTask

_log = logging.getLogger("cw.dispatch")

#: Max flagged rows one tick settles per client; the rest wait a tick.
ADOPT_SETTLE_MAX_PER_TICK: Final = 8
#: Max origin-head probes the drift scan makes per client per tick.
ADOPT_DRIFT_MAX_PROBES_PER_TICK: Final = 8
# Adoption's explicit fetches also run outside the dispatch tick (notably from
# the operator requeue command), so give them the same bounded subprocess
# contract instead of relying on the tick-scoped gate being active.
ADOPTED_FETCH_TIMEOUT_SECONDS: Final = 60.0


@dataclass(frozen=True)
class SettleResult:
    """What :func:`settle_adopted_pins` did this tick, for the same-tick scan.

    ``advanced`` maps a ticket id to its new pin; ``deferred`` names rows whose
    flag stayed set (origin unreadable); ``settled`` names rows whose flag was
    cleared this tick, whether or not the pin moved.
    """

    advanced: dict[str, str] = field(default_factory=dict)
    deferred: frozenset[str] = frozenset()
    settled: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AdoptedHeadDrift:
    """One adopted row whose origin head moved off its pin."""

    branch: str
    pinned: str
    observed: str
    commit_range: str


def fetch_adopted_branch(client: ClientConfig, branch: str) -> None:
    """Fetch *branch* from origin so ancestry sees a foreign push's objects.

    Bounded and best-effort: a failure leaves ancestry to answer ``None``,
    which every caller treats as fail-closed for the pin. Also run by
    ``cw dev-queue requeue --repin-adopted-head`` before its locked re-pin.
    """
    try:
        fetch_gate = active_tick_fetch()
        fetch_context: AbstractContextManager[TickFetchGate]
        if fetch_gate is None:
            fetch_gate = TickFetchGate(
                warned=set(),
                per_call_s=ADOPTED_FETCH_TIMEOUT_SECONDS,
                budget_s=ADOPTED_FETCH_TIMEOUT_SECONDS,
            )
            fetch_context = fetch_gate.tick()
        else:
            fetch_context = nullcontext(fetch_gate)
        with fetch_context:
            result = fetch_feature_branch(client, branch)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("adopted_pin_fetch_failed branch=%s error=%s", branch, exc)
        return
    if result.outcome is not FetchOutcome.FETCHED:
        _log.warning(
            "adopted_pin_fetch_failed branch=%s outcome=%s reason=%s",
            branch,
            result.outcome.value,
            result.reason or "unknown",
        )


def _flagged_rows(
    client: ClientConfig, store: DevQueueStore
) -> list[tuple[TicketTask, str]]:
    """``(row, pin)`` for *client*'s flagged adopted rows in *store*."""
    return [
        (task, task.adopted_head_sha)
        for task in store.tasks
        if task.client == client.name
        and task.adopted_head_sha is not None
        and task.adopted_repin_due
    ]


def _settle_one(
    client: ClientConfig, task: TicketTask, pin: str
) -> tuple[str, bool] | None:
    """Settle one flagged row; return its new pin and whether CAS succeeded."""
    branch = resolve_feature_branch(task.ticket_id, client, task)
    repo = _git_dir(client)
    observed, _gh_available = branch_head_sha_on_origin(branch, cwd=repo)
    if observed is None:
        _log.warning(
            "adopted_pin_settle_deferred ticket=%s client=%s branch=%s:"
            " origin head unreadable; retrying next tick",
            task.ticket_id,
            client.name,
            branch,
        )
        return None
    new_pin = pin
    ancestry_result = "same"
    if observed != pin:
        fetch_adopted_branch(client, branch)
        if is_ancestor(repo, pin, observed) is True:
            new_pin = observed
            ancestry_result = "descendant"
        else:
            ancestry_result = "not_descendant_or_unknown"
            _log.warning(
                "adopted_pin_settle_kept ticket=%s client=%s pinned=%s"
                " observed=%s: not a descendant (or git error); the drift gate"
                " parks it",
                task.ticket_id,
                client.name,
                pin,
                observed,
            )
    applied = apply_settled_pin(
        client.name,
        task.ticket_id,
        expected_old=pin,
        new_pin=new_pin,
        observed_sha=observed,
        ancestry_result=ancestry_result,
    )
    if not applied:
        _log.info(
            "adopted_pin_settle_cas_lost ticket=%s client=%s pinned=%s",
            task.ticket_id,
            client.name,
            pin,
        )
        return (pin, False)
    if new_pin != pin:
        _log.info(
            "adopted_pin_settled ticket=%s client=%s old=%s new=%s observed=%s",
            task.ticket_id,
            client.name,
            pin,
            new_pin,
            observed,
        )
    return new_pin, True


def settle_adopted_pins(
    client: ClientConfig, *, queue_snapshot: DevQueueStore | None = None
) -> SettleResult:
    """Run the session-end re-pin for *client*'s flagged adopted rows.

    When the tick's *queue_snapshot* shows no flagged adopted row for
    *client*, returns at once without a second queue read: a row flagged
    after the snapshot is safe until the next tick, because the claim ignores
    a drift hit on a flagged row under its lock. Otherwise it loads the queue
    lock-free (a fresh read), takes up to
    :data:`ADOPT_SETTLE_MAX_PER_TICK` rows of any status with
    ``adopted_repin_due`` set, and settles each one. Every gh and git call
    happens with no lock held; the only write is
    :func:`~cw.dev_queue.adoption.apply_settled_pin`'s brief compare-and-set.
    Runs even when the client has no free slot, so a saturated client still
    settles.
    """
    advanced: dict[str, str] = {}
    deferred: set[str] = set()
    settled: set[str] = set()
    if queue_snapshot is not None and not _flagged_rows(client, queue_snapshot):
        return SettleResult()
    flagged = _flagged_rows(client, load_dev_queue())
    for task, pin in flagged[:ADOPT_SETTLE_MAX_PER_TICK]:
        settled_pin = _settle_one(client, task, pin)
        if settled_pin is None:
            deferred.add(task.ticket_id)
            continue
        new_pin, applied = settled_pin
        if not applied:
            deferred.add(task.ticket_id)
            continue
        settled.add(task.ticket_id)
        if new_pin != pin:
            advanced[task.ticket_id] = new_pin
    return SettleResult(
        advanced=advanced, deferred=frozenset(deferred), settled=frozenset(settled)
    )


def _scan_candidates(
    client: ClientConfig, queue_snapshot: DevQueueStore, settle: SettleResult
) -> list[tuple[TicketTask, str]]:
    """``(row, effective pin)`` for the PENDING adopted rows the scan judges.

    The tick-start snapshot can be staler than the settle's fresh load, so the
    skip is keyed on *settle*: a deferred row, or one still flagged and not
    settled this tick, is skipped (its pin is about to move, A16). The
    effective pin is ``settle.advanced`` overlaid on the snapshot's.
    """
    return [
        (task, settle.advanced.get(task.ticket_id, task.adopted_head_sha))
        for task in queue_snapshot.tasks
        if task.client == client.name
        and task.status == QueueItemStatus.PENDING
        and task.adopted_head_sha is not None
        and task.ticket_id not in settle.deferred
        and not (task.adopted_repin_due and task.ticket_id not in settle.settled)
    ]


def resolve_adopted_head_drifts(
    client: ClientConfig, queue_snapshot: DevQueueStore, settle: SettleResult
) -> dict[str, AdoptedHeadDrift]:
    """Ticket ids whose adopted PR's origin head moved off the pin.

    An unreadable head fails open (no hit). On a mismatch the
    branch is fetched (bounded, no lock) and the commit range summarized for
    the park's attention event. Probes are capped at
    :data:`ADOPT_DRIFT_MAX_PROBES_PER_TICK`; the rest wait a tick.
    """
    drifts: dict[str, AdoptedHeadDrift] = {}
    repo = _git_dir(client)
    candidates = _scan_candidates(client, queue_snapshot, settle)
    for task, pinned in candidates[:ADOPT_DRIFT_MAX_PROBES_PER_TICK]:
        branch = resolve_feature_branch(task.ticket_id, client, task)
        observed, _gh_available = branch_head_sha_on_origin(branch, cwd=repo)
        if observed is None or observed == pinned:
            continue
        fetch_adopted_branch(client, branch)
        drifts[task.ticket_id] = AdoptedHeadDrift(
            branch=branch,
            pinned=pinned,
            observed=observed,
            commit_range=commit_range_summary(repo, pinned, observed),
        )
    if drifts:
        _log.warning(
            "dispatch: adopted-head drift gate holds %d task(s) for %s: %s",
            len(drifts),
            client.name,
            sorted(drifts),
        )
    return drifts
