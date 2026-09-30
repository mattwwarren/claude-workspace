"""One-shot recovery of legacy codex sessions (``cw codex migrate-legacy``).

RFC 0014 B1 (#2389). A legacy codex session predates A2: its review ran on a
thread inside ``serve`` and it carries no ``local_liveness`` handle, so the A1
harvest sweep (``cw.reconcile.local``) never sees it, and only the boot sweep
(``cw.reconcile.codex_boot``) would ever dispose of it. Before B2 retires that
boot sweep, this command recovers every such session still live, once.

Per session, in order:

1. **Candidate.** The boot sweep's predicates: a live (``ACTIVE``/``IDLE``)
   ``DAEMON`` session with no liveness handle, a parseable ticket id, a
   dev-queue row still bound to it, a client config, and a codex backend. The
   boot sweep's ``_is_headless`` clause is replaced by
   :func:`_headless_scan_kind`, which tells "readable and not headless"
   (excluded, uncounted) apart from "cannot tell" (counted, parked).
2. **Live-writer scan first.** A session whose worktree or context cannot be
   read, or whose worktree may still hold a codex writer
   (``codex_boot.live_writer_park``), is ``skipped_writer_live``: nothing is
   mutated and it stays unresolved.
3. **Revalidate and act under ``sessions_lock``.** The session and its row are
   re-read and re-checked (``codex_boot.stale_snapshot_reason``), then the A1
   gate-audit-close path runs (``act_on_codex_harvest_candidate``) with no
   liveness handle and ``legacy_reason`` set, so its ``SESSION_COMPLETED``
   audit event carries ``legacy: true``.

Every outcome is written to the marker (``cw.config.codex_legacy_recovery_file``)
as soon as it is known. The run completes only when no session is left
unresolved; a later run retries just the unresolved sessions (plus any live
candidate an interrupted run never reached), and a completed marker makes
every further run a no-op. The marker, not an event, is the run record: no
new event type is emitted.

Lock order: marker lock, then ``sessions_lock``, then ``dev_queue_lock``
(nested only for revalidation). The marker lock is taken nowhere else.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.atomic import atomic_write_text
from cw.config import (
    codex_legacy_recovery_file,
    codex_legacy_recovery_lock_file,
    load_clients,
    load_effective_config,
    load_state,
    refuse_real_state_write,
    sessions_lock,
)
from cw.dev_queue import dev_queue_lock, load_dev_queue
from cw.exceptions import CodexLegacyRecoveryMarkerError
from cw.executor import resolve_executor_config
from cw.models import (
    CODEX_BACKEND,
    HOOK_CONTEXT_RELATIVE_PATH,
    CodexHarvestOutcome,
    CodexLegacyDisposition,
    CodexLegacyRecoveryMarker,
    LegacyRecoveryStatus,
    Outcome,
    SessionOrigin,
    UnresolvedEntry,
)
from cw.reconcile._shared import _LIVE_STATUSES, ticket_id_for_session
from cw.reconcile.codex_boot import live_writer_park, stale_snapshot_reason
from cw.reconcile.local import act_on_codex_harvest_candidate

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cw.models import ClientConfig, OrchestratorConfig, Session, TicketTask

_log = logging.getLogger(__name__)

# SESSION_COMPLETED ``reason`` for a session this recovery closes; the gate's
# own reason moves to ``detail`` (see cw.reconcile.local's audit payload).
CODEX_LEGACY_RECOVERY_REASON = "codex_legacy_recovery"

# UnresolvedEntry.reason values besides the cannot-tell _HeadlessScanKind ones.
REASON_LIVE_WRITER = "live_writer"
REASON_CLIENT_MISSING = "client_config_missing"
REASON_AUDIT_WRITE_FAILED = "audit_write_failed"
REASON_STATE_WRITE_FAILED = "state_write_failed"

# Why a session resolved as skipped_already_handled; logged, not persisted.
_WHY_ROW_UNBOUND = "its dev-queue row does not belong to it"
_WHY_NOT_CODEX = "its stage no longer runs on codex"
_WHY_SESSION_GONE = "it is no longer in sessions.json"
_WHY_NO_LONGER_ELIGIBLE = "it is no longer a live legacy codex session"
_WHY_TRANSITION_LOST = "its row moved before the transition"

_UNRESOLVED_DISPOSITIONS = frozenset(
    {CodexLegacyDisposition.FAILED, CodexLegacyDisposition.SKIPPED_WRITER_LIVE}
)
_ACTED_DISPOSITIONS = frozenset(
    {CodexLegacyDisposition.REQUEUED, CodexLegacyDisposition.PARKED}
)


class _HeadlessScanKind(StrEnum):
    """What a session's ``.claude/cw-context.json`` says about headlessness.

    Only ``NOT_HEADLESS`` (readable, and genuinely not headless) excludes a
    session, as the boot sweep's ``_is_headless`` does. The four cannot-tell
    kinds are candidates that park as ``skipped_writer_live``; their values
    are the ``UnresolvedEntry.reason`` recorded for them.
    """

    HEADLESS = "headless"
    NOT_HEADLESS = "not_headless"
    WORKTREE_UNSET = "worktree_unset"
    WORKTREE_MISSING = "worktree_missing"
    CONTEXT_MISSING = "context_missing"
    CONTEXT_UNREADABLE = "context_unreadable"


@dataclass(frozen=True)
class LegacyRecoveryReport:
    """How one run ended, and the marker as it now stands on disk."""

    status: LegacyRecoveryStatus
    marker: CodexLegacyRecoveryMarker

    @property
    def ok(self) -> bool:
        """False only for a partial run: some session is still unresolved."""
        return self.status is not LegacyRecoveryStatus.PARTIAL


@dataclass(frozen=True)
class _Resolution:
    """One session's disposition, and why (the unresolved reason, if any).

    ``disposition`` is None for a session that turned out not to be B1's (a
    non-codex stage) on a first look; it is dropped uncounted.
    """

    disposition: CodexLegacyDisposition | None
    reason: str | None = None


@dataclass(frozen=True)
class _Candidate:
    session: Session
    ticket_id: str
    kind: _HeadlessScanKind


@dataclass(frozen=True)
class _Snapshot:
    """The unlocked reads the classification runs against, loaded once."""

    sessions: list[Session]
    tasks: dict[tuple[str, str], TicketTask]
    clients: dict[str, ClientConfig]
    config: OrchestratorConfig


_ACT_RESOLUTIONS: dict[CodexHarvestOutcome, _Resolution] = {
    CodexHarvestOutcome.REQUEUED: _Resolution(CodexLegacyDisposition.REQUEUED),
    CodexHarvestOutcome.PARKED: _Resolution(CodexLegacyDisposition.PARKED),
    CodexHarvestOutcome.AUDIT_FAILED: _Resolution(
        CodexLegacyDisposition.FAILED, REASON_AUDIT_WRITE_FAILED
    ),
    CodexHarvestOutcome.TRANSITION_LOST: _Resolution(
        CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED, _WHY_TRANSITION_LOST
    ),
    # Only the harvest sweep's wrapper returns NO_ROW; listed so the map is
    # total. It would leave the session untouched, which is a failure here.
    CodexHarvestOutcome.NO_ROW: _Resolution(
        CodexLegacyDisposition.FAILED, CodexHarvestOutcome.NO_ROW.value
    ),
}


# --------------------------------------------------------------------------- #
# Marker store
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def codex_legacy_marker_lock() -> Iterator[None]:
    """Exclusive lock over the marker's load, check and write window.

    The ``cw.focus`` lock shape: ``fcntl.flock`` on a dedicated sibling lock
    file, a fresh fd per acquisition, so it is not reentrant.
    """
    codex_legacy_recovery_file().parent.mkdir(parents=True, exist_ok=True)
    fd = codex_legacy_recovery_lock_file().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def load_codex_legacy_marker() -> CodexLegacyRecoveryMarker | None:
    """Load the marker; None when no run has written one yet.

    A corrupt marker is a hard error (``CodexLegacyRecoveryMarkerError``),
    never "absent": see that exception's docstring.
    """
    path = codex_legacy_recovery_file()
    if not path.exists():
        return None
    try:
        return CodexLegacyRecoveryMarker.model_validate_json(path.read_text())
    except (ValidationError, ValueError) as err:
        msg = (
            f"codex legacy recovery marker {path} is corrupt; inspect or"
            f" restore it before re-running: {err}"
        )
        raise CodexLegacyRecoveryMarkerError(msg) from err


def save_codex_legacy_marker(marker: CodexLegacyRecoveryMarker) -> None:
    """Persist the marker atomically (caller holds the marker lock)."""
    path = codex_legacy_recovery_file()
    refuse_real_state_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, marker.model_dump_json(indent=2))


def _recompute_counts(marker: CodexLegacyRecoveryMarker) -> None:
    counts = Counter(outcome.disposition for outcome in marker.outcomes)
    marker.scanned = len(marker.outcomes)
    marker.requeued = counts[CodexLegacyDisposition.REQUEUED]
    marker.parked = counts[CodexLegacyDisposition.PARKED]
    marker.failed = counts[CodexLegacyDisposition.FAILED]
    marker.skipped_already_handled = counts[
        CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED
    ]
    marker.skipped_writer_live = counts[CodexLegacyDisposition.SKIPPED_WRITER_LIVE]


def _record_outcome(
    marker: CodexLegacyRecoveryMarker, outcome: Outcome, reason: str | None
) -> None:
    """Upsert *outcome* (latest wins), recompute, and persist immediately.

    Written per session so a crash mid-run never loses an outcome already
    known.
    """
    session_id = outcome.session_id
    marker.outcomes = [o for o in marker.outcomes if o.session_id != session_id]
    marker.outcomes.append(outcome)
    marker.unresolved = [u for u in marker.unresolved if u.session_id != session_id]
    if outcome.disposition in _UNRESOLVED_DISPOSITIONS:
        marker.unresolved.append(
            UnresolvedEntry(
                session_id=session_id,
                client=outcome.client,
                ticket_id=outcome.ticket_id,
                reason=reason or outcome.disposition.value,
            )
        )
    _recompute_counts(marker)
    save_codex_legacy_marker(marker)


# --------------------------------------------------------------------------- #
# Candidate selection and classification (unlocked snapshot)
# --------------------------------------------------------------------------- #


def _read_context(worktree: Path) -> dict[str, object] | _HeadlessScanKind:
    """The worktree's ``cw-context.json`` object, or the cannot-tell kind.

    ``ValueError`` covers both malformed JSON and invalid UTF-8.
    """
    try:
        raw = (worktree / HOOK_CONTEXT_RELATIVE_PATH).read_text(encoding="utf-8")
        context = json.loads(raw)
    except FileNotFoundError:
        return _HeadlessScanKind.CONTEXT_MISSING
    except (OSError, ValueError):
        return _HeadlessScanKind.CONTEXT_UNREADABLE
    if not isinstance(context, dict):
        return _HeadlessScanKind.CONTEXT_UNREADABLE
    return context


def _headless_scan_kind(session: Session) -> _HeadlessScanKind:
    """Classify *session*'s worktree context, keeping "cannot tell" distinct.

    A second reader of ``cw-context.json`` next to ``_shared._is_headless``,
    on purpose: that one fails open (anything unreadable is "not headless"),
    which would silently drop a session B1 must count.
    """
    worktree = session.worktree_path
    if worktree is None:
        return _HeadlessScanKind.WORKTREE_UNSET
    if not worktree.is_dir():
        return _HeadlessScanKind.WORKTREE_MISSING
    context = _read_context(worktree)
    if isinstance(context, _HeadlessScanKind):
        return context
    if context.get("headless"):
        return _HeadlessScanKind.HEADLESS
    return _HeadlessScanKind.NOT_HEADLESS


def _as_candidate(session: Session) -> _Candidate | None:
    """The boot sweep's session predicates, with the headless clause widened."""
    if (
        session.status not in _LIVE_STATUSES
        or session.origin is not SessionOrigin.DAEMON
        or session.local_liveness is not None
    ):
        return None
    ticket_id = ticket_id_for_session(session.name)
    if ticket_id is None:
        return None
    kind = _headless_scan_kind(session)
    if kind is _HeadlessScanKind.NOT_HEADLESS:
        return None
    return _Candidate(session=session, ticket_id=ticket_id, kind=kind)


