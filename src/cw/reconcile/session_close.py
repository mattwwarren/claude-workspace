"""Operator session closes: the shared in-lock close body and queued closes (#2694).

Three parts, kept together because they share one close body:

- :func:`close_session_in_state` -- the in-lock close that ``cw spawn close``
  performs (moved from ``cw.cli.spawn``), plus its flip-only
  ``settle_row=False`` variant for ``cw dev-queue cancel``, whose row
  ``cancel_ticket`` has already CANCELLED. Reconcile cannot import ``cw.cli``,
  so the body lives here.
- A lock-free queued-close store under ``state_dir()/queued_closes/``: one
  JSON file per session, written atomically, fenced to the session
  incarnation (``started_at`` + ``resumed_at`` + ``surface_ref``) it was
  issued against. An operator close that met a held sessions lock records
  itself here instead of waiting, and the file is the retry path.
- Two appliers that share one per-entry function (``_apply_entry``):
  :func:`apply_queued_closes`, called inside reconcile's sessions-lock hold
  (ADR-0005: no second write path), and :func:`close_sessions_for_cancel`,
  the cancel command's inline attempt, fenced to the incarnations
  :func:`read_cancel_fence` read before the row was cancelled. An entry's
  ``origin`` is the only row-handling switch: ``spawn_close`` is a full
  close (route a staged result, else cancel the RUNNING row, #2458),
  ``cancel`` is flip-only.

Authority (ADR-0006 invariant 2): every close here is a recorded explicit
operator command, never a policy reap. Nothing here calls a daemon ``stop``
(``tests/test_reconcile_stop_call_sites_guard.py``): the reconcile applier
queues stops with :func:`~cw.reconcile.deferred.defer_surface_stop`, and the
cancel applier returns its stop targets to the CLI, which stops them after
the lock. Every close that flips a session is audited (ADR-0006 invariant 3)
through the one shared mechanism, :mod:`cw.reconcile.close_audit`: the
durable intent is written before any mutation (inside
:func:`close_session_in_state`), and after the save the close records its
``SESSION_COMPLETED`` history event and finalizes the intent into a
``session.reap_authorized`` event. A queued entry is cleared only once both
the close and that finalize have committed, so a crash between steps leaves
a replayable entry, never a committed close without a record; a replay
finalizes the pending intent without a second history event. The WARNING
logs (``queued_close_*``; cw's default logging drops INFO) accompany it. No
orchestrator ``session.completed`` event is emitted: the dispatch loop
consumes that one to settle rows, which these closes do themselves.
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
from cw.reconcile._shared import (
    _apply_sentinel_to_task,
    holds_staged_emit_result,
    ticket_id_for_session,
)
from cw.reconcile.close_audit import (
    OPERATOR_CLOSE_REASON,
    CloseAudit,
    CloseAuditError,
    finalize_close_audit_intent,
    queue_close_audit_intent,
    record_close_audit,
)
from cw.reconcile.deferred import defer_surface_stop
from cw.result import reconstruct_staged_sentinel

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
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
# The operator command each origin records, for the audit event.
_ORIGIN_COMMANDS: dict[str, str] = {
    "spawn_close": "cw spawn close",
    "cancel": "cw dev-queue cancel",
}
# Who applied the close: reconcile (a queued entry) or the command inline.
_APPLIED_BY_RECONCILE = "reconcile"
_APPLIED_BY_INLINE = "inline"

QUEUED_CLOSES_DIRNAME = "queued_closes"
_ENTRY_SUFFIX = ".json"
# A session id becomes a file name: no separators, no leading dot.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class QueuedClose:
    """One recorded operator close awaiting the sessions lock.

    ``started_at``/``resumed_at``/``surface_ref`` fence it to the session
    incarnation it was issued against (``claude_session_id`` is not part of
    the fence: reconcile backfills it within one incarnation). ``started_at``
    is ``None`` only for an entry written before it was recorded; such an
    entry can never match a session, so it is voided rather than applied.
    """

    session_id: str
    queued_at: datetime
    origin: QueuedCloseOrigin
    started_at: datetime | None
    resumed_at: datetime | None
    surface_ref: str | None


@dataclass(frozen=True, slots=True)
class CancelCloseOutcome:
    """What ``cw dev-queue cancel``'s session step did.

    *surfaces_to_stop*: the surface of every resolved DAEMON session still in
    the incarnation the cancel fenced (the caller stops them, as cancel
    always has). *pending_ids*: sessions whose close is recorded but not
    applied (the sessions lock was busy). *unrecorded_ids*: sessions neither
    closed nor recorded (the entry write failed and the lock was busy), which
    need a manual ``cw spawn close``. *refused_ids*: sessions resumed into a
    new incarnation since the cancel's snapshot; they are neither closed nor
    stopped. *failed*: ``(session_id, error)`` for sessions whose close failed
    for a reason other than a busy sessions lock; their entry is kept for the
    next reconcile pass.
    """

    surfaces_to_stop: tuple[str, ...] = ()
    pending_ids: tuple[str, ...] = ()
    unrecorded_ids: tuple[str, ...] = ()
    refused_ids: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()


class _Outcome(StrEnum):
    APPLIED = "applied"
    VOID = "void"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _EntryResult:
    outcome: _Outcome
    surface: str | None = None
    prior_status: SessionStatus | None = None
    # The session and audit context of an APPLIED entry; *flipped* is True
    # only when this apply changed its status (else it is a replay or a
    # repeat close of an already closed session).
    session: Session | None = None
    audit: CloseAudit | None = None
    flipped: bool = False
    # ``<ExcType>: <message>`` of a FAILED entry.
    error: str = ""


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


def operator_close_audit(
    sess: Session,
    *,
    origin: QueuedCloseOrigin,
    applied_by: str,
    queued_at: datetime | None,
) -> CloseAudit:
    """The audit context of a ``cw spawn close`` / ``cw dev-queue cancel`` close.

    *applied_by* is ``inline`` (the command itself) or ``reconcile`` (a
    queued entry); *queued_at* is the entry's, ``None`` inline. The fields
    extend the shared ``SESSION_COMPLETED`` history metadata.
    """
    return CloseAudit(
        command=_ORIGIN_COMMANDS[origin],
        reason=OPERATOR_CLOSE_REASON,
        ticket_id=ticket_id_for_session(sess.name),
        details={
            "origin": origin,
            "applied_by": applied_by,
            "queued_at": _iso(queued_at) or "",
            "settle_row": str(origin == _FULL_CLOSE_ORIGIN).lower(),
            "started_at": sess.started_at.isoformat(),
            "resumed_at": _iso(sess.resumed_at) or "",
        },
    )


def close_session_in_state(
    sess: Session, *, audit: CloseAudit, settle_row: bool = True
) -> str | None:
    """Close *sess* in memory; return the DAEMON surface to stop after the lock.

    The caller holds ``sessions_lock`` and saves the state *sess* belongs to,
    then records and finalizes *audit* (:mod:`cw.reconcile.close_audit`).
    A close that will change *sess* first writes *audit*'s durable intent,
    before any mutation (the row step included); a
    :class:`~cw.reconcile.close_audit.CloseAuditError` there means nothing
    was closed.

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
    queue_close_audit_intent(sess, audit)
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


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


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
        started_at=session.started_at,
        resumed_at=session.resumed_at,
        surface_ref=session.surface_ref,
    )


