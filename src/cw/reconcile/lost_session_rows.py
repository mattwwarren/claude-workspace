"""Park RUNNING rows whose session record is missing; page sessionless ones (#2715).

Two RUNNING-row shapes no other sweep owns:

- **Record missing.** The row is bound to a ``session_id`` that resolves to no
  ``Session``. ``dispatch.claim.spawn._handle_post_launch_failure`` keeps the
  row RUNNING when the ``sessions.json`` write failed after the worker
  launched (#2502); the leaked-worker sweep then stops the unrecorded worker,
  and the row stays RUNNING, holding its lane slot, forever. This sweep parks
  it BLOCKED_ON_USER with disposition ``running_row_session_lost``. It keeps
  ``session_id`` as evidence (so a parked row can never look sessionless),
  does not charge ``unproductive_attempts`` (a missing record is an
  infrastructure fault), and pages once after the queue write. The
  disposition escalates after ``ESCALATION_PARK_MINUTES`` and is never
  auto-requeued; ``cw dev-queue requeue`` releases it.
- **Sessionless.** The row has no ``session_id`` at all past
  ``SPAWN_GRACE_SECONDS`` since its claim (a claim of unknown age counts).
  It is paged once, latched in ``TicketTask.advisory_note``, and never parked:
  the claim-to-bind window can exceed the grace, and the unowned-running
  sweep adopts the row once the evidence ties (#2591).

Mass-park guards: when ``sessions.json`` holds no session at all (missing or
wiped) and more than ``SESSION_LOST_PARKS_MAX_PER_TICK`` bound rows would
park, nothing is parked and one WARNING is logged. Otherwise at most that many
rows are parked per tick, in queue order; the rest wait for later ticks.

Constructive evidence acts (ADR-0006 amendment #2715): not gated by
``reap_policy``. The spawn grace only delays a page (ADR-0014). Runs in
``_run_terminal_backstops_and_sweeps`` under ``sessions_lock``, after the
adoption sweep and the terminal backstops; nests ``dev_queue_lock`` for each
write (ADR-0019 order) and records its pages after that lock releases. No
push notification, matching the #2524 stranded-session page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, NamedTuple

from cw.config import load_state
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.queue_rows import _find_running_row, _is_backstop_exempt
from cw.reconcile._shared import (
    _SESSION_LOST_REASON,
    SPAWN_GRACE_SECONDS,
    find_live_sessions_for_ticket,
    resolve_session_for_task,
)
from cw.reconcile.escalation import ESCALATION_PARK_MINUTES

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from cw.models import ClientConfig, CwState, TicketTask

_log = logging.getLogger(__name__)

# Most record-missing rows parked in one tick; the rest wait for later ticks.
SESSION_LOST_PARKS_MAX_PER_TICK = 3
# ``paused_status`` of the sessionless page.
SESSIONLESS_PAGE_REASON = "running_row_no_session"
# The sessionless page-once latch in ``TicketTask.advisory_note``. The leading
# ``?`` matches the session-mismatch advisory's ``?session_mismatch``.
NO_SESSION_ADVISORY_NOTE = "?no_session"


@dataclass(frozen=True)
class LostRow:
    """One RUNNING row this sweep acts on, with its identity at detect time.

    ``created_at``, ``attempts``, ``claimed_at`` and ``session_id`` are
    re-checked under ``dev_queue_lock`` before any write.
    """

    ticket_id: str
    client: str
    lane: str
    created_at: datetime
    attempts: int
    claimed_at: datetime | None
    session_id: str | None


class LostSessionRows(NamedTuple):
    """:func:`find_lost_session_rows`'s result.

    ``parks``: bound rows whose session record is missing, in queue order.
    ``sessionless``: unbound rows past the spawn grace and not yet paged.
    ``wiped``: no session is recorded at all and more than
    ``SESSION_LOST_PARKS_MAX_PER_TICK`` rows would park.
    """

    parks: list[LostRow]
    sessionless: list[LostRow]
    wiped: bool


def _lost_row(task: TicketTask) -> LostRow:
    return LostRow(
        ticket_id=task.ticket_id,
        client=task.client,
        lane=task.lane,
        created_at=task.created_at,
        attempts=task.attempts,
        claimed_at=task.claimed_at,
        session_id=task.session_id,
    )


def _past_spawn_grace(task: TicketTask, now: datetime) -> bool:
    """True when *task*'s claim is at least ``SPAWN_GRACE_SECONDS`` old.

    A claim with no ``claimed_at`` (claimed before schema v44) is of unknown
    age and counts. A naive ``claimed_at`` fails closed rather than raising
    on the aware/naive subtraction.
    """
    claimed_at = task.claimed_at
    if claimed_at is None:
        return True
    if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
        _log.debug(
            "lost_session_rows: %s/%s not paged: naive claimed_at %s",
            task.client,
            task.ticket_id,
            claimed_at,
        )
        return False
    return now - claimed_at >= timedelta(seconds=SPAWN_GRACE_SECONDS)


def find_lost_session_rows(
    state: CwState,
    tasks: Iterable[TicketTask],
    clients: dict[str, ClientConfig],
    *,
    now: datetime,
) -> LostSessionRows:
    """Classify every in-scope, non-exempt RUNNING row. Pure.

    A bound row whose session resolves is left to the phantom sweep and the
    terminal backstops; a sessionless row already latched is left alone.
    """
    parks: list[LostRow] = []
    sessionless: list[LostRow] = []
    for task in tasks:
        if (
            task.status is not QueueItemStatus.RUNNING
            or task.client not in clients
            or _is_backstop_exempt(task)
        ):
            continue
        if task.session_id is not None:
            if resolve_session_for_task(task, state) is None:
                parks.append(_lost_row(task))
        elif task.advisory_note != NO_SESSION_ADVISORY_NOTE and _past_spawn_grace(
            task, now
        ):
            sessionless.append(_lost_row(task))
    wiped = not state.sessions and len(parks) > SESSION_LOST_PARKS_MAX_PER_TICK
    return LostSessionRows(parks=parks, sessionless=sessionless, wiped=wiped)


def _same_claim(task: TicketTask, row: LostRow) -> bool:
    return (
        task.attempts == row.attempts
        and task.claimed_at == row.claimed_at
        and task.session_id == row.session_id
    )


def _emit_page(payload: dict[str, object], *, ticket_id: str) -> bool:
    """Record one ``session.needs_attention``; True when it landed.

    A failed inbox write is logged, never raised: it must not abort the tick.
    """
    try:
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            payload,
            correlation_id=ticket_id,
        )
    except (CwError, OSError):
        _log.warning(
            "lost_session_rows: %s page for %s not written",
            payload.get("paused_status"),
            ticket_id,
            exc_info=True,
        )
        return False
    return True


def _page_payload(
    row: LostRow, *, session_id: str, paused_status: str, breadcrumbs: str
) -> dict[str, object]:
    """The nine canonical ``session.needs_attention`` keys for *row*.

    There is no Session to name, so ``session_name`` is ``""`` and
    ``claude_session_id`` is None.
    """
    return {
        "session_id": session_id,
        "session_name": "",
        "client": row.client,
        "ticket_id": row.ticket_id,
        "claude_session_id": None,
        "paused_status": paused_status,
        "breadcrumbs": breadcrumbs,
        "crashed": False,
        "lane": row.lane,
    }


def _park_breadcrumbs(row: LostRow) -> str:
    return (
        f"ticket {row.ticket_id}'s RUNNING row was bound to session "
        f"{row.session_id}, which has no record in sessions.json (the record "
        "was never written or was lost). Parked BLOCKED_ON_USER "
        f"({_SESSION_LOST_REASON}); nothing was deleted and the row keeps the "
        "missing session id as evidence. The leaked-worker sweep stops any live "
        "worker that has no cw session. Verify the ticket's worktree holds no "
        "unsaved work, then release it with: cw dev-queue requeue "
        f"{row.ticket_id} -c {row.client}. Escalates after "
        f"{ESCALATION_PARK_MINUTES} minutes if unattended."
    )


def _park_lost_rows(rows: list[LostRow]) -> list[LostRow]:
    """Park each row still RUNNING on the same claim; return those parked.

    One ``dev_queue_lock`` hold for the re-check, the transitions (each emits
    its ``task.transition`` inline) and the save. ``pr_url`` is passed
    through because the seam overwrites it on a BLOCKED_ON_USER transition.
    """
    parked: list[LostRow] = []
    with dev_queue_lock():
        store = load_dev_queue()
        for row in rows:
            task = _find_running_row(
                store, row.ticket_id, row.client, created_at=row.created_at
            )
            if task is None or not _same_claim(task, row) or _is_backstop_exempt(task):
                _log.info(
                    "lost_session_rows: %s/%s changed since detect; not parked",
                    row.client,
                    row.ticket_id,
                )
                continue
            transition_task_status(
                task,
                QueueItemStatus.BLOCKED_ON_USER,
                disposition=_SESSION_LOST_REASON,
                blocked_reason=(
                    f"bound session {row.session_id} has no record in sessions.json"
                ),
                pr_url=task.pr_url,
                unproductive=False,
            )
            parked.append(row)
        if parked:
            save_dev_queue(store)
    return parked


def _act_on_parks(found: LostSessionRows) -> list[LostRow]:
    """Apply the wiped-state guard and the per-tick cap, park, then page."""
    if found.wiped:
        _log.warning(
            "lost_session_rows: %d RUNNING rows are bound to sessions with no"
            " record and sessions.json holds no session at all; parking none"
            " (wiped-state guard)",
            len(found.parks),
        )
        return []
    batch = found.parks[:SESSION_LOST_PARKS_MAX_PER_TICK]
    waiting = len(found.parks) - len(batch)
    if waiting:
        _log.info(
            "lost_session_rows: per-tick park cap reached; %d row(s) wait a tick",
            waiting,
        )
    parked = _park_lost_rows(batch) if batch else []
    for row in parked:
        _log.info(
            "lost_session_rows: parked %s/%s (%s): bound session %s has no record",
            row.client,
            row.ticket_id,
            _SESSION_LOST_REASON,
            row.session_id,
        )
        _emit_page(
            _page_payload(
                row,
                session_id=row.session_id or "",
                paused_status=_SESSION_LOST_REASON,
                breadcrumbs=_park_breadcrumbs(row),
            ),
            ticket_id=row.ticket_id,
        )
    return parked


def _sessionless_breadcrumbs(
    row: LostRow, *, now: datetime, live_names: list[str]
) -> str:
    age = (
        f"{(now - row.claimed_at).total_seconds():.0f}s"
        if row.claimed_at is not None
        else "an unknown time (claimed before schema v44)"
    )
    text = (
        f"ticket {row.ticket_id}'s RUNNING row (attempt {row.attempts}) has no "
        f"session bound {age} after its claim. Nothing was changed. Live sessions "
        f"for this ticket: {', '.join(live_names) or 'none'}. A slow worktree "
        "phase or an unstamped worker can look like this; reconcile adopts the "
        "row once the evidence ties (#2591). Check cw list and cw dev-queue tasks."
    )
    if live_names:
        return text
    return (
        f"{text} If no worker is live for this ticket, cw doctor --reap (class 2) "
        "releases the row -- only after confirming no worker is live for this "
        "ticket."
    )


def _page_sessionless(
    row: LostRow, *, state: CwState, native_live: set[str], now: datetime
) -> bool:
    """Page one sessionless row, then latch it; True when latched.

    Page first, latch second (at-least-once, the #2524 rule): a failed page
    leaves the row unlatched so the next tick retries, and a row whose claim
    changed since detect is not latched.
    """
    live = find_live_sessions_for_ticket(state, row.ticket_id, row.client, native_live)
    payload = _page_payload(
        row,
        session_id=f"no-session:{row.client}/{row.ticket_id}#{row.attempts}",
        paused_status=SESSIONLESS_PAGE_REASON,
        breadcrumbs=_sessionless_breadcrumbs(
            row, now=now, live_names=[f"{s.id} ({s.name})" for s in live]
        ),
    )
    if not _emit_page(payload, ticket_id=row.ticket_id):
        return False
    with dev_queue_lock():
        store = load_dev_queue()
        task = _find_running_row(
            store, row.ticket_id, row.client, created_at=row.created_at
        )
        if task is None or not _same_claim(task, row):
            _log.info(
                "lost_session_rows: %s/%s changed after its page; not latched",
                row.client,
                row.ticket_id,
            )
            return False
        task.advisory_note = NO_SESSION_ADVISORY_NOTE
        save_dev_queue(store)
    return True


def run_lost_session_recovery(
    *, now: datetime, clients: dict[str, ClientConfig], native_live: set[str]
) -> list[str]:
    """Park record-missing rows and page sessionless ones. Returns parked tickets.

    *clients* is the tick's client scope (empty: a no-op); *native_live* is
    the tick's roster, used only to name live same-ticket sessions on a page.
    Reads state and the queue fresh.
    """
    if not clients:
        return []
    state = load_state()
    found = find_lost_session_rows(state, load_dev_queue().tasks, clients, now=now)
    parked = _act_on_parks(found)
    for row in found.sessionless:
        _page_sessionless(row, state=state, native_live=native_live, now=now)
    return [row.ticket_id for row in parked]
