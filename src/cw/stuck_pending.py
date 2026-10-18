"""Which PENDING tickets are stuck: blocked by something that will not clear itself.

Shared by ``cw orchestrate watch`` (a ``STUCK`` stream line) and ``cw
dev-queue wait`` (exit 5), both computed watcher-side from the read-only
ticket view (:func:`cw.ticket_view.build_ticket_view`); no event type is
emitted (#2693).

Every ``DispatchSkipReason`` is classified, and a test pins that
:data:`NEEDS_ACTION_SKIP_REASONS` and :data:`SELF_CLEARING_SKIP_REASONS`
partition the enum, so a new reason fails until someone decides which side it
is on.

- **Needs action** (a person must act): a lane circuit-breaker pause with no
  auto-resume scheduled, the ssh-key gate and the disk-pressure gate. Plus
  the synthetic ``lane_paused`` (an operator, legacy or ``clients.yaml``
  pause, which dispatch reports no skip reason for) and, ``wait`` only, the
  synthetic ``handoff_unrouted``.
- **Self-clearing** (never alerts): caps, backoff, usage limit, availability
  and freshness gates, an occupied worktree, spawn errors. Also the three
  codes dispatch only reports while parking the row BLOCKED_ON_USER with a
  needs_attention page -- ``attempt_cap_blocked``, ``stale_pr_blocked`` and
  ``tracker_mcp_gate_blocked`` -- since ``wait`` already exits 2 on such a
  park and a PENDING row carries the code only briefly (before the park) or
  from a stale per-ticket tick after a requeue.

Freshness: the ssh-key and disk-pressure gates are re-emitted on every
dispatch tick while they hold (client-wide ``dispatch.tick``), so an
event-sourced code older than ``TICK_STALE_SECONDS`` is ignored as outlived.
Known limitation: the ticket view prefers a ticket's latest per-ticket tick
over the client snapshot, so an old per-ticket tick for a ticket (for example
a ``worktree_occupied``) hides a fresh client-wide gate until it ages out of
the inbox.

Age is measured by the caller on a monotonic clock (:class:`PendingTracker`
takes ``at`` in monotonic seconds): ``TicketTask`` has no pending-since field,
so the timer starts when this process first observes the row in that state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw.auto_dev_result import SCOPE_GATED_APPROVAL_STATUSES
from cw.dev_queue.next_commands import LANE_PAUSED_SKIP_CODE
from cw.exceptions import CwError
from cw.lane_pause import LanePauseSource
from cw.models import DispatchSkipReason, QueueItemStatus
from cw.models.ticket_view import DispatchSource
from cw.reconcile.fix_dispatch import FIX_LOOP_PENDING_DISPATCH
from cw.ticket_view import ViewSources, build_ticket_view

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult
    from cw.lane_pause import LanePauseView
    from cw.models import TicketTask
    from cw.models.ticket_view import TicketView

STUCK_PENDING_MINUTES = 15
HANDOFF_STUCK_SECONDS = 300
HANDOFF_UNROUTED_CODE = "handoff_unrouted"
SECONDS_PER_MINUTE = 60

NEEDS_ACTION_SKIP_REASONS: frozenset[str] = frozenset(
    {
        DispatchSkipReason.LANE_CIRCUIT_PAUSED.value,
        DispatchSkipReason.SSH_KEY_GATE.value,
        DispatchSkipReason.DISK_PRESSURE_GATE.value,
    }
)
SELF_CLEARING_SKIP_REASONS: frozenset[str] = frozenset(
    {
        DispatchSkipReason.ATTEMPT_CAP_BLOCKED.value,
        DispatchSkipReason.STALE_PR_BLOCKED.value,
        DispatchSkipReason.TRACKER_MCP_GATE_BLOCKED.value,
        DispatchSkipReason.CAP_FULL.value,
        DispatchSkipReason.LANE_CAP_BLOCKED.value,
        DispatchSkipReason.HOST_CAPACITY_GATED.value,
        DispatchSkipReason.USAGE_LIMITED.value,
        DispatchSkipReason.AVAILABILITY_GATE.value,
        DispatchSkipReason.FRESHNESS_GATE.value,
        DispatchSkipReason.WORKTREE_OCCUPIED.value,
        DispatchSkipReason.SPAWN_ERROR.value,
        DispatchSkipReason.SPAWN_ERROR_BACKOFF.value,
        DispatchSkipReason.NO_PENDING.value,
        DispatchSkipReason.NONE.value,
    }
)

# The event-sourced gates: client-wide dispatch.tick codes, re-emitted every
# tick while the gate holds, so a stale one has outlived its condition.
_GATE_DETAILS: dict[str, str] = {
    DispatchSkipReason.SSH_KEY_GATE.value: (
        "no unlocked SSH key for client {client}; dispatch is held"
    ),
    DispatchSkipReason.DISK_PRESSURE_GATE.value: (
        "worktree disk is nearly full; dispatch is held"
    ),
}
_UNKNOWN_TRIGGER = "unknown"
_EVENT_SOURCES = frozenset({DispatchSource.EVENT, DispatchSource.LAST_TICK})


class PendingBlock(NamedTuple):
    """Why a ticket is stuck, and the ``cw`` command that clears it (if any).

    *command* is the lane's ``cw lane resume`` for the two lane-pause codes
    and None for every other code (no ``cw`` remedy).
    """

    code: str
    detail: str
    command: str | None


class PendingProbe(NamedTuple):
    """One PENDING row's ticket view and its stuck classification."""

    view: TicketView
    block: PendingBlock | None


