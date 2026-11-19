"""Refresh a row's snapshot at stage entry, and fall back when that fails.

RFC 0019 T2 (#3712), R6 and R9. Later stages run on the snapshot: REVIEW (per
pass), codex IMPL and codex FINALIZE call :func:`snapshot_for_stage`, which
refreshes the snapshot from the tracker, and on any failure falls back to the
snapshot the queue row carries NOW (never the stale in-memory task). A stage
never parks because a refresh failed. Exactly one ``ticket.snapshot_degraded``
event is recorded per degraded call; it is deliberately not forwarded to the
operator channel (per-pass noise).

Only the GitHub (``cli: gh``) branch exists here. Every other tracker resolves
to ``no_binding`` and runs on its last snapshot; the MCP courier refresh is
#3760, which also owns the reserved ``turn_failed``, ``courier_unreachable``
and courier ``refused`` reasons.

Never called from the dispatch tick or ``reconcile()`` (pinned by
``tests/test_refresh_never_on_tick.py``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, Literal, get_args

from cw.events import record_event
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.ticket_snapshot._fetch import snapshot_from_github
from cw.ticket_snapshot._models import (
    SnapshotRefusedError,
    SnapshotUntrustedError,
    digest_or_none,
)
from cw.ticket_snapshot._record import current_row, record_snapshot
from cw.ticket_snapshot._render import render_context, write_context_json
from cw.ticket_snapshot._store import read
from cw.tracker import TRACKER_GITHUB_ISSUES, resolve_tracker

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.ticket_snapshot._models import TicketSnapshot

_log = logging.getLogger("cw.ticket_snapshot")

#: The closed refresh-failure vocabulary (R6). The last three are reserved for
#: the MCP courier refresh (#3760); this module raises only the first four.
RefreshReason = Literal[
    "gh_failed",
    "cap",
    "no_binding",
    "refused",
    "turn_failed",
    "courier_unreachable",
]
REFRESH_REASONS: Final[tuple[str, ...]] = get_args(RefreshReason)

#: ``kind`` of a ``ticket.snapshot_degraded`` event.
DegradedKind = Literal["refresh_failed", "read_untrusted"]


class RefreshError(CwError):
    """A refresh did not produce a recorded snapshot; ``reason`` names why."""

    reason: RefreshReason

    def __init__(self, message: str, *, reason: RefreshReason) -> None:
        super().__init__(message)
        self.reason = reason


def is_github(client: ClientConfig) -> bool:
    """True when *client* tracks on ``github-issues`` (exact; unset is not)."""
    return resolve_tracker(client.workspace_path) == TRACKER_GITHUB_ISSUES


def refresh_snapshot(
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    *,
    stage: str,
    session_id: str | None = None,
) -> TicketSnapshot:
    """Fetch, record and render a fresh snapshot of *task*'s ticket.

    Returns the in-memory snapshot. Raises :class:`RefreshError`: ``gh_failed``
    when the fetch fails, ``cap`` when the record step refuses it (size or
    count cap), ``refused`` on an unexpected store error, ``no_binding`` on a
    non-GitHub tracker. ``.cw/context.json`` in *worktree* is re-rendered.
    """
    if not is_github(client):
        msg = f"no refresh binding for the {stage} stage on this tracker"
        raise RefreshError(msg, reason="no_binding")
    try:
        snapshot = snapshot_from_github(task.ticket_id, task.client, worktree)
    except SnapshotRefusedError as exc:
        msg = "gh could not fetch the ticket for a snapshot refresh"
        raise RefreshError(msg, reason="gh_failed") from exc
    try:
        record = record_snapshot(snapshot, refresh=True)
        rendered = render_context(
            snapshot, record.snapshot_digest, materialized_by_session=session_id
        )
        write_context_json(worktree, rendered)
    except SnapshotRefusedError as exc:
        msg = "the refreshed snapshot was refused by a size or count cap"
        raise RefreshError(msg, reason="cap") from exc
    except (CwError, OSError, ValueError) as exc:
        msg = f"snapshot record step failed ({type(exc).__name__})"
        raise RefreshError(msg, reason="refused") from exc
    return snapshot


def _row_of(task: TicketTask) -> TicketTask | None:
    """The current queue row for *task*, or None when no row matches."""
    return current_row(task.ticket_id, task.client)


def read_current(task: TicketTask) -> TicketSnapshot | None:
    """The snapshot the row carries now (unlocked load), or None with no digest.

    Raises :class:`SnapshotUntrustedError` as ``read`` does.
    """
    row = _row_of(task)
    return None if row is None else read(row)


def emit_degraded(
    row: TicketTask,
    *,
    kind: DegradedKind,
    reason: str,
    stage: str,
) -> None:
    """Log and record one ``ticket.snapshot_degraded``. Never raises.

    ``ticket_id``, ``client`` and the digest come from the row; no ticket text.
    """
    digest = digest_or_none(row.snapshot_digest)
    _log.warning(
        "ticket snapshot degraded ticket_id=%s client=%s kind=%s reason=%s stage=%s",
        row.ticket_id,
        row.client,
        kind,
        reason,
        stage,
    )
    try:
        record_event(
            OrchestratorEventType.TICKET_SNAPSHOT_DEGRADED,
            {
                "ticket_id": row.ticket_id,
                "client": row.client,
                "kind": kind,
                "reason": reason,
                "snapshot_digest": digest,
                "stage": stage,
            },
            correlation_id=row.ticket_id,
        )
    except (CwError, OSError):
        _log.warning(
            "ticket.snapshot_degraded event not recorded ticket_id=%s",
            row.ticket_id,
        )


def _read_or_degrade(row: TicketTask, *, stage: str) -> TicketSnapshot | None:
    """``read(row)``; an untrusted read records ``read_untrusted`` and is None."""
    try:
        return read(row)
    except SnapshotUntrustedError as exc:
        emit_degraded(row, kind="read_untrusted", reason=exc.reason, stage=stage)
    except OSError:
        emit_degraded(row, kind="read_untrusted", reason="invalid", stage=stage)
    return None


def read_for_stage(task: TicketTask, stage: str) -> TicketSnapshot | None:
    """The current row's snapshot with no refresh; None binds nothing.

    The default resolver of :class:`~cw.ticket_snapshot.SnapshotContext`. A
    row with no digest is None silently; an untrusted read records one
    ``read_untrusted`` event.
    """
    row = _row_of(task)
    return None if row is None else _read_or_degrade(row, stage=stage)


def snapshot_for_stage(
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    *,
    stage: str,
) -> TicketSnapshot | None:
    """Refresh, else fall back to the current row's snapshot. Never raises.

    Returns the fresh snapshot, else the last one the row carries, else None
    (no digest, or the stored snapshot failed the trust read). Every fallback
    records exactly one ``ticket.snapshot_degraded``: ``read_untrusted`` when
    the fallback read fails or finds no digest, otherwise ``refresh_failed``.
    What a stage does with None is the caller's decision.
    """
    try:
        return refresh_snapshot(task, client, worktree, stage=stage)
    except RefreshError as exc:
        reason: str = exc.reason
    _log.warning(
        "ticket snapshot refresh failed; falling back to the row snapshot"
        " ticket_id=%s client=%s reason=%s stage=%s",
        task.ticket_id,
        task.client,
        reason,
        stage,
    )
    row = _row_of(task)
    if row is None:
        # The task object may carry an old digest, but with no current queue
        # row there is no trusted snapshot to fall back to.
        row = task.model_copy(update={"snapshot_digest": None})
    if row.snapshot_digest is None:
        emit_degraded(row, kind="read_untrusted", reason="missing", stage=stage)
        return None
    snapshot = _read_or_degrade(row, stage=stage)
    if snapshot is not None:
        emit_degraded(row, kind="refresh_failed", reason=reason, stage=stage)
    return snapshot