def _row_owned_by(task: TicketTask, session_id: str) -> bool:
    """Whether *task* is still this session's own claim.

    A row re-claimed by another session, or one the boot pass parked and
    linked back (``codex_orphan_session_id``, owned by
    ``cw.reconcile.codex_reparks``), is not.
    """
    return task.session_id == session_id and task.codex_orphan_session_id != (
        session_id
    )


def _load_snapshot() -> _Snapshot:
    return _Snapshot(
        sessions=load_state().sessions,
        tasks={(task.ticket_id, task.client): task for task in load_dev_queue().tasks},
        clients=load_clients(),
        config=load_effective_config(),
    )


def _work_list(
    snapshot: _Snapshot, marker: CodexLegacyRecoveryMarker, retry_ids: set[str]
) -> tuple[list[_Candidate], list[Outcome]]:
    """Candidates to recover, and earlier-unresolved sessions no longer eligible.

    A retry considers the unresolved sessions plus any candidate no earlier
    run recorded (one an interrupted run never reached); sessions an earlier
    run already resolved are left alone.
    """
    seen = {outcome.session_id for outcome in marker.outcomes}
    candidates = [
        candidate
        for candidate in map(_as_candidate, snapshot.sessions)
        if candidate is not None
        and (candidate.session.id in retry_ids or candidate.session.id not in seen)
    ]
    eligible = {candidate.session.id for candidate in candidates}
    gone = [
        outcome
        for outcome in marker.outcomes
        if outcome.session_id in retry_ids and outcome.session_id not in eligible
    ]
    return candidates, gone