def _lane_pause_block(pause: LanePauseView) -> PendingBlock | None:
    if pause.source == LanePauseSource.CIRCUIT_BREAKER:
        if pause.auto_resume_at is not None:
            return None
        trigger = pause.trigger.value if pause.trigger is not None else _UNKNOWN_TRIGGER
        return PendingBlock(
            code=DispatchSkipReason.LANE_CIRCUIT_PAUSED.value,
            detail=(
                f"lane {pause.lane} circuit breaker tripped ({trigger});"
                " no auto-resume is scheduled"
            ),
            command=pause.resume_command,
        )
    return PendingBlock(
        code=LANE_PAUSED_SKIP_CODE,
        detail=(
            f"lane {pause.lane} is paused ({pause.source.value}); resume it explicitly"
        ),
        command=pause.resume_command,
    )


def classify_pending(view: TicketView) -> PendingBlock | None:
    """The needs-action block holding *view*'s PENDING row, else None.

    A paused lane is judged from its live pause view (a breaker pause with an
    auto-resume scheduled clears itself). Otherwise only a fresh ssh-key or
    disk-pressure gate counts; see the module docstring for freshness.
    """
    dispatch = view.dispatch
    if view.status != QueueItemStatus.PENDING or dispatch is None:
        return None
    if dispatch.lane_paused and dispatch.lane_pause is not None:
        return _lane_pause_block(dispatch.lane_pause)
    template = _GATE_DETAILS.get(dispatch.skip_reason or "")
    if template is None:
        return None
    if dispatch.source in _EVENT_SOURCES and dispatch.stale:
        return None
    return PendingBlock(
        code=str(dispatch.skip_reason),
        detail=template.format(client=view.client),
        command=None,
    )


def handoff_unrouted_block(status: str, seconds: int) -> PendingBlock:
    """The ``wait`` block for a recorded hand-off result dispatch has not routed."""
    return PendingBlock(
        code=HANDOFF_UNROUTED_CODE,
        detail=(
            f"dispatch has not routed the recorded {status} result for {seconds}s;"
            " is `cw dev-queue run` alive?"
        ),
        command=None,
    )


def probe_pending(
    ticket_id: str, client: str, *, now: datetime, sources: ViewSources
) -> PendingProbe | None:
    """*ticket_id*'s view and classification; None when its row has vanished."""
    try:
        view = build_ticket_view(ticket_id, client, now=now, sources=sources)
    except CwError:
        return None
    return PendingProbe(view=view, block=classify_pending(view))


def scan_pending(rows: Iterable[TicketTask], *, now: datetime) -> list[PendingProbe]:
    """Probe every PENDING row in *rows* with one shared :class:`ViewSources`.

    Rows that vanished, or are no longer PENDING when re-read, are skipped.
    """
    sources = ViewSources()
    probes: list[PendingProbe] = []
    for row in rows:
        if row.status != QueueItemStatus.PENDING:
            continue
        probe = probe_pending(row.ticket_id, row.client, now=now, sources=sources)
        if probe is not None and probe.view.status == QueueItemStatus.PENDING:
            probes.append(probe)
    return probes


class PendingTracker:
    """Fire once per (key, code) after a row has been stuck *threshold_seconds*.

    The timer starts at the first observation of a key in a code; a code
    change restarts it (and re-arms the alert); a None block or
    :meth:`retain` dropping the key forgets it.
    """

    def __init__(self, threshold_seconds: float) -> None:
        self._threshold = threshold_seconds
        self._since: dict[str, tuple[str, float]] = {}
        self._fired: set[str] = set()

    def observe(self, key: str, block: PendingBlock | None, at: float) -> float | None:
        """Record *block* for *key* at monotonic *at*; the age when it alerts."""
        if block is None:
            self._forget(key)
            return None
        seen = self._since.get(key)
        if seen is None or seen[0] != block.code:
            self._fired.discard(key)
            self._since[key] = (block.code, at)
            seen = self._since[key]
        age = at - seen[1]
        if age < self._threshold or key in self._fired:
            return None
        self._fired.add(key)
        return age

    def retain(self, keys: set[str]) -> None:
        """Forget every key not in *keys* (its row left PENDING or scope)."""
        for key in set(self._since) - keys:
            self._forget(key)

    def _forget(self, key: str) -> None:
        self._since.pop(key, None)
        self._fired.discard(key)


def is_internal_handoff(sentinel: AutoDevResult) -> bool:
    """True iff *sentinel* is dispatch's own routine routing, not an outcome.

    A ``blocked`` result whose blocker is the fix-loop hand-off (dispatch
    leaves the row RUNNING), or a plan/review approval result (dispatch moves
    the row off RUNNING once it routes it). Uses the same constants dispatch
    branches on (``cw.dispatch.routing``).
    """
    if sentinel.status in SCOPE_GATED_APPROVAL_STATUSES:
        return True
    return (
        sentinel.status == "blocked"
        and sentinel.blocker is not None
        and sentinel.blocker.reason == FIX_LOOP_PENDING_DISPATCH
    )
