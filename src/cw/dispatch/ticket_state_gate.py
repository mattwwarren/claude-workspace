"""Pre-dispatch closed-ticket guard: the lock-free resolve half (#1605).

The incident this closes: a PENDING PLAN/IMPL row whose GitHub issue was
already CLOSED was claimed and a worker spent a whole session on a ticket
nobody wanted any more. This module answers, before any lock is taken, the
one question the claim path needs: **is this PENDING row's issue CLOSED?**
The claim path (``cw.dispatch.claim.screening``) turns a ``defer=False`` hit
into a CANCELLED row and a ``defer=True`` hit into a no-write hold.

Contracts:

* **GitHub only (ADR-0013, R2).** A client whose tracker is not
  ``github-issues`` (Linear, another system, or unset) is never read. Cancel
  is the destructive action, so an unknown tracker is never guessed at.
* **Positive evidence only.** A hit comes from a CLOSED verdict or from an
  explicit "not evaluated this tick" marker (``defer=True``), never from a
  failed read. A read returning None or any state other than ``OPEN`` /
  ``CLOSED`` records a negative attempt, yields no hit, and the row
  dispatches as it would without the guard.
* **Bounded, lock-free reads (R3, ARCHITECTURE 7.16).** At most
  ``_MAX_STATE_READS_PER_TICK`` reads per client per tick, each bounded by
  ``_STATE_READ_TIMEOUT_SECONDS``, and a ``_STATE_READ_BUDGET_SECONDS``
  wall-clock budget. A candidate that needs a read but falls past the cap or
  the budget gets a ``defer=True`` hit: held PENDING, never claimed
  unevaluated (unlike ``pr_gate``'s capped-out candidates, which claim). No
  call here runs under ``sessions_lock``, ``dev_queue_lock`` or any state lock
  (ADR-0019); the cache write takes ``dispatch_state_lock`` only.
* **Cache.** Closed verdicts never expire; open verdicts live for
  ``_OPEN_STATE_TTL_SECONDS``; a failed read backs off for
  ``_FAILED_READ_BACKOFF_SECONDS`` so a persistently failing issue cannot
  monopolise the cap. The read window is least-recently-attempted first,
  then claim order. A row the guard cancelled carries a ``cancelled_at``
  stamp; if it is PENDING again the operator requeued it, and it is left
  alone.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, NamedTuple

from cw.dispatch_state_ticket_state import (
    TicketStateProbe,
    load_ticket_state_probes,
    save_ticket_state_probes,
    ticket_state_key,
)
from cw.gh import fetch_issue_state
from cw.models import QueueItemStatus, Stage
from cw.tracker import TrackerClass, tracker_kind
from cw.worktree import _git_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from cw.models import ClientConfig, DevQueueStore, LaneConfig, TicketTask

_log = logging.getLogger("cw.dispatch")

#: ``cancel_reason`` on the guard's ``dispatch.tick`` event (no row field).
TICKET_CLOSED_CANCEL_REASON: Final = "ticket_closed"

_GH_ISSUE_STATE_CLOSED: Final = "CLOSED"
_GH_ISSUE_STATE_OPEN: Final = "OPEN"
# Internal tuning constants, no external contract (local because
# review_debt_filing/_constants.py is private to that package).
_STATE_READ_TIMEOUT_SECONDS: Final = 5
_MAX_STATE_READS_PER_TICK: Final = 10
_STATE_READ_BUDGET_SECONDS: Final = 25.0
_OPEN_STATE_TTL_SECONDS: Final = 300
_FAILED_READ_BACKOFF_SECONDS: Final = 90
_GATED_STAGES: Final = frozenset({Stage.PLAN, Stage.IMPL})


class ClosedTicketHit(NamedTuple):
    """A guard verdict for one PENDING row, keyed by ticket id in the result.

    ``defer=False``: the issue is CLOSED, so the claim cancels the row.
    ``defer=True``: not evaluated this tick (read cap or budget), so the claim
    holds the row PENDING with no write. ``created_at`` is the row identity
    the claim re-checks under the lock.
    """

    created_at: datetime
    defer: bool


def claimable_lane_names(
    effective_lanes: Sequence[LaneConfig], running_by_lane: Mapping[str, int]
) -> frozenset[str]:
    """Lanes that can claim this tick: not paused and with a free slot.

    Mirrors the per-lane grant arithmetic in ``_dispatch_client_lanes``, so a
    row in a lane that cannot reach the claim is never read or deferred.
    """
    return frozenset(
        lane.name
        for lane in effective_lanes
        if not lane.paused and lane.max_parallel - running_by_lane.get(lane.name, 0) > 0
    )


def _row_key(task: TicketTask) -> str:
    return ticket_state_key(task.client, task.ticket_id, task.created_at)


def _is_candidate(task: TicketTask, client_name: str, lanes: Collection[str]) -> bool:
    return (
        task.client == client_name
        and task.status == QueueItemStatus.PENDING
        and task.stage in _GATED_STAGES
        and task.adopted_pr_url is None
        and task.pr_url is None
        and task.ticket_id.isdecimal()
        and task.lane in lanes
    )


def _candidates(
    client_name: str,
    queue_snapshot: DevQueueStore,
    *,
    claimable_lanes: Collection[str],
    priority_ticket_ids: Sequence[str],
    cache: Mapping[str, TicketStateProbe],
) -> list[TicketTask]:
    """Guard candidates in claim order, one per ticket id.

    Priority ids first (list order), then ``(-priority, created_at)``, which is
    the order ``_claim_next_pending`` visits rows. A row carrying a
    ``cancelled_at`` stamp was requeued by the operator and is skipped.
    """
    eligible = [
        task
        for task in queue_snapshot.tasks
        if _is_candidate(task, client_name, claimable_lanes)
        and ((probe := cache.get(_row_key(task))) is None or probe.cancelled_at is None)
    ]
    ranked = sorted(eligible, key=lambda t: (-t.priority, t.created_at))
    ordered = [
        task for tid in priority_ticket_ids for task in ranked if task.ticket_id == tid
    ]
    ordered.extend(task for task in ranked if task.ticket_id not in priority_ticket_ids)
    seen: set[str] = set()
    unique: list[TicketTask] = []
    for task in ordered:
        if task.ticket_id not in seen:
            seen.add(task.ticket_id)
            unique.append(task)
    return unique


def _age_seconds(now: datetime, then: datetime | None) -> float | None:
    return None if then is None else (now - then).total_seconds()


def _needs_read(probe: TicketStateProbe | None, now: datetime) -> bool:
    """False for a fresh OPEN verdict or a failed read still in backoff."""
    if probe is None:
        return True
    open_age = _age_seconds(now, probe.checked_at)
    if open_age is not None and open_age < _OPEN_STATE_TTL_SECONDS:
        return False
    failed_age = _age_seconds(now, probe.attempted_at)
    return failed_age is None or failed_age >= _FAILED_READ_BACKOFF_SECONDS


def _last_attempt(probe: TicketStateProbe | None) -> datetime:
    """When this row was last read (any outcome); never-read sorts first."""
    stamps = [] if probe is None else [probe.checked_at, probe.attempted_at]
    return max(
        (s for s in stamps if s is not None), default=datetime.min.replace(tzinfo=UTC)
    )


def _read_state(
    client: ClientConfig,
    task: TicketTask,
    prior: TicketStateProbe | None,
    now: datetime,
) -> TicketStateProbe:
    """Read one issue's state; a non-verdict is a negative attempt, never a hit."""
    state = fetch_issue_state(
        task.ticket_id, _STATE_READ_TIMEOUT_SECONDS, cwd=_git_dir(client)
    )
    if state == _GH_ISSUE_STATE_CLOSED:
        return TicketStateProbe(checked_at=now, closed=True)
    if state == _GH_ISSUE_STATE_OPEN:
        return TicketStateProbe(checked_at=now, closed=False)
    _log.info(
        "dispatch: closed-ticket guard could not read %s/%s (state=%s);"
        " dispatching as usual, retry after %ds",
        client.name,
        task.ticket_id,
        state,
        _FAILED_READ_BACKOFF_SECONDS,
    )
    return TicketStateProbe(
        checked_at=None if prior is None else prior.checked_at,
        closed=False,
        attempted_at=now,
    )


