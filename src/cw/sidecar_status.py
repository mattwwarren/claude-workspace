"""Shared read/write/cadence helpers for one-record state-dir sidecars (#3159).

Two dispatch-loop post-tick steps keep a small JSON record in the state dir that
is both their cadence gate (``last_run_at``) and the record ``cw doctor`` reads:
:mod:`cw.session_auto_prune` (``sessions_auto_prune.json``) and
:mod:`cw.worker_tmp_retention` (``worker_tmp_retention.json``). This module is the
one implementation of that shape, extracted from their two verbatim copies.

Reads fail open (a missing, unreadable or corrupt record is None, so the step
simply runs). A write failure is logged against the caller's logger and never
raised: it would otherwise fail a run that already did its work.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel

from cw.atomic import atomic_write_text
from cw.config import state_dir

if TYPE_CHECKING:
    import logging
    from pathlib import Path


def sidecar_path(filename: str) -> Path:
    """Return the state-dir path of the sidecar named *filename*."""
    return state_dir() / filename


def read_sidecar[S: BaseModel](filename: str, model: type[S]) -> S | None:
    """Return the record in *filename* parsed as *model*, or None (fail-open).

    None covers a missing file, any ``OSError`` (a directory in its place, a
    permission error) and any ``ValueError`` (corrupt JSON, a schema mismatch).
    """
    try:
        return model.model_validate_json(sidecar_path(filename).read_text())
    except (OSError, ValueError):
        return None


def write_sidecar(filename: str, status: BaseModel, log: logging.Logger) -> None:
    """Persist *status* to *filename* atomically; a write failure is logged.

    *log* is the caller's logger, so the warning is attributed to the step whose
    record could not be written.
    """
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_text(sidecar_path(filename), status.model_dump_json(indent=2))
    except OSError:
        log.warning("could not write %s; continuing", filename, exc_info=True)


def is_due(last_run_at: datetime | None, interval: timedelta, now: datetime) -> bool:
    """True when no run is recorded or *interval* has elapsed since *last_run_at*.

    A *last_run_at* in the future (clock skew, a restored sidecar) counts as due,
    so a bad timestamp cannot stall the schedule.
    """
    return last_run_at is None or not timedelta(0) <= now - last_run_at < interval