def _write_entry(entry: QueuedClose) -> Path:
    path = _entry_path(entry.session_id)
    payload = {
        "session_id": entry.session_id,
        "queued_at": entry.queued_at.isoformat(),
        "origin": entry.origin,
        "started_at": _iso(entry.started_at),
        "resumed_at": _iso(entry.resumed_at),
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
        started_at=_optional_time(data.get("started_at")),
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
    if sess.started_at != entry.started_at:
        mismatched.append("started_at")
    if sess.resumed_at != entry.resumed_at:
        mismatched.append("resumed_at")
    if sess.surface_ref != entry.surface_ref:
        mismatched.append("surface_ref")
    return mismatched


def _apply_entry(
    state: CwState, entry: QueuedClose, *, applied_by: str
) -> _EntryResult:
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
            " entry_started_at=%s current_started_at=%s"
            " entry_resumed_at=%s current_resumed_at=%s entry_surface_ref=%s"
            " current_surface_ref=%s",
            entry.session_id,
            entry.origin,
            entry.queued_at.isoformat(),
            ",".join(mismatched),
            _iso(entry.started_at),
            _iso(sess.started_at),
            _iso(entry.resumed_at),
            _iso(sess.resumed_at),
            entry.surface_ref,
            sess.surface_ref,
        )
        return _EntryResult(_Outcome.VOID)
    prior_status = sess.status
    audit = operator_close_audit(
        sess, origin=entry.origin, applied_by=applied_by, queued_at=entry.queued_at
    )
    surface = close_session_in_state(
        sess, audit=audit, settle_row=entry.origin == _FULL_CLOSE_ORIGIN
    )
    return _EntryResult(
        _Outcome.APPLIED,
        surface,
        prior_status,
        session=sess,
        audit=audit,
        flipped=sess.status != prior_status,
    )