class _ReadBudget:
    """The per-client, per-tick read cap plus wall-clock deadline."""

    def __init__(self, clock: Callable[[], float], budget_seconds: float) -> None:
        self.clock = clock
        self.budget_seconds = budget_seconds
        self.deadline = clock() + budget_seconds
        self.reads = 0

    def take(self) -> bool:
        """Consume one read slot, or False when the cap or deadline is hit."""
        if self.reads >= _MAX_STATE_READS_PER_TICK or self.clock() >= self.deadline:
            return False
        self.reads += 1
        return True


def resolve_closed_ticket_hits(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    claimable_lanes: Collection[str],
    priority_ticket_ids: Sequence[str] | None = None,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
    budget_seconds: float = _STATE_READ_BUDGET_SECONDS,
) -> dict[str, ClosedTicketHit]:
    """Ticket id -> guard hit for *client*'s PENDING PLAN/IMPL candidates.

    Runs lock-free (see the module docstring). *now*, *clock* and
    *budget_seconds* are injectable for tests; production callers omit them.
    """
    # ADR-0013 / R2: only a GitHub tracker is read. OTHER (Linear, ...) and
    # UNSET do nothing; there is no way to read their state without guessing.
    if tracker_kind(client.workspace_path).tracker_class is not TrackerClass.GITHUB:
        return {}
    resolved_now = now if now is not None else datetime.now(UTC)
    cache = load_ticket_state_probes()
    candidates = _candidates(
        client.name,
        queue_snapshot,
        claimable_lanes=claimable_lanes,
        priority_ticket_ids=priority_ticket_ids or [],
        cache=cache,
    )
    hits: dict[str, ClosedTicketHit] = {}
    to_read: list[tuple[datetime, int, TicketTask]] = []
    for index, task in enumerate(candidates):
        probe = cache.get(_row_key(task))
        if probe is not None and probe.closed:
            hits[task.ticket_id] = ClosedTicketHit(task.created_at, defer=False)
        elif _needs_read(probe, resolved_now):
            to_read.append((_last_attempt(probe), index, task))
    to_read.sort(key=lambda item: (item[0], item[1]))
    new_entries = _read_window(
        client,
        [task for _, _, task in to_read],
        cache,
        hits,
        resolved_now,
        _ReadBudget(clock, budget_seconds),
    )
    keep = {_row_key(t) for t in queue_snapshot.tasks if t.client == client.name}
    prefix = f"{client.name}/"
    if new_entries or any(k.startswith(prefix) and k not in keep for k in cache):
        save_ticket_state_probes(client.name, new_entries, prune_to_keys=keep)
    return hits