def _classify(
    candidate: _Candidate, snapshot: _Snapshot, *, retrying: bool
) -> _Resolution | tuple[TicketTask, ClientConfig]:
    """The row, client and backend checks; the row and client when all pass.

    Runs before any scan, so only a codex session with its own row is ever
    parked for a writer. A non-codex stage is not B1's and is dropped
    uncounted, unless an earlier run already counted the session.
    """
    session = candidate.session
    task = snapshot.tasks.get((candidate.ticket_id, session.client))
    if task is None or not _row_owned_by(task, session.id):
        return _Resolution(
            CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED, _WHY_ROW_UNBOUND
        )
    client = snapshot.clients.get(session.client)
    if client is None:
        return _Resolution(CodexLegacyDisposition.FAILED, REASON_CLIENT_MISSING)
    if resolve_executor_config(task.stage, task, client).backend != CODEX_BACKEND:
        return _Resolution(
            CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED if retrying else None,
            _WHY_NOT_CODEX,
        )
    return task, client


def _scan_for_writer(candidate: _Candidate) -> Path | _Resolution:
    """The worktree to act on, or why the session must wait for its writer.

    A cannot-tell context parks without a scan; otherwise the boot pass's own
    live-writer scan runs, unlocked, as the boot pass runs it.
    """
    worktree = candidate.session.worktree_path
    if candidate.kind is not _HeadlessScanKind.HEADLESS or worktree is None:
        return _Resolution(
            CodexLegacyDisposition.SKIPPED_WRITER_LIVE, candidate.kind.value
        )
    if live_writer_park(worktree) is not None:
        return _Resolution(
            CodexLegacyDisposition.SKIPPED_WRITER_LIVE, REASON_LIVE_WRITER
        )
    return worktree


