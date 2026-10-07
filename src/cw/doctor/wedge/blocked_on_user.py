"""BLOCKED_ON_USER wedge detectors and remedies for ``cw doctor`` (classes 5, 7).

The dead-session BLOCKED_ON_USER detector (class-5) and the #2100
terminal_sibling duplicate-park detector (class-7), their shared predicates,
and the two queue mutators the reap applies for them:
:func:`_collapse_blocked_on_user_tasks` (also reached from
``loop_health._reap_session_by_selector`` through a deferred import of the
package) and :func:`_cancel_terminal_sibling_parks`. The only submodule that
logs; ``_log`` is bound to the pinned :data:`_LOGGER_NAME`. Imports
``_constants``. Split out of the flat ``doctor/wedge.py`` (#2164).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.config import state_file
from cw.dev_queue import transition_task_status
from cw.doctor._shared import WedgeFinding
from cw.doctor.wedge._constants import (
    _DIRTY_WORKTREE_DISPOSITION,
    _HUMAN_GATED_PARK_DISPOSITIONS,
    _LOGGER_NAME,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_TERMINAL_SIBLING,
)
from cw.models import QueueItemStatus, ReapReason
from cw.native_daemon import get_native_daemon_client

if TYPE_CHECKING:
    from cw.models import CwState, DevQueueStore, Session, TicketTask

_log = logging.getLogger(_LOGGER_NAME)


def _is_dead_session_task(
    task: TicketTask,
    session_by_id: dict[str, Session],
    live_short_ids: set[str],
) -> bool:
    """Return True when a BLOCKED_ON_USER task's session is dead.

    Dead = session_id is None (dirty-worktree / gh-blocked phantom paths),
    OR session not in state, OR surface_ref is None or absent from the live
    daemon roster. Covers all three BLOCKED_ON_USER creation paths (see #590).
    """
    if task.session_id is None:
        return True
    session = session_by_id.get(task.session_id)
    if session is None:
        return True
    if session.surface_ref is None:
        return True
    return session.surface_ref not in live_short_ids


def _is_terminal_sibling_disposition(task: TicketTask) -> bool:
    """True iff *task* carries the terminal_sibling park disposition (#2100).

    The BROAD exclusion — disposition alone, regardless of ``attempts`` or
    ``session_id`` — used by both
    :func:`_check_wedge_dead_session_blocked_on_user` and
    :func:`_collapse_blocked_on_user_tasks` to keep either from ever
    reverting a terminal_sibling row to PENDING: ``park_terminal_sibling_tasks``
    re-parks purely on disposition (a terminal sibling existing for the same
    (client, ticket_id)), not on ``attempts``, so ANY terminal_sibling row
    reverted to PENDING gets re-parked on the very next reconcile pass
    regardless of its claim history. Contrast :func:`_is_terminal_sibling_park`
    below — the NARROWER shape (``session_id is None`` + ``attempts == 0``)
    that is additionally safe to auto-CANCEL under ``--reap``.
    """
    return task.disposition == ReapReason.TERMINAL_SIBLING.value


def _check_wedge_dead_session_blocked_on_user(
    state: CwState,
    queue: DevQueueStore,
) -> list[WedgeFinding]:
    """Detect BLOCKED_ON_USER tasks whose sessions are dead (OOM/crash path).

    Guards daemon I/O: list_live_session_short_ids() is only called when at
    least one BLOCKED_ON_USER task exists in the queue.

    Human-gated parks (disposition in _HUMAN_GATED_PARK_DISPOSITIONS) are
    excluded outright (#1653): their worker exited by design, so they always
    look "dead" to this heuristic, but they are waiting on an operator, not
    wedged — reverting them re-runs the identical park.

    A terminal_sibling park (#2100) is likewise excluded outright: it too
    always has session_id is None (park_terminal_sibling_tasks clears it) and
    so always looks "dead" to this heuristic, but reverting ANY terminal_sibling
    row to PENDING just gets it re-parked terminal_sibling on the next
    reconcile pass (see _is_terminal_sibling_disposition) — its remedy is
    CANCEL, and only for the narrower reap-eligible shape (see
    _WEDGE_TERMINAL_SIBLING/_check_wedge_terminal_sibling_park).
    """
    candidates = [
        t
        for t in queue.tasks
        if t.status == QueueItemStatus.BLOCKED_ON_USER
        and t.disposition not in _HUMAN_GATED_PARK_DISPOSITIONS
        and t.disposition != _DIRTY_WORKTREE_DISPOSITION
        and not _is_terminal_sibling_disposition(t)
    ]
    if not candidates:
        return []

    session_by_id = {s.id: s for s in state.sessions}
    live_short_ids = get_native_daemon_client().list_live_session_short_ids()

    findings: list[WedgeFinding] = []
    for task in candidates:
        if not _is_dead_session_task(task, session_by_id, live_short_ids):
            continue
        findings.append(
            WedgeFinding(
                wedge_class=_WEDGE_BLOCKED_DEAD_SESSION,
                session_id=task.session_id,
                ticket_id=task.ticket_id,
                recipe=(
                    "BLOCKED_ON_USER task with dead session holds lane slot. "
                    "Run: cw doctor --reap to revert task to PENDING."
                ),
                state_file=str(state_file()),
            )
        )
    return findings


def _is_terminal_sibling_park(task: TicketTask) -> bool:
    """True iff *task* is a #2100 terminal_sibling duplicate park.

    BLOCKED_ON_USER + disposition == ReapReason.TERMINAL_SIBLING +
    session_id is None + attempts == 0 — exactly the shape
    ``park_terminal_sibling_tasks`` (``cw.reconcile.tasks``) stamps: a row
    ``add_ticket`` minted during a lock-contention race, never claimed
    (attempts == 0, ``ever_spawned=False`` at construction — see
    ``cw.reconcile.review_recipes.auto_fix_ci``), for a ticket whose real row
    already reached COMPLETED or CANCELLED. The ``attempts == 0`` guard
    matters: a row with claim history is not this narrow duplicate shape and
    is left to the existing dead-session wedge class
    (``_check_wedge_dead_session_blocked_on_user``) instead. Shared by the
    class-7 detector and its ``--reap`` remedy so the two can never select a
    different row set.
    """
    return (
        task.status == QueueItemStatus.BLOCKED_ON_USER
        and task.disposition == ReapReason.TERMINAL_SIBLING.value
        and task.session_id is None
        and task.attempts == 0
    )


def _check_wedge_terminal_sibling_park(queue: DevQueueStore) -> list[WedgeFinding]:
    """Detect #2100 terminal_sibling duplicate parks holding a lane slot.

    Distinct from class-5 (``_check_wedge_dead_session_blocked_on_user``,
    which excludes this disposition outright): that class's remedy is to
    revert the row to PENDING, but a terminal_sibling park has nothing to
    revert TO — the ticket's real row already finished — so reverting it
    just gets it re-parked terminal_sibling on the very next reconcile pass
    (a silent ping-pong; see ``_cancel_terminal_sibling_parks`` for the
    correct CANCEL remedy). Needs no daemon/session lookup, unlike class-5:
    ``_is_terminal_sibling_park`` is a pure predicate over the row itself.
    """
    return [
        WedgeFinding(
            wedge_class=_WEDGE_TERMINAL_SIBLING,
            session_id=None,
            ticket_id=task.ticket_id,
            recipe=(
                "BLOCKED_ON_USER task parked terminal_sibling holds a lane"
                " slot for a ticket whose real row already finished. Run:"
                " cw doctor --reap to cancel this duplicate row."
            ),
            state_file=str(state_file()),
        )
        for task in queue.tasks
        if _is_terminal_sibling_park(task)
    ]


def _collapse_blocked_on_user_tasks(
    queue: DevQueueStore,
    blocked_ticket_ids: set[str],
) -> bool:
    """Revert oldest BLOCKED_ON_USER task to PENDING; cancel duplicates.

    For each ticket_id in blocked_ticket_ids, sorts BLOCKED_ON_USER tasks by
    created_at (ascending), reverts the first (oldest) to PENDING with
    session_id cleared, and cancels the rest. Skips the whole ticket
    (no mutation) when the oldest task already has ``pr_url`` set — see
    the inline comment at the guard for why.

    Human-gated parks are never touched (#1653): rows whose disposition is in
    _HUMAN_GATED_PARK_DISPOSITIONS are filtered out before any mutation, as
    defense in depth behind the class-5 detector's own exclusion — this
    helper is also reached from loop_health._reap_session_by_selector, whose
    callers select tickets by other criteria.

    A terminal_sibling park (#2100) is excluded the same way, for the same
    defense-in-depth reason — reverting one to PENDING here would just get it
    re-parked terminal_sibling on the next reconcile pass, the exact
    ping-pong #2100 reports; its correct --reap remedy is
    ``_cancel_terminal_sibling_parks`` instead. This is what stops the
    ping-pong even for a caller (loop_health._reap_session_by_selector) that
    never routes through the class-7 detector at all.

    Returns True when any mutation was applied.
    """
    changed = False
    for ticket_id in blocked_ticket_ids:
        all_blocked = [
            t
            for t in queue.tasks
            if t.ticket_id == ticket_id and t.status == QueueItemStatus.BLOCKED_ON_USER
        ]
        tasks_for_ticket = [
            t
            for t in all_blocked
            if t.disposition not in _HUMAN_GATED_PARK_DISPOSITIONS
            and t.disposition != _DIRTY_WORKTREE_DISPOSITION
            and not _is_terminal_sibling_disposition(t)
        ]
        if len(tasks_for_ticket) < len(all_blocked):
            _log.warning(
                "Ticket %s: %d BLOCKED_ON_USER row(s) parked on a human gate "
                "or terminal_sibling (#2100) left untouched by collapse; "
                "release via requeue/approve/a gate recipe, or "
                "cw doctor --reap for a terminal_sibling row.",
                ticket_id,
                len(all_blocked) - len(tasks_for_ticket),
            )
        if not tasks_for_ticket:
            continue
        # Stable sort preserves insertion order for equal created_at values.
        tasks_for_ticket.sort(key=lambda t: t.created_at)
        oldest = tasks_for_ticket[0]
        # Why: reverting a task that already has a pr_url clears it and
        # re-enables dispatch, which re-runs FINALIZE against a branch that
        # may already be merged — producing a duplicate/empty PR (#912).
        if oldest.pr_url:
            _log.warning(
                "Skipping _collapse_blocked_on_user_tasks for ticket %s: "
                "oldest BLOCKED_ON_USER task has pr_url set (%s). "
                "Will not revert to PENDING.",
                ticket_id,
                oldest.pr_url,
            )
            continue
        transition_task_status(oldest, QueueItemStatus.PENDING)
        oldest.session_id = None
        changed = True
        for dup in tasks_for_ticket[1:]:
            transition_task_status(dup, QueueItemStatus.CANCELLED)
            changed = True
    return changed


def _cancel_terminal_sibling_parks(queue: DevQueueStore, ticket_ids: set[str]) -> bool:
    """CANCEL every BLOCKED_ON_USER terminal_sibling park for *ticket_ids* (#2100).

    Deliberately NOT a reuse of ``_collapse_blocked_on_user_tasks``: that
    helper reverts the OLDEST blocked row of a ticket to PENDING because
    there IS a legitimate park to recover — a terminal_sibling park has
    nothing to recover, since the ticket's real row already reached
    COMPLETED/CANCELLED, so reverting it to PENDING would just get it
    re-parked terminal_sibling on the very next reconcile pass (the exact
    ping-pong #2100 reports). Every matching row is CANCELLED outright
    instead. Matches via ``_is_terminal_sibling_park`` — the same predicate
    the class-7 detector uses — so this can never touch a ticket's real,
    non-duplicate row, even when *ticket_ids* also names one.
    """
    changed = False
    for task in queue.tasks:
        if task.ticket_id in ticket_ids and _is_terminal_sibling_park(task):
            transition_task_status(task, QueueItemStatus.CANCELLED)
            changed = True
    return changed