def _read_window(
    client: ClientConfig,
    to_read: Sequence[TicketTask],
    cache: Mapping[str, TicketStateProbe],
    hits: dict[str, ClosedTicketHit],
    now: datetime,
    budget: _ReadBudget,
) -> dict[str, TicketStateProbe]:
    """Read *to_read* in order within *budget*; defer the rest into *hits*.

    Every read, successful or not, consumes one slot, and one failed read does
    not stop the rest (a short-circuit would let one failing issue starve
    every later row).
    """
    new_entries: dict[str, TicketStateProbe] = {}
    deferred = 0
    for task in to_read:
        if not budget.take():
            hits[task.ticket_id] = ClosedTicketHit(task.created_at, defer=True)
            deferred += 1
            continue
        key = _row_key(task)
        entry = _read_state(client, task, cache.get(key), now)
        new_entries[key] = entry
        if entry.closed:
            hits[task.ticket_id] = ClosedTicketHit(task.created_at, defer=False)
    if deferred:
        _log.info(
            "dispatch: closed-ticket guard deferred %d candidate(s) for %s past its"
            " per-tick read cap (%d) or budget (%ss); held PENDING, read next tick",
            deferred,
            client.name,
            _MAX_STATE_READS_PER_TICK,
            budget.budget_seconds,
        )
    return new_entries