# --------------------------------------------------------------------------- #
# Revalidate and act (locked)
# --------------------------------------------------------------------------- #


def _bound_row(ticket_id: str, session: Session) -> TicketTask | None:
    """The row still claimed by *session* (caller holds ``dev_queue_lock``)."""
    return next(
        (
            task
            for task in load_dev_queue().tasks
            if task.ticket_id == ticket_id
            and task.client == session.client
            and _row_owned_by(task, session.id)
        ),
        None,
    )


def _revalidate_and_act(
    candidate: _Candidate,
    client: ClientConfig,
    snapshot: _Snapshot,
    *,
    worktree: Path,
    now: datetime,
) -> _Resolution:
    """Re-read the session and row under lock, then gate, audit and close it.

    Holding ``sessions_lock`` across the act excludes every session writer,
    the boot sweep included: whichever of the two runs second sees the
    session no longer live and leaves it alone. ``dev_queue_lock`` cannot
    span the act (its revert/park helpers take it, and it is not
    reentrant), so a row moved in that window is caught by their own
    ``expected_session_id`` re-check and comes back ``TRANSITION_LOST``.
    """
    snapshot_session = candidate.session
    # Why not mutate_state: act_on_codex_harvest_candidate mutates the passed
    # session and saves the caller's `state` with save_state, which takes no
    # lock, and it never re-reads the session. sessions_lock must therefore
    # span the revalidation AND the act, which mutate_state's load-mutate-save
    # callback cannot express. dev_queue_lock nests inside it for revalidation
    # only, in codex_boot._close_or_propose_reap's order.
    with sessions_lock():
        state = load_state()
        fresh = next((s for s in state.sessions if s.id == snapshot_session.id), None)
        if fresh is None:
            return _Resolution(
                CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED, _WHY_SESSION_GONE
            )
        with dev_queue_lock():
            task = _bound_row(candidate.ticket_id, fresh)
            stale = stale_snapshot_reason(fresh, snapshot_session, candidate.ticket_id)
        if stale is not None or task is None:
            return _Resolution(
                CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED,
                stale or _WHY_ROW_UNBOUND,
            )
        try:
            outcome = act_on_codex_harvest_candidate(
                state,
                fresh,
                None,
                task,
                client,
                snapshot.clients,
                snapshot.config,
                worktree=worktree,
                now=now,
                legacy_reason=CODEX_LEGACY_RECOVERY_REASON,
            )
        except OSError:
            _log.exception(
                "codex_legacy_recovery: could not persist the recovery of session"
                " %s (%s/%s); it stays unresolved",
                fresh.id,
                fresh.client,
                candidate.ticket_id,
            )
            return _Resolution(CodexLegacyDisposition.FAILED, REASON_STATE_WRITE_FAILED)
    return _ACT_RESOLUTIONS[outcome]