def _apply_entry_isolated(
    state: CwState, entry: QueuedClose, *, applied_by: str
) -> _EntryResult:
    """:func:`_apply_entry`, or a FAILED result (entry kept) when its close fails.

    One poison entry must not wedge every reconcile pass: it is logged and
    left for the operator, and the next entry still applies.
    """
    try:
        return _apply_entry(state, entry, applied_by=applied_by)
    except (CwError, OSError, ValueError) as exc:
        _log.warning(
            "queued_close_failed: session=%s origin=%s queued_at=%s action=entry_kept",
            entry.session_id,
            entry.origin,
            entry.queued_at.isoformat(),
            exc_info=True,
        )
        return _EntryResult(_Outcome.FAILED, error=f"{type(exc).__name__}: {exc}")


def _settle(results: Iterable[tuple[QueuedClose, _EntryResult]]) -> list[str]:
    """Audit each committed close; return the entries now safe to clear.

    Call it only after the closes are saved. An APPLIED close records its
    history event (when this pass flipped it) and finalizes its intent; a
    replay of an already-closed session only finalizes the intent its crashed
    pass left (no second history event). The entry is cleared only once the
    finalize committed. Void and unknown entries clear; failed ones stay.
    """
    settled: list[str] = []
    for entry, result in results:
        if result.outcome is _Outcome.FAILED:
            continue
        sess, audit, prior = result.session, result.audit, result.prior_status
        if sess is None or audit is None or prior is None:
            settled.append(entry.session_id)
            continue
        if result.flipped:
            # Logged with its traceback by record_close_audit; the close
            # stands and its intent is still delivered below.
            with contextlib.suppress(CloseAuditError):
                record_close_audit(sess, audit, prior_status=prior)
        if finalize_close_audit_intent(sess, audit, replay=not result.flipped):
            settled.append(entry.session_id)
            continue
        _log.warning(
            "queued_close_audit_pending: session=%s origin=%s action=entry_kept",
            entry.session_id,
            entry.origin,
        )
    return settled


def apply_queued_closes(state: CwState, *, deferred: DeferredReconcileJobs) -> int:
    """Apply every queued close to *state*; return how many applied.

    Called from ``reconcile``'s locked body, which holds ``sessions_lock``:
    each close writes its audit intent before mutating, the session flips
    are saved once here, each surface stop is queued on *deferred* (run
    after the lock releases), and an entry is removed only after the save
    and its audit finalize (:func:`_settle`). A ``spawn_close`` entry's row
    step may take ``dev_queue_lock`` (SESSIONS before STATE, as the inline
    close).
    """
    entries = pending_queued_closes()
    if not entries:
        return 0
    results = [
        (entry, _apply_entry_isolated(state, entry, applied_by=_APPLIED_BY_RECONCILE))
        for entry in entries
    ]
    applied = 0
    for entry, result in results:
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
    _clear_settled(_settle(results))
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


def read_cancel_fence() -> dict[str, Session]:
    """Snapshot every DAEMON session's incarnation, BEFORE ``cancel_ticket`` runs.

    Lock-free. :func:`close_sessions_for_cancel` acts on a cleared session
    only while it is still the incarnation recorded here, so a session
    resumed after the operator's cancel (a new incarnation the cancelled row
    never ran) is left running instead of being closed and stopped.
    """
    return {s.id: s for s in load_state().sessions if s.origin is SessionOrigin.DAEMON}


