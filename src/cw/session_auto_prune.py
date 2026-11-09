"""Automatic bounded pruning of terminal sessions (#2965, half of #2637).

``sessions.json`` is parsed whole on every ``load_state()``, and terminal
sessions accumulate forever, so the dispatch loop prunes it on a schedule
instead of waiting for an operator to run ``cw session prune``. This module is
the thin scheduling wrapper: all selection and archival stay in
:func:`cw.session_retention.prune_sessions`.

The caller is the dispatch loop's guarded post-tick step
(``cw.dispatch.loop._run_sessions_auto_prune_guarded``), which holds no guarded
lock, so the one bounded ``sessions_lock`` inside ``prune_sessions`` is the
only hold. Nothing here takes a lock itself.

Each due run records a small ``sessions_auto_prune.json`` sidecar in the state
dir. It is both the cadence gate (``last_run_at``) and the record
``cw doctor``'s ``sessions-auto-prune`` check reads: the events inbox cannot
serve either role, because it is trimmed and a failed or empty run emits no
event. A run that archived or deleted at least one session also emits one
``sessions.auto_pruned`` audit event.

A lock-discipline violation (``CwLockReentrancyError``, ``CwLockOrderError``)
is a programming error, not an operational failure: it is re-raised, never
recorded as a failed run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from cw.atomic import atomic_write_text
from cw.config import state_dir
from cw.events import record_event
from cw.exceptions import CwError, CwLockOrderError, CwLockReentrancyError
from cw.models import OrchestratorEventType
from cw.session_retention import SessionPruneResult, prune_sessions

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import OrchestratorConfig

_log = logging.getLogger("cw.session_auto_prune")

_STATUS_FILENAME = "sessions_auto_prune.json"


class AutoPruneStatus(BaseModel):
    """The sidecar record of the most recent automatic prune run."""

    last_run_at: datetime
    status: Literal["ok", "failed"]
    archived_count: int = 0
    deleted_count: int = 0
    kept_count: int = 0
    cutoff: datetime | None = None
    age_days: int
    max_per_run: int
    capped: bool = False
    bytes_before: int = 0
    bytes_after: int = 0
    error: str | None = None


def _status_path() -> Path:
    return state_dir() / _STATUS_FILENAME


def read_auto_prune_status() -> AutoPruneStatus | None:
    """Return the last run's record, or None when absent or unreadable (fail-open)."""
    try:
        return AutoPruneStatus.model_validate_json(_status_path().read_text())
    except (OSError, ValueError):
        return None


def _write_status(status: AutoPruneStatus) -> None:
    """Persist *status* atomically; a write failure is logged, never raised."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_text(_status_path(), status.model_dump_json(indent=2))
    except OSError:
        _log.warning("could not write %s; continuing", _STATUS_FILENAME, exc_info=True)


def _is_due(config: OrchestratorConfig, now: datetime) -> bool:
    """True when no run is recorded or the interval has elapsed since the last.

    A ``last_run_at`` in the future (clock skew, a restored sidecar) counts as
    due, so a bad timestamp cannot stall the schedule.
    """
    last = read_auto_prune_status()
    if last is None:
        return True
    interval = timedelta(minutes=config.sessions_auto_prune_interval_minutes)
    return not timedelta(0) <= now - last.last_run_at < interval


def _emit_event(config: OrchestratorConfig, result: SessionPruneResult) -> None:
    """Record the ``sessions.auto_pruned`` audit event for a run that pruned."""
    payload = {
        "archived_count": result.archived_count,
        "deleted_count": result.deleted_count,
        "kept_count": result.kept_count,
        "cutoff": result.cutoff.isoformat(),
        "age_days": config.sessions_auto_prune_age_days,
        "max_per_run": config.sessions_auto_prune_max_per_run,
        "capped": result.capped,
        "bytes_before": result.bytes_before,
        "bytes_after": result.bytes_after,
        "archive_path": result.archive_path,
    }
    try:
        record_event(OrchestratorEventType.SESSIONS_AUTO_PRUNED, payload)
    except OSError:
        _log.warning("failed to record sessions.auto_pruned event", exc_info=True)


def auto_prune_sessions(
    config: OrchestratorConfig, *, now: datetime | None = None
) -> SessionPruneResult | None:
    """Run one automatic prune if enabled and due; return its result or None.

    None means disabled, not yet due, or the run failed (recorded in the
    sidecar, never raised). The caller must hold no guarded lock: the single
    bounded ``sessions_lock`` hold happens inside ``prune_sessions``.
    """
    if not config.sessions_auto_prune_enabled:
        return None
    run_at = now or datetime.now(UTC)
    if not _is_due(config, run_at):
        return None

    age_days = config.sessions_auto_prune_age_days
    max_per_run = config.sessions_auto_prune_max_per_run
    try:
        result = prune_sessions(
            before=run_at - timedelta(days=age_days),
            archive=True,
            max_sessions=max_per_run,
        )
    except (CwLockReentrancyError, CwLockOrderError):
        raise
    except (CwError, OSError, ValueError) as exc:
        _log.exception("automatic sessions prune failed; will retry next interval")
        _write_status(
            AutoPruneStatus(
                last_run_at=run_at,
                status="failed",
                age_days=age_days,
                max_per_run=max_per_run,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        return None

    _write_status(
        AutoPruneStatus(
            last_run_at=run_at,
            status="ok",
            archived_count=result.archived_count,
            deleted_count=result.deleted_count,
            kept_count=result.kept_count,
            cutoff=result.cutoff,
            age_days=age_days,
            max_per_run=max_per_run,
            capped=result.capped,
            bytes_before=result.bytes_before,
            bytes_after=result.bytes_after,
        )
    )
    if result.archived_count + result.deleted_count > 0:
        _emit_event(config, result)
    return result