def _recover(
    candidate: _Candidate, snapshot: _Snapshot, *, now: datetime, retrying: bool
) -> _Resolution:
    classified = _classify(candidate, snapshot, retrying=retrying)
    if isinstance(classified, _Resolution):
        return classified
    _task, client = classified
    scan = _scan_for_writer(candidate)
    if isinstance(scan, _Resolution):
        return scan
    return _revalidate_and_act(candidate, client, snapshot, worktree=scan, now=now)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def _log_resolution(
    session_id: str, client: str, ticket_id: str, resolution: _Resolution
) -> None:
    if resolution.disposition in _ACTED_DISPOSITIONS:
        return
    _log.warning(
        "codex_legacy_recovery: session %s (%s/%s) %s: %s",
        session_id,
        client,
        ticket_id,
        resolution.disposition,
        resolution.reason,
    )


def _recover_all(
    marker: CodexLegacyRecoveryMarker, snapshot: _Snapshot, now: datetime
) -> None:
    retry_ids = {entry.session_id for entry in marker.unresolved}
    candidates, gone = _work_list(snapshot, marker, retry_ids)
    _log.info(
        "codex_legacy_recovery: %d candidate session(s), %d no longer eligible",
        len(candidates),
        len(gone),
    )
    for prior in gone:
        resolution = _Resolution(
            CodexLegacyDisposition.SKIPPED_ALREADY_HANDLED, _WHY_NO_LONGER_ELIGIBLE
        )
        _log_resolution(prior.session_id, prior.client, prior.ticket_id, resolution)
        _record_outcome(
            marker,
            prior.model_copy(update={"disposition": resolution.disposition}),
            None,
        )
    for candidate in candidates:
        session = candidate.session
        resolution = _recover(
            candidate, snapshot, now=now, retrying=session.id in retry_ids
        )
        if resolution.disposition is None:
            continue
        _log_resolution(session.id, session.client, candidate.ticket_id, resolution)
        row = snapshot.tasks.get((candidate.ticket_id, session.client))
        outcome = Outcome(
            session_id=session.id,
            ticket_id=candidate.ticket_id,
            client=session.client,
            disposition=resolution.disposition,
            prior_status=session.status,
            prior_stage=row.stage if row is not None else None,
        )
        _record_outcome(marker, outcome, resolution.reason)


