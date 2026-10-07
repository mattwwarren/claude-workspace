"""Adopt RUNNING rows whose launched session was never stamped on them (#2591).

Dispatch claims a row to RUNNING, launches a worker, and only then stamps the
session id onto the row (``cw.dispatch.claim.claimed_row._stamp_spawn_success``).
When that stamp fails twice (#2502), the worker keeps running but the row stays
RUNNING with no ``session_id``. Nothing could then route its completion: the
dispatch consumer and the Stop-hook router both look a row up by the session
id, so the row stuck RUNNING, holding its lane slot, until an operator
released it by hand.

This sweep binds such a row to its session when the evidence ties the two
together, and does nothing otherwise. The evidence is the worktree's
``cw-context.json``, which the spawn writes before the worker exists
(``cw.spawn._write_hook_context``). A row is adopted only when all of these hold:

- it is RUNNING, unbound, not backstop-exempt, the only RUNNING row for its
  (client, ticket), and its client is in this tick's client scope;
- the context's ``attempt`` equals the row's ``attempts`` (``attempts`` moves
  only at claim) and its ``ticket_id`` and ``client`` are the row's;
- the context's ``session_id`` is a recorded Session that no other row owns,
  of the same client, whose name parses to the same ticket;
- that Session did not start before the row's ``claimed_at``. A Session that
  did belongs to an earlier row for the same ticket, whose context survived a
  pre-launch crash of this claim. A naive ``started_at`` or ``claimed_at``
  fails closed rather than raising. A row with no ``claimed_at`` (claimed
  before schema v44) has no timing evidence and skips this check.

Adoption writes exactly the fields dispatch's own stamp writes
(``_apply_spawn_success_fields``) and leaves ``stage_base_ref`` alone, so no
git runs. It is constructive, the same class as the emitted-sentinel router:
no revert, stop, park or removal, so it is not gated by ``reap_policy``
(ADR-0006) and is not timer-driven (ADR-0014). It applies to any Session
status and any backend, since the codex preflight writes the same context.

Not covered here (the #2591 follow-up): a row whose Session was never recorded
(the ``sessions.json`` write failed), a row bound to an absent Session, and
any signal for a row that cannot be tied. Each such row is left untouched. A
live unrecorded worker is still stopped by the leaked-worker sweep, never
adopted.

Runs from ``cw.reconcile.core._run_terminal_backstops_and_sweeps``, under
``reconcile()``'s ``sessions_lock``, before the TIMED_OUT backstop. It reads
state and the queue fresh, nests ``dev_queue_lock`` for each bind (ADR-0019
order), persists a durable audit outbox marker before each bind, and drains
that marker after releasing the queue lock. A failed queue write leaves the
row available for the next tick; a failed audit write leaves the marker for a
later reconcile tick, so a durable bind is never silently unaudited.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from cw._hook_context import _read_cw_context
from cw.atomic import atomic_write_text
from cw.config import events_dir, load_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType, QueueItemStatus
from cw.reconcile._shared import ticket_id_for_session
from cw.worktree import worktree_path_for

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        Session,
        TicketTask,
    )

_log = logging.getLogger(__name__)
_ADOPTION_OUTBOX_NAME = "task-session-adopted.outbox.json"


@dataclass(frozen=True)
class UnownedCandidate:
    """One unbound RUNNING row and the recorded session its claim launched.

    ``created_at``, ``attempts`` and ``claimed_at`` are the row's identity at
    detect time; the act re-checks all three under ``dev_queue_lock`` before
    binding.
    """

    ticket_id: str
    client: str
    lane: str
    created_at: datetime
    attempts: int
    claimed_at: datetime | None
    session_id: str
    session_name: str


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _session_postdates_claim(session: Session, row: TicketTask) -> bool:
    """True unless *session* started before *row*'s claim, or that is unknowable.

    Never raises: ``Session.started_at`` and ``TicketTask.claimed_at`` are
    plain ``datetime`` fields, and comparing a naive one with an aware one
    raises ``TypeError``, which would abort the whole reconcile tick. Either
    being naive fails closed (not adopted).
    """
    claimed_at = row.claimed_at
    if claimed_at is None:
        return True
    started_at = session.started_at
    if not (_is_aware(claimed_at) and _is_aware(started_at)):
        _log.debug(
            "unowned_running: %s/%s not adopted: naive datetime "
            "(claimed_at=%s, session %s started_at=%s)",
            row.client,
            row.ticket_id,
            claimed_at,
            session.id,
            started_at,
        )
        return False
    if started_at < claimed_at:
        _log.debug(
            "unowned_running: %s/%s not adopted: session %s started at %s, "
            "before the claim at %s",
            row.client,
            row.ticket_id,
            session.id,
            started_at.isoformat(),
            claimed_at.isoformat(),
        )
        return False
    return True


def _claim_context_session_id(
    context: dict[str, object] | None, row: TicketTask
) -> str | None:
    """The session id *context* ties to *row*'s current claim, or None.

    A missing, unreadable or non-object context, one with no ``attempt`` (a
    resume writes none), or one naming another attempt, ticket or client is
    no proof. ``bool`` is excluded from the attempt check: JSON ``true``
    would otherwise equal attempt 1.
    """
    if context is None:
        return None
    attempt = context.get("attempt")
    session_id = context.get("session_id")
    if (
        type(attempt) is int
        and attempt == row.attempts
        and context.get("ticket_id") == row.ticket_id
        and context.get("client") == row.client
        and isinstance(session_id, str)
    ):
        return session_id
    return None


def _classify_unbound(
    row: TicketTask,
    *,
    client: ClientConfig,
    sessions_by_id: dict[str, Session],
    bound_session_ids: set[str],
) -> UnownedCandidate | None:
    """Return the adoption for one unbound RUNNING *row*, or None to leave it."""
    worktree = worktree_path_for(
        client, f"{client.feature_branch_prefix}/{row.ticket_id}"
    )
    session_id = _claim_context_session_id(_read_cw_context(str(worktree)), row)
    if session_id is None:
        return None
    if session_id in bound_session_ids:
        _log.debug(
            "unowned_running: %s/%s not adopted: session %s is bound to another row",
            row.client,
            row.ticket_id,
            session_id,
        )
        return None
    session = sessions_by_id.get(session_id)
    if (
        session is None
        or session.client != row.client
        or ticket_id_for_session(session.name) != row.ticket_id
    ):
        _log.debug(
            "unowned_running: %s/%s not adopted: session %s is not recorded "
            "for this client and ticket",
            row.client,
            row.ticket_id,
            session_id,
        )
        return None
    if not _session_postdates_claim(session, row):
        return None
    return UnownedCandidate(
        ticket_id=row.ticket_id,
        client=row.client,
        lane=row.lane,
        created_at=row.created_at,
        attempts=row.attempts,
        claimed_at=row.claimed_at,
        session_id=session.id,
        session_name=session.name,
    )


def detect_unowned_running(
    store: DevQueueStore, state: CwState, clients: dict[str, ClientConfig]
) -> list[UnownedCandidate]:
    """Return every unbound RUNNING row the evidence ties to a session. Pure.

    Reads the worktrees' ``cw-context.json`` files and nothing else; writes
    nothing (ADR-0006 invariant 1). *clients* is the tick's client scope: a
    row whose client is not in it (including an empty mapping) is skipped.
    """
    # Deferred, not module-top: cw.dispatch's package __init__ imports
    # cw.reconcile, so a top-level import of any cw.dispatch submodule here
    # is a real circular import at package-init time (same precedent as
    # cw.reconcile.tasks).
    from cw.dispatch.claim import _is_backstop_exempt

    sessions_by_id = {s.id: s for s in state.sessions}
    bound_session_ids = {t.session_id for t in store.tasks if t.session_id is not None}
    running = Counter(
        (t.client, t.ticket_id)
        for t in store.tasks
        if t.status is QueueItemStatus.RUNNING
    )
    candidates: list[UnownedCandidate] = []
    for row in store.tasks:
        if row.status is not QueueItemStatus.RUNNING or row.session_id is not None:
            continue
        client = clients.get(row.client)
        if client is None or _is_backstop_exempt(row):
            continue
        if running[(row.client, row.ticket_id)] > 1:
            _log.debug(
                "unowned_running: %s/%s not adopted: duplicate RUNNING rows",
                row.client,
                row.ticket_id,
            )
            continue
        candidate = _classify_unbound(
            row,
            client=client,
            sessions_by_id=sessions_by_id,
            bound_session_ids=bound_session_ids,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _still_adoptable(
    store: DevQueueStore, row: TicketTask, candidate: UnownedCandidate
) -> bool:
    """Re-check, under the bind's ``dev_queue_lock``, what detect saw.

    The row must still be unbound on the same claim, no other row may have
    taken the session, and no second RUNNING row may have appeared for the
    same (client, ticket) since detect read its snapshot.
    """
    if (
        row.session_id is not None
        or row.attempts != candidate.attempts
        or row.claimed_at != candidate.claimed_at
    ):
        return False
    return not any(
        other is not row
        and (
            other.session_id == candidate.session_id
            or (
                other.status is QueueItemStatus.RUNNING
                and other.client == row.client
                and other.ticket_id == row.ticket_id
            )
        )
        for other in store.tasks
    )


def _emit_adoption_record(record: dict[str, object]) -> None:
    """Record one validated adoption outbox entry."""
    payload: dict[str, object] = {
        "client": record["client"],
        "ticket_id": record["ticket_id"],
        "lane": record["lane"],
        "session_id": record["session_id"],
        "session_name": record["session_name"],
        "attempt": record["attempt"],
        "claimed_at": record["claimed_at"],
    }
    record_event(
        OrchestratorEventType.TASK_SESSION_ADOPTED,
        payload,
        correlation_id=cast("str", record["ticket_id"]),
    )


def _adoption_outbox_path() -> Path:
    return events_dir() / _ADOPTION_OUTBOX_NAME


def _read_adoption_outbox() -> list[dict[str, object]]:
    path = _adoption_outbox_path()
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(entry, dict) for entry in raw):
        message = "task.session_adopted outbox is not a list of records"
        raise CwError(message)
    entries = [cast("dict[str, object]", entry) for entry in raw]
    required = {
        "client",
        "ticket_id",
        "created_at",
        "lane",
        "session_id",
        "session_name",
        "attempt",
        "claimed_at",
    }
    if any(
        not required.issubset(entry)
        or any(
            not isinstance(entry[key], str)
            for key in required - {"attempt", "claimed_at"}
        )
        or type(entry["attempt"]) is not int
        or not (entry["claimed_at"] is None or isinstance(entry["claimed_at"], str))
        for entry in entries
    ):
        message = "task.session_adopted outbox contains an invalid record"
        raise CwError(message)
    return entries


def _write_adoption_outbox(entries: list[dict[str, object]]) -> None:
    path = _adoption_outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(entries, sort_keys=True) + "\n")


def _outbox_key(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record.get("client"),
        record.get("ticket_id"),
        record.get("created_at"),
        record.get("session_id"),
        record.get("attempt"),
        record.get("claimed_at"),
    )


def _adoption_record(candidate: UnownedCandidate) -> dict[str, object]:
    return {
        "client": candidate.client,
        "ticket_id": candidate.ticket_id,
        "created_at": candidate.created_at.isoformat(),
        "lane": candidate.lane,
        "session_id": candidate.session_id,
        "session_name": candidate.session_name,
        "attempt": candidate.attempts,
        "claimed_at": (
            candidate.claimed_at.isoformat()
            if candidate.claimed_at is not None
            else None
        ),
    }


def _stage_adoption(candidate: UnownedCandidate) -> None:
    """Write the durable marker before the queue bind is allowed."""
    record = _adoption_record(candidate)
    entries = _read_adoption_outbox()
    if not any(_outbox_key(entry) == _outbox_key(record) for entry in entries):
        entries.append(record)
        _write_adoption_outbox(entries)


def _remove_adoption_record(record: dict[str, object]) -> None:
    entries = _read_adoption_outbox()
    remaining = [
        entry for entry in entries if _outbox_key(entry) != _outbox_key(record)
    ]
    if len(remaining) != len(entries):
        _write_adoption_outbox(remaining)


def _drain_adoption_outbox() -> None:
    """Emit staged adoption events whose binds are present, retaining failures."""
    try:
        entries = _read_adoption_outbox()
    except (CwError, OSError, ValueError):
        _log.exception("unowned_running: adoption outbox could not be read")
        return
    if not entries:
        return
    with dev_queue_lock():
        store = load_dev_queue()
        ready: list[dict[str, object]] = []
        stale: list[dict[str, object]] = []
        for entry in entries:
            row = next(
                (
                    task
                    for task in store.tasks
                    if task.client == entry.get("client")
                    and task.ticket_id == entry.get("ticket_id")
                    and task.created_at.isoformat() == entry.get("created_at")
                ),
                None,
            )
            if row is None:
                stale.append(entry)
            elif row.status is QueueItemStatus.RUNNING and row.session_id == entry.get(
                "session_id"
            ):
                ready.append(entry)
            else:
                stale.append(entry)
    for entry in stale:
        try:
            _remove_adoption_record(entry)
        except (CwError, OSError, ValueError):
            _log.exception(
                "unowned_running: stale adoption outbox entry could not be removed"
            )
    for entry in ready:
        try:
            _emit_adoption_record(entry)
            _remove_adoption_record(entry)
        except (CwError, OSError, ValueError):
            _log.exception(
                "unowned_running: task.session_adopted remains queued for %s/%s",
                entry.get("client"),
                entry.get("ticket_id"),
            )


def _act_adopt(candidate: UnownedCandidate) -> bool:
    """Bind the candidate's row to its session; return whether it was bound."""
    from cw.dispatch.claim import _apply_spawn_success_fields, _find_running_row

    try:
        _stage_adoption(candidate)
    except (CwError, OSError, ValueError):
        _log.exception(
            "unowned_running: %s/%s adoption deferred because its audit "
            "outbox was not persisted",
            candidate.client,
            candidate.ticket_id,
        )
        return False
    with dev_queue_lock():
        store = load_dev_queue()
        row = _find_running_row(
            store,
            candidate.ticket_id,
            candidate.client,
            created_at=candidate.created_at,
        )
        if row is None or not _still_adoptable(store, row, candidate):
            try:
                _remove_adoption_record(_adoption_record(candidate))
            except (CwError, OSError, ValueError):
                _log.exception(
                    "unowned_running: stale adoption outbox entry could not be removed"
                )
            return False
        try:
            _apply_spawn_success_fields(row, session_id=candidate.session_id)
            save_dev_queue(store)
        except (CwError, OSError):
            _log.exception(
                "unowned_running: %s/%s adoption deferred because the "
                "dev-queue bind was not persisted",
                candidate.client,
                candidate.ticket_id,
            )
            return False
    _drain_adoption_outbox()
    _log.info(
        "unowned_running: adopted %s/%s onto session %s (attempt %d)",
        candidate.client,
        candidate.ticket_id,
        candidate.session_id,
        candidate.attempts,
    )
    return True


def run_unowned_running_recovery(*, clients: dict[str, ClientConfig]) -> list[str]:
    """Adopt every unbound RUNNING row the evidence ties to a session.

    Returns the adopted ticket ids. *clients* is the tick's client scope.
    """
    _drain_adoption_outbox()
    if not clients:
        return []
    candidates = detect_unowned_running(load_dev_queue(), load_state(), clients)
    return [c.ticket_id for c in candidates if _act_adopt(c)]
