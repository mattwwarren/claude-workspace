"""Refresh a row's snapshot at stage entry, and fall back when that fails.

RFC 0019 T2 (#3712), R6 and R9. Later stages run on the snapshot: REVIEW (per
pass), codex IMPL and codex FINALIZE call :func:`snapshot_for_stage`, which
refreshes the snapshot from the tracker, and on any failure falls back to the
snapshot the queue row carries NOW (never the stale in-memory task). A stage
never parks because a refresh failed. Exactly one ``ticket.snapshot_degraded``
event is recorded per degraded call; it is deliberately not forwarded to the
operator channel (per-pass noise).

Two refresh branches exist. GitHub (``cli: gh``) fetches through ``gh``. Any
other tracker refreshes through an injected :data:`CourierFetch` (the MCP
courier turn of #3760, built in ``cw.tracker_io``, which this package cannot
import without a cycle); with no courier it resolves to ``no_binding`` and runs
on its last snapshot. The courier raises :class:`RefreshError` for
``turn_failed``, ``courier_unreachable``, ``refused`` and ``no_binding``.

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
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import ClientConfig, TicketTask
    from cw.ticket_snapshot._models import TicketSnapshot

    #: ``courier(task, client)``: one MCP refresh turn's snapshot, or RefreshError.
    CourierFetch = Callable[[TicketTask, ClientConfig], TicketSnapshot]

_log = logging.getLogger("cw.ticket_snapshot")

#: The closed refresh-failure vocabulary (R6). ``gh_failed`` is GitHub-only;
#: ``turn_failed`` and ``courier_unreachable`` come only from the MCP courier
#: (#3760), which shares ``no_binding``, ``refused`` and ``cap`` with the rest.
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


def _fetch_github(task: TicketTask, worktree: Path) -> TicketSnapshot:
    """Fetch *task*'s issue through ``gh``; a failure is ``gh_failed``."""
    try:
        return snapshot_from_github(task.ticket_id, task.client, worktree)
    except SnapshotRefusedError as exc:
        msg = "gh could not fetch the ticket for a snapshot refresh"
        raise RefreshError(msg, reason="gh_failed") from exc


def _record_and_render(
    snapshot: TicketSnapshot,
    worktree: Path,
    *,
    session_id: str | None,
    detect_author_conflicts: bool,
) -> TicketSnapshot:
    """Record *snapshot*, stamp the row and re-render ``.cw/context.json``.

    ``cap`` when a size or count cap refuses it, ``refused`` on any other store
    or render error. The one tail shared by the GitHub and courier branches.
    """
    try:
        record = record_snapshot(
            snapshot, refresh=True, detect_author_conflicts=detect_author_conflicts
        )
        # The stored snapshot, not the input: it carries any author-conflict
        # flags the record computed, so this pass cannot bind a conflicted one.
        snapshot = record.snapshot
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


def refresh_snapshot(
    task: TicketTask,
    client: ClientConfig,
    worktree: Path,
    *,
    stage: str,
    session_id: str | None = None,
    courier: CourierFetch | None = None,
) -> TicketSnapshot:
    """Fetch, record and render a fresh snapshot of *task*'s ticket.

    Returns the in-memory snapshot. Raises :class:`RefreshError`: ``gh_failed``
    when the GitHub fetch fails, ``cap`` when the record step refuses it (size
    or count cap), ``refused`` on an unexpected store error, ``no_binding`` on a
    non-GitHub tracker with no *courier*. A non-GitHub tracker with a *courier*
    fetches through it (its own :class:`RefreshError` propagates) and records
    with author-conflict detection on, the same trust class as
    ``cw ticket snapshot --from-json`` (a model-reported ``author_id``); GitHub
    records with it off. ``.cw/context.json`` in *worktree* is re-rendered.
    """
    github = is_github(client)
    if github:
        snapshot = _fetch_github(task, worktree)
    elif courier is not None:
        snapshot = courier(task, client)
    else:
        msg = f"no refresh binding for the {stage} stage on this tracker"
        raise RefreshError(msg, reason="no_binding")
    return _record_and_render(
        snapshot,
        worktree,
        session_id=session_id,
        detect_author_conflicts=not github,
    )


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
    courier: CourierFetch | None = None,
) -> TicketSnapshot | None:
    """Refresh, else fall back to the current row's snapshot. Never raises.

    Returns the fresh snapshot, else the last one the row carries, else None
    (no digest, or the stored snapshot failed the trust read). Every fallback
    records exactly one ``ticket.snapshot_degraded``: ``read_untrusted`` when
    the fallback read fails or finds no digest, otherwise ``refresh_failed``.
    What a stage does with None is the caller's decision.
    """
    try:
        return refresh_snapshot(task, client, worktree, stage=stage, courier=courier)
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