def _finalize(marker: CodexLegacyRecoveryMarker, now: datetime) -> LegacyRecoveryReport:
    status = LegacyRecoveryStatus.PARTIAL
    if not marker.unresolved:
        marker.completed_at = now
        status = LegacyRecoveryStatus.COMPLETED
    save_codex_legacy_marker(marker)
    _log.info(
        "codex_legacy_recovery: %s (scanned=%d requeued=%d parked=%d failed=%d"
        " skipped_already_handled=%d skipped_writer_live=%d)",
        status,
        marker.scanned,
        marker.requeued,
        marker.parked,
        marker.failed,
        marker.skipped_already_handled,
        marker.skipped_writer_live,
    )
    return LegacyRecoveryReport(status=status, marker=marker)


def run_codex_legacy_recovery(now: datetime | None = None) -> LegacyRecoveryReport:
    """Recover every live legacy codex session once; see the module docstring.

    Raises ``CodexLegacyRecoveryMarkerError`` on a corrupt marker, before
    anything is scanned or mutated.
    """
    run_at = now if now is not None else datetime.now(UTC)
    with codex_legacy_marker_lock():
        existing = load_codex_legacy_marker()
        if existing is not None and existing.completed_at is not None:
            _log.info(
                "codex_legacy_recovery: already completed at %s",
                existing.completed_at.isoformat(),
            )
            return LegacyRecoveryReport(
                status=LegacyRecoveryStatus.ALREADY_COMPLETED, marker=existing
            )
        marker = existing if existing is not None else CodexLegacyRecoveryMarker()
        _recover_all(marker, _load_snapshot(), run_at)
        return _finalize(marker, run_at)


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #


def _counts(marker: CodexLegacyRecoveryMarker) -> dict[str, int]:
    return {
        "scanned": marker.scanned,
        "requeued": marker.requeued,
        "parked": marker.parked,
        "failed": marker.failed,
        "skipped_already_handled": marker.skipped_already_handled,
        "skipped_writer_live": marker.skipped_writer_live,
    }


def _headline(report: LegacyRecoveryReport) -> str:
    completed_at = report.marker.completed_at
    if completed_at is None:
        return (
            "codex legacy recovery partial:"
            f" {len(report.marker.unresolved)} session(s) unresolved;"
            " resolve each cause below and re-run"
        )
    verb = (
        "already completed"
        if report.status is LegacyRecoveryStatus.ALREADY_COMPLETED
        else "completed"
    )
    return f"codex legacy recovery {verb} at {completed_at.isoformat()}"


def format_report_text(report: LegacyRecoveryReport) -> str:
    """Human-readable report: headline, the six counts, any unresolved sessions."""
    lines = [_headline(report)]
    lines.extend(f"  {name}: {value}" for name, value in _counts(report.marker).items())
    if report.marker.unresolved:
        lines.append("unresolved:")
        lines.extend(
            f"  {entry.session_id} ({entry.client}/{entry.ticket_id}): {entry.reason}"
            for entry in report.marker.unresolved
        )
    return "\n".join(lines)


def format_report_json(report: LegacyRecoveryReport) -> str:
    """``{"status", "counts", "unresolved"}`` for scripts and the B2 gate."""
    return json.dumps(
        {
            "status": report.status.value,
            "counts": _counts(report.marker),
            "unresolved": [
                entry.model_dump(mode="json") for entry in report.marker.unresolved
            ],
        },
        indent=2,
    )