def _fenced_sessions(
    wanted: set[str], fence: Mapping[str, Session], now: datetime
) -> tuple[list[Session], list[str]]:
    """The cleared DAEMON sessions still in their fenced incarnation, and the rest.

    A session absent from *fence* was created after the snapshot (a fresh
    spawn the row was bound to), so there is no older incarnation to confuse
    it with and its current one is used.
    """
    matched: list[Session] = []
    refused: list[str] = []
    for sess in load_state().sessions:
        if sess.id not in wanted or sess.origin is not SessionOrigin.DAEMON:
            continue
        fenced = _entry_for(fence.get(sess.id, sess), origin=_CANCEL_ORIGIN, now=now)
        mismatched = _fence_mismatch(sess, fenced)
        if not mismatched:
            matched.append(sess)
            continue
        _log.warning(
            "cancel_close_refused: session=%s mismatched=%s"
            " fenced_started_at=%s current_started_at=%s"
            " fenced_resumed_at=%s current_resumed_at=%s"
            " fenced_surface_ref=%s current_surface_ref=%s action=left_running",
            sess.id,
            ",".join(mismatched),
            _iso(fenced.started_at),
            _iso(sess.started_at),
            _iso(fenced.resumed_at),
            _iso(sess.resumed_at),
            fenced.surface_ref,
            sess.surface_ref,
        )
        refused.append(sess.id)
    return matched, refused


def close_sessions_for_cancel(
    session_ids: Iterable[str | None],
    *,
    fence: Mapping[str, Session],
    timeout_s: float,
    skip_lock: bool,
) -> CancelCloseOutcome:
    """Close the sessions ``cancel_ticket`` just unbound from their rows.

    *fence* is :func:`read_cancel_fence`, taken before ``cancel_ticket``.
    Resolves each id with one lock-free read taken AFTER ``cancel_ticket``
    (so a session created since the snapshot is seen); a session resumed
    into a new incarnation since the snapshot is refused (logged, neither
    closed nor stopped). For every other live DAEMON session it writes a
    ``cancel`` entry FIRST, then makes one bounded sessions-lock attempt of
    *timeout_s* that applies the entries flip-only (re-checking the fence
    under the lock) and clears them. On a timeout -- or with *skip_lock*,
    after an earlier ticket's attempt already timed out -- the entries stay
    for the next reconcile pass. Any other failure of the attempt (a close
    that raised, or a state load/save error) is reported in ``failed``, not
    as a busy lock; those entries stay for the next pass too. Each close is
    audited (intent first, record and finalize after the save) and its entry
    cleared once the finalize committed. USER-origin and unknown ids are
    skipped; TERMINAL sessions are not flipped but their surfaces are still
    returned, as cancel always stopped every DAEMON session it cleared.
    """
    wanted = {sid for sid in session_ids if sid is not None}
    if not wanted:
        return CancelCloseOutcome()
    now = datetime.now(UTC)
    sessions, refused = _fenced_sessions(wanted, fence, now)
    entries = [
        _entry_for(s, origin=_CANCEL_ORIGIN, now=now)
        for s in sessions
        if s.status not in TERMINAL_SESSION_STATUSES
    ]
    unrecorded = {entry.session_id for entry in entries if not _record_entry(entry)}
    results: dict[str, _EntryResult] = {}

    def _flip(state: CwState) -> None:
        for entry in entries:
            results[entry.session_id] = _apply_entry_isolated(
                state, entry, applied_by=_APPLIED_BY_INLINE
            )

    if entries and not skip_lock:
        try:
            mutate_state(_flip, bounded=True, timeout_s=timeout_s)
        except SessionsLockTimeoutError:
            pass  # Busy lock: the recorded entries are the retry path.
        except (CwError, OSError, ValueError) as exc:
            # Not a lock timeout, and nothing was saved: every entry failed.
            failed = _EntryResult(_Outcome.FAILED, error=f"{type(exc).__name__}: {exc}")
            results.update((entry.session_id, failed) for entry in entries)
    settled = _settle(
        (e, results[e.session_id]) for e in entries if e.session_id in results
    )
    _clear_settled(set(settled) - unrecorded)
    voided = {sid for sid, r in results.items() if r.outcome is _Outcome.VOID}
    return CancelCloseOutcome(
        surfaces_to_stop=tuple(
            s.surface_ref
            for s in sessions
            if s.surface_ref is not None and s.id not in voided
        ),
        pending_ids=tuple(
            e.session_id
            for e in entries
            if e.session_id not in results and e.session_id not in unrecorded
        ),
        unrecorded_ids=tuple(
            e.session_id
            for e in entries
            if e.session_id in unrecorded and e.session_id not in results
        ),
        refused_ids=(
            *refused,
            *(e.session_id for e in entries if e.session_id in voided),
        ),
        failed=tuple(
            (e.session_id, results[e.session_id].error)
            for e in entries
            if e.session_id in results
            and results[e.session_id].outcome is _Outcome.FAILED
        ),
    )
