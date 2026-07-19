"""Dev-queue persistence: file locks + plan/queue load & save.

Extracted from the flat ``cw.dev_queue`` module (#1317, part 1). Owns the
on-disk layer: the ``_lock`` / ``_plan_lock`` file-lock context managers (plus
the public ``dev_queue_lock`` alias), ``plan_path`` / ``save_plan`` /
``load_plan`` for the dispatch plan, and ``load_dev_queue`` / ``save_dev_queue``
for the queue store. ``load_dev_queue`` normalises the raw payload via
``cw.dev_queue.migrate.migrate_dev_queue``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text, rotate_backup
from cw.config import (
    dev_plan_file,
    dev_plan_lock,
    dev_queue_file,
    refuse_real_state_write,
)
from cw.config import (
    dev_queue_lock as _dev_queue_lock_file,
)
from cw.dev_queue.migrate import migrate_dev_queue
from cw.models import DevQueueStore, DispatchPlan

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dev queue."""
    dev_queue_file().parent.mkdir(parents=True, exist_ok=True)
    fd = _dev_queue_lock_file().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


# Public alias for callers that need the dev-queue lock directly (e.g. the
# reconciler, which needs load → mutate → save around a RUNNING→PENDING
# revert). Prefer higher-level helpers like ``add_ticket`` when available.
dev_queue_lock = _lock


@contextlib.contextmanager
def _plan_lock() -> Iterator[None]:
    """Acquire an exclusive file lock for the dispatch plan."""
    dev_plan_file().parent.mkdir(parents=True, exist_ok=True)
    fd = dev_plan_lock().open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def plan_path() -> Path:
    """Return the path to the persisted dispatch plan file."""
    return dev_plan_file()


def save_plan(plan: DispatchPlan) -> Path:
    """Persist a DispatchPlan to disk under the plan file lock.

    Returns the path the plan was written to.
    """
    with _plan_lock():
        path = dev_plan_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, plan.model_dump_json(indent=2))
    return path


def load_plan() -> DispatchPlan | None:
    """Load the persisted DispatchPlan, returning None if missing.

    Returns None if the plan file does not exist or fails validation.
    Does not raise on validation errors — callers should fall back to
    enqueue order when None is returned.
    """
    path = dev_plan_file()
    if not path.exists():
        return None
    try:
        return DispatchPlan.model_validate_json(path.read_text())
    except (ValueError, OSError):
        return None


def load_dev_queue() -> DevQueueStore:
    """Load the dev queue from disk, returning an empty store if missing."""
    path = dev_queue_file()
    if not path.exists():
        return DevQueueStore()
    raw = json.loads(path.read_text())
    return DevQueueStore.model_validate(migrate_dev_queue(raw))


def save_dev_queue(store: DevQueueStore) -> None:
    """Persist the dev queue to disk atomically.

    Write-ahead backs up the previous on-disk payload before overwriting,
    rotating out anything past the last _DEFAULT_BACKUP_KEEP snapshots
    (GitHub #1017) — the only reason the Jul 2026 GEN-A/GEN-B clobber
    incident was recoverable was a manually-made backup; this makes that
    protection automatic.
    """
    path = dev_queue_file()
    refuse_real_state_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_backup(path)
    atomic_write_text(path, store.model_dump_json(indent=2))
