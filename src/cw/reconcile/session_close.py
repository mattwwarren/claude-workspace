"""Operator session closes: the shared in-lock close body and queued closes (#2694).

Three parts, kept together because they share one close body:

- :func:`close_session_in_state` -- the in-lock close that ``cw spawn close``
  performs (moved from ``cw.cli.spawn``), plus its flip-only
  ``settle_row=False`` variant for ``cw dev-queue cancel``, whose row
  ``cancel_ticket`` has already CANCELLED. Reconcile cannot import ``cw.cli``,
  so the body lives here.
- A lock-free queued-close store under ``state_dir()/queued_closes/``: one
  JSON file per session, written atomically, fenced to the session
  incarnation (``resumed_at`` + ``surface_ref``) it was issued against. An
  operator close that met a held sessions lock records itself here instead
  of waiting, and the file is the retry path.
- Two appliers that share one per-entry function (``_apply_entry``):
  :func:`apply_queued_closes`, called inside reconcile's sessions-lock hold
  (ADR-0005: no second write path), and :func:`close_sessions_for_cancel`,
  the cancel command's inline attempt. An entry's ``origin`` is the only
  row-handling switch: ``spawn_close`` is a full close (route a staged
  result, else cancel the RUNNING row, #2458), ``cancel`` is flip-only.

Authority (ADR-0006 invariant 2): every close here is a recorded explicit
operator command, never a policy reap. Nothing here calls a daemon ``stop``
(``tests/test_reconcile_stop_call_sites_guard.py``): the reconcile applier
queues stops with :func:`~cw.reconcile.deferred.defer_surface_stop`, and the
cancel applier returns its stop targets to the CLI, which stops them after
the lock. The audit record is a WARNING log (cw's default logging drops
INFO), with no orchestrator event, matching the inline ``cw spawn close``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast, get_args

from cw.atomic import atomic_write_text
from cw.config import (
    load_state,
    mutate_state,
    refuse_real_state_write,
    save_state,
    state_dir,
)
from cw.dev_queue import (
    cancel_task_for_session,
    load_dev_queue,
    running_task_for_session,
)
from cw.exceptions import CwError, SessionsLockTimeoutError
from cw.models import (
    TERMINAL_SESSION_STATUSES,
    CompletionReason,
    SessionOrigin,
    SessionStatus,
)
from cw.reconcile._shared import _apply_sentinel_to_task, holds_staged_emit_result
from cw.reconcile.deferred import defer_surface_stop
from cw.result import reconstruct_staged_sentinel

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from cw.models import CwState, Session
    from cw.reconcile.deferred import DeferredReconcileJobs

_log = logging.getLogger(__name__)

QueuedCloseOrigin = Literal["spawn_close", "cancel"]
_ORIGINS: frozenset[str] = frozenset(get_args(QueuedCloseOrigin))
# The origin whose entries settle the row (full close); every other origin is
# flip-only.
_FULL_CLOSE_ORIGIN: QueuedCloseOrigin = "spawn_close"
_CANCEL_ORIGIN: QueuedCloseOrigin = "cancel"

QUEUED_CLOSES_DIRNAME = "queued_closes"
_ENTRY_SUFFIX = ".json"
# A session id becomes a file name: no separators, no leading dot.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class QueuedClose:
    """One recorded operator close awaiting the sessions lock."""

    session_id: str
    queued_at: datetime
    origin: QueuedCloseOrigin
    resumed_at: datetime | None
    surface_ref: str | None


@dataclass(frozen=True, slots=True)
class CancelCloseOutcome:
    """What ``cw dev-queue cancel``'s session step did.

    *surfaces_to_stop*: every resolved DAEMON session's surface (the caller
    stops them, as cancel always has). *pending_ids*: sessions whose close is
    recorded but not applied (the sessions lock was busy). *unrecorded_ids*:
    sessions neither closed nor recorded (the entry write failed and the lock
    was busy), which need a manual ``cw spawn close``.
    """

    surfaces_to_stop: tuple[str, ...] = ()
    pending_ids: tuple[str, ...] = ()
    unrecorded_ids: tuple[str, ...] = ()


class _Outcome(StrEnum):
    APPLIED = "applied"
    VOID = "void"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class _EntryResult:
    outcome: _Outcome
    surface: str | None = None
    prior_status: SessionStatus | None = None


# ---------------------------------------------------------------------------
# The close body
# ---------------------------------------------------------------------------


def route_staged_emit_result(sess: Session) -> bool:
    """Route *sess*'s staged ``cw result emit`` result to its RUNNING row (#2458).

    Returns True when the row is now dispositioned by that result -- routed,
    or already landed terminal by a concurrent caller -- so the caller must
    NOT also cancel it. Returns False (cancel as before) when nothing is
    staged, the staged dict reconstructs into neither sentinel arm, the
    session owns no RUNNING row, or the shared authority refuses the route
    (a #1031 stage mismatch, or a BlockedResult landing the row FAILED).

    The row is found by :func:`~cw.dev_queue.running_task_for_session` --
    the lookup ``cancel_task_for_session`` itself uses -- via a lock-free
    read; ``_apply_sentinel_to_task`` re-resolves it under
    ``dev_queue_lock``. Called under ``sessions_lock``, the same
    ``sessions_lock`` -> ``dev_queue_lock`` nesting ``signal_stop`` already
    uses.
    """
    if not holds_staged_emit_result(sess):
        return False
    staged = reconstruct_staged_sentinel(sess.last_result)
    if staged is None:
        return False
    task = running_task_for_session(load_dev_queue(), sess.id)
    if task is None:
        return False
    outcome = _apply_sentinel_to_task(task.ticket_id, sess, staged)
    return outcome.routed or outcome.task_already_terminal


def _daemon_surface_or_none(sess: Session) -> str | None:
    """*sess*'s surface when it is a DAEMON session's, else ``None``."""
    if sess.surface_ref is not None and sess.origin is SessionOrigin.DAEMON:
        return sess.surface_ref
    return None


def close_session_in_state(sess: Session, *, settle_row: bool = True) -> str | None:
    """Close *sess* in memory; return the DAEMON surface to stop after the lock.

    The caller holds ``sessions_lock`` and saves the state *sess* belongs to.

    ``settle_row=True`` (``cw spawn close`` and queued ``spawn_close``
    entries): an already-COMPLETED session mutates nothing (#2480: its
    surface is still returned so a repeat close clears a leaked worker);
    otherwise a DAEMON session's staged ``cw result emit`` result is routed
    (#2458), else its RUNNING row is cancelled so the dispatcher cannot
    revert and re-spawn it (#317), and the session is stamped COMPLETED(USER).

    ``settle_row=False`` (the ``cw dev-queue cancel`` step and queued
    ``cancel`` entries) is flip-only: a TERMINAL session (COMPLETED or
    TIMED_OUT) mutates nothing, and a live one is stamped COMPLETED(USER)
    without touching the queue or taking the dev-queue lock. It is used only
    where ``cancel_ticket`` already CANCELLED the row and cleared its
    ``session_id``, so the row steps could only cost an unbounded
    ``dev_queue_lock`` wait inside the sessions-lock hold.
    """
    if settle_row:
        already_closed = sess.status == SessionStatus.COMPLETED
    else:
        already_closed = sess.status in TERMINAL_SESSION_STATUSES
    if already_closed:
        return _daemon_surface_or_none(sess)
    surface = _daemon_surface_or_none(sess)
    if sess.surface_ref is not None and surface is None:
        _log.warning(
            "Session %s has legacy surface_ref %r; skipping surface close",
            sess.id,
            sess.surface_ref,
        )
    if (
        settle_row
        and sess.origin is SessionOrigin.DAEMON
        and not route_staged_emit_result(sess)
    ):
        cancel_task_for_session(sess.id)
    sess.status = SessionStatus.COMPLETED
    sess.completed_at = datetime.now(UTC)
    sess.completed_reason = CompletionReason.USER
    return surface


# ---------------------------------------------------------------------------
# The queued-close store
# ---------------------------------------------------------------------------


def queued_closes_dir() -> Path:
    """Directory of queued-close entries (read at call time; tests redirect it)."""
    return state_dir() / QUEUED_CLOSES_DIRNAME


def _entry_path(session_id: str) -> Path:
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        msg = f"unsafe session id for a queued close: {session_id!r}"
        raise ValueError(msg)
    return queued_closes_dir() / f"{session_id}{_ENTRY_SUFFIX}"


def _entry_for(
    session: Session, *, origin: QueuedCloseOrigin, now: datetime
) -> QueuedClose:
    return QueuedClose(
        session_id=session.id,
        queued_at=now,
        origin=origin,
        resumed_at=session.resumed_at,
        surface_ref=session.surface_ref,
    )


def _write_entry(entry: QueuedClose) -> Path:
    path = _entry_path(entry.session_id)
    payload = {
        "session_id": entry.session_id,
        "queued_at": entry.queued_at.isoformat(),
        "origin": entry.origin,
        "resumed_at": entry.resumed_at.isoformat() if entry.resumed_at else None,
        "surface_ref": entry.surface_ref,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    refuse_real_state_write(path)
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def queue_close(session: Session, *, origin: QueuedCloseOrigin, now: datetime) -> Path:
    """Record an operator close of *session* for the next reconcile pass.

    Lock-free and atomic; a second close of the same session overwrites the
    first (one entry per session). Raises ``ValueError`` for a session id
    that cannot be a file name, and lets an ``OSError`` from the write
    propagate. Returns the entry path (deleting it withdraws the close).
    """
    return _write_entry(_entry_for(session, origin=origin, now=now))


def _optional_time(raw: object) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"expected an ISO-8601 string or null, got {type(raw).__name__}"
        raise TypeError(msg)
    return datetime.fromisoformat(raw)


def _parse_entry(raw: str) -> QueuedClose:
    """Parse one entry; ``ValueError``/``TypeError`` for anything malformed."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        msg = "entry is not a JSON object"
        raise TypeError(msg)
    session_id = data.get("session_id")
    origin = data.get("origin")
    surface_ref = data.get("surface_ref")
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        msg = f"bad session_id {session_id!r}"
        raise ValueError(msg)
    if origin not in _ORIGINS:
        msg = f"unknown origin {origin!r}"
        raise ValueError(msg)
    if surface_ref is not None and not isinstance(surface_ref, str):
        msg = f"bad surface_ref {surface_ref!r}"
        raise TypeError(msg)
    queued_at = _optional_time(data.get("queued_at"))
    if queued_at is None or queued_at.tzinfo is None:
        msg = "queued_at must be a timezone-aware timestamp"
        raise ValueError(msg)
    return QueuedClose(
        session_id=session_id,
        queued_at=queued_at,
        origin=cast("QueuedCloseOrigin", origin),
        resumed_at=_optional_time(data.get("resumed_at")),
        surface_ref=surface_ref,
    )


def _read_entry(path: Path) -> QueuedClose | None:
    """Parse *path*, or ``None``; never raises, so one entry cannot abort a pass.

    A corrupt entry (malformed, truncated, not UTF-8) can never apply, so it is
    removed. An entry that cannot be read at all (``OSError``, e.g. a
    permission error) may still be valid, so it is skipped and kept.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.warning(
            "queued_close_unreadable: path=%s error=%s: %s action=entry_kept",
            path,
            type(exc).__name__,
            exc,
        )
        return None
    except UnicodeDecodeError as exc:
        _discard_corrupt(path, exc)
        return None
    try:
        return _parse_entry(raw)
    except (ValueError, TypeError) as exc:
        _discard_corrupt(path, exc)
        return None


def _discard_corrupt(path: Path, exc: Exception) -> None:
    """Remove the corrupt entry at *path*, logging why (best effort)."""
    _log.warning(
        "queued_close_corrupt: path=%s error=%s: %s action=entry_removed",
        path,
        type(exc).__name__,
        exc,
    )
    with contextlib.suppress(OSError):
        path.unlink()


def pending_queued_closes() -> list[QueuedClose]:
    """Every parseable queued close, oldest first (temp files are ignored)."""
    entries = [
        entry
        for path in sorted(queued_closes_dir().glob(f"*{_ENTRY_SUFFIX}"))
        if (entry := _read_entry(path)) is not None
    ]
    return sorted(entries, key=lambda entry: entry.queued_at)


def clear_queued_close(session_id: str) -> None:
    """Remove *session_id*'s entry; a missing entry is a no-op."""
    with contextlib.suppress(FileNotFoundError):
        _entry_path(session_id).unlink()


def _clear_settled(session_ids: Iterable[str]) -> None:
    """Remove each settled entry; a failed removal is logged, never raised.

    The entry is then kept, and the next pass finds its session already
    closed (or voided), so re-applying it changes nothing.
    """
    for session_id in session_ids:
        try:
            clear_queued_close(session_id)
        except OSError as exc:
            _log.warning(
                "queued_close_clear_failed: session=%s error=%s: %s action=entry_kept",
                session_id,
                type(exc).__name__,
                exc,
            )


# ---------------------------------------------------------------------------
# Applying entries
# ---------------------------------------------------------------------------


def _fence_mismatch(sess: Session, entry: QueuedClose) -> list[str]:
    """The incarnation fields that changed since *entry* was recorded."""
    mismatched: list[str] = []
    if sess.resumed_at != entry.resumed_at:
        mismatched.append("resumed_at")
    if sess.surface_ref != entry.surface_ref:
        mismatched.append("surface_ref")
    return mismatched


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _apply_entry(state: CwState, entry: QueuedClose) -> _EntryResult:
    """Apply one entry to *state* in memory (caller holds the lock and saves).

    A session that no longer exists, or that was resumed into a new
    incarnation since the close was recorded, voids the entry: the recorded
    authority was for the old one.
    """
    sess = next((s for s in state.sessions if s.id == entry.session_id), None)
    if sess is None:
        _log.warning(
            "queued_close_unknown_session: session=%s origin=%s queued_at=%s"
            " action=entry_removed",
            entry.session_id,
            entry.origin,
            entry.queued_at.isoformat(),
        )
        return _EntryResult(_Outcome.UNKNOWN)
    mismatched = _fence_mismatch(sess, entry)
    if mismatched:
        _log.warning(
            "queued_close_void: session=%s origin=%s queued_at=%s mismatched=%s"
            " entry_resumed_at=%s current_resumed_at=%s entry_surface_ref=%s"
            " current_surface_ref=%s",
            entry.session_id,
            entry.origin,
            entry.queued_at.isoformat(),
            ",".join(mismatched),
            _iso(entry.resumed_at),
            _iso(sess.resumed_at),
            entry.surface_ref,
            sess.surface_ref,
        )
        return _EntryResult(_Outcome.VOID)
    prior_status = sess.status
    surface = close_session_in_state(
        sess, settle_row=entry.origin == _FULL_CLOSE_ORIGIN
    )
    return _EntryResult(_Outcome.APPLIED, surface, prior_status)


def _apply_entry_isolated(state: CwState, entry: QueuedClose) -> _EntryResult | None:
    """:func:`_apply_entry`, or ``None`` (entry kept) when its close fails.

    One poison entry must not wedge every reconcile pass: it is logged and
    left for the operator, and the next entry still applies.
    """
    try:
        return _apply_entry(state, entry)
    except (CwError, OSError, ValueError):
        _log.warning(
            "queued_close_failed: session=%s origin=%s queued_at=%s action=entry_kept",
            entry.session_id,
            entry.origin,
            entry.queued_at.isoformat(),
            exc_info=True,
        )
        return None


def apply_queued_closes(state: CwState, *, deferred: DeferredReconcileJobs) -> int:
    """Apply every queued close to *state*; return how many applied.

    Called from ``reconcile``'s locked body, which holds ``sessions_lock``:
    the session flips are saved once here, each surface stop is queued on
    *deferred* (run after the lock releases), and applied, void and unknown
    entries are removed after the save. A ``spawn_close`` entry's row step
    may take ``dev_queue_lock`` (SESSIONS before STATE, as the inline close).
    """
    entries = pending_queued_closes()
    if not entries:
        return 0
    settled: list[str] = []
    applied = 0
    for entry in entries:
        result = _apply_entry_isolated(state, entry)
        if result is None:
            continue
        settled.append(entry.session_id)
        if result.surface is not None:
            defer_surface_stop(deferred, result.surface)
        if result.outcome is _Outcome.APPLIED:
            applied += 1
            _log.warning(
                "queued_close_applied: session=%s origin=%s settle_row=%s"
                " queued_at=%s prior_status=%s surface=%s",
                entry.session_id,
                entry.origin,
                entry.origin == _FULL_CLOSE_ORIGIN,
                entry.queued_at.isoformat(),
                result.prior_status,
                result.surface,
            )
    if applied:
        save_state(state)
    _clear_settled(settled)
    return applied


def _record_entry(entry: QueuedClose) -> bool:
    """Write *entry*; False (logged) when it cannot be recorded."""
    try:
        _write_entry(entry)
    except (OSError, ValueError):
        _log.warning(
            "queued_close_unrecorded: session=%s origin=%s",
            entry.session_id,
            entry.origin,
            exc_info=True,
        )
        return False
    return True


def close_sessions_for_cancel(
    session_ids: Iterable[str | None], *, timeout_s: float, skip_lock: bool
) -> CancelCloseOutcome:
    """Close the sessions ``cancel_ticket`` just unbound from their rows.

    Resolves each id with one lock-free read taken AFTER ``cancel_ticket``
    (so a session created since the command started is seen). For every live
    DAEMON session it writes a ``cancel`` entry FIRST, then makes one bounded
    sessions-lock attempt of *timeout_s* that applies the entries flip-only
    and clears them. On a timeout -- or with *skip_lock*, after an earlier
    ticket's attempt already timed out -- the entries stay for the next
    reconcile pass. USER-origin and unknown ids are skipped; TERMINAL
    sessions are not flipped but their surfaces are still returned, as cancel
    always stopped every DAEMON session it cleared.
    """
    wanted = {sid for sid in session_ids if sid is not None}
    if not wanted:
        return CancelCloseOutcome()
    sessions = [
        s
        for s in load_state().sessions
        if s.id in wanted and s.origin is SessionOrigin.DAEMON
    ]
    surfaces = tuple(s.surface_ref for s in sessions if s.surface_ref is not None)
    now = datetime.now(UTC)
    entries = [
        _entry_for(s, origin=_CANCEL_ORIGIN, now=now)
        for s in sessions
        if s.status not in TERMINAL_SESSION_STATUSES
    ]
    unrecorded = {entry.session_id for entry in entries if not _record_entry(entry)}
    settled: set[str] = set()

    def _flip(state: CwState) -> None:
        for entry in entries:
            if _apply_entry_isolated(state, entry) is not None:
                settled.add(entry.session_id)

    if entries and not skip_lock:
        with contextlib.suppress(SessionsLockTimeoutError):
            mutate_state(_flip, bounded=True, timeout_s=timeout_s)
    _clear_settled(settled - unrecorded)
    return CancelCloseOutcome(
        surfaces_to_stop=surfaces,
        pending_ids=tuple(
            e.session_id
            for e in entries
            if e.session_id not in settled and e.session_id not in unrecorded
        ),
        unrecorded_ids=tuple(
            e.session_id
            for e in entries
            if e.session_id in unrecorded and e.session_id not in settled
        ),
    )
