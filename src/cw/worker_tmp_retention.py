"""Retention sweep for the shared ``~/.cw/tmp`` fallback worker TMPDIR (#2682).

A worker only lands here when its per-worktree ``.cw/tmp`` env block is missing
or not honored (#2672), and nothing else ever removes what it leaves behind. The
dir is one flat shared tree, so ownership by a live worker cannot be read from
its layout. The sweep therefore protects, on Linux, every path a live process
holds open or as its ``cwd`` (plus that path's ancestors up to the root), and
ages out everything else by ``max(mtime, ctime)``. Where ``/proc`` is unreadable
it falls back to age alone, and the status record says so.

The caller is the dispatch loop's guarded post-tick step
(``cw.dispatch.loop._run_worker_tmp_retention_guarded``), which holds no guarded
lock. Nothing here takes a lock or runs a subprocess. Like the sessions
auto-prune, each due run writes a ``worker_tmp_retention.json`` sidecar that is
both the cadence gate and the record ``cw doctor`` reads. This sweep is not a
``reap_policy`` act: it is gated only by its own enable flag.

A lock-discipline violation (``CwLockReentrancyError``, ``CwLockOrderError``) is
re-raised, never recorded as a failed run.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from pydantic import BaseModel

from cw.atomic import atomic_write_text
from cw.config import state_dir
from cw.exceptions import CwError, CwLockOrderError, CwLockReentrancyError
from cw.worktree import resolve_fallback_worker_tmpdir

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig

_log = logging.getLogger("cw.worker_tmp_retention")

_STATUS_FILENAME = "worker_tmp_retention.json"
_PROC_ROOT = Path("/proc")

# Removals per run. Bounds the wall time of one dispatch-loop step; the rest is
# picked up by the next run, and the record shows ``capped``.
MAX_REMOVALS_PER_RUN = 5000
# Entries the scan will visit per run. The scan runs on the dispatch-loop
# thread, so it is bounded too; hitting this sets ``capped``.
MAX_SCAN_ENTRIES = 200_000
# Entries ``measure_fallback_tmp`` will count before reporting ``truncated``.
MEASURE_LIMIT = 50_000

_NOT_EMPTY_ERRNOS = frozenset({errno.ENOTEMPTY, errno.EEXIST})


class RetentionStatus(BaseModel):
    """The sidecar record of the most recent fallback-tmp retention run."""

    last_run_at: datetime
    status: Literal["ok", "failed"]
    removed_files: int = 0
    removed_dirs: int = 0
    freed_bytes: int = 0
    kept_in_use: int = 0
    errors: int = 0
    retention_days: int
    capped: bool = False
    proc_guard: bool = False
    error: str | None = None


@dataclass
class SweepResult:
    """What one sweep did. ``proc_guard`` is False when ``/proc`` was unreadable."""

    removed_files: int = 0
    removed_dirs: int = 0
    freed_bytes: int = 0
    kept_in_use: int = 0
    errors: int = 0
    capped: bool = False
    proc_guard: bool = False


class TmpUsage(NamedTuple):
    """Bounded measurement of the fallback dir for ``cw doctor``."""

    files: int
    bytes: int
    truncated: bool


def _lstat(path: str | os.PathLike[str]) -> os.stat_result:
    """``os.lstat``, behind a seam so tests can model a race or a cross-device entry."""
    return os.lstat(path)


def _status_path() -> Path:
    return state_dir() / _STATUS_FILENAME


def read_retention_status() -> RetentionStatus | None:
    """Return the last run's record, or None when absent or unreadable (fail-open)."""
    try:
        return RetentionStatus.model_validate_json(_status_path().read_text())
    except (OSError, ValueError):
        return None


def _write_status(status: RetentionStatus) -> None:
    """Persist *status* atomically; a write failure is logged, never raised."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_text(_status_path(), status.model_dump_json(indent=2))
    except OSError:
        _log.warning("could not write %s; continuing", _STATUS_FILENAME, exc_info=True)


def _is_due(config: OrchestratorConfig, now: datetime) -> bool:
    """True when no run is recorded or the interval has elapsed since the last.

    A ``last_run_at`` in the future counts as due, so a bad timestamp cannot
    stall the schedule.
    """
    last = read_retention_status()
    if last is None:
        return True
    interval = timedelta(minutes=config.worker_tmp_fallback_retention_interval_minutes)
    return not timedelta(0) <= now - last.last_run_at < interval


def _is_stale(st: os.stat_result, cutoff_ts: float) -> bool:
    """True when the entry's newer of mtime and ctime predates the cutoff."""
    return max(st.st_mtime, st.st_ctime) < cutoff_ts


def _check_root(root: Path) -> None:
    """Refuse a root that is a symlink or not a directory (never swept through)."""
    if root.is_symlink():
        msg = f"fallback tmp root {root} refused: is a symlink"
        raise ValueError(msg)
    if not root.is_dir():
        msg = f"fallback tmp root {root} refused: not a directory"
        raise ValueError(msg)


def _scan(
    root: Path,
    root_dev: int,
    cutoff_ts: float,
    protected: frozenset[Path] | None,
    result: SweepResult,
) -> list[tuple[Path, bool]]:
    """Collect stale, unprotected entries as ``(path, is_real_dir)`` in walk order.

    Entries on another device are skipped (and not descended into). Symlinks are
    never followed: a symlink is listed as a non-directory, so it is unlinked
    rather than swept through. Protected stale entries are counted, not listed.
    """
    entries: list[tuple[Path, bool]] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in [*dirnames, *filenames]:
            if seen >= MAX_SCAN_ENTRIES:
                result.capped = True
                dirnames.clear()
                return entries
            seen += 1
            path = base / name
            try:
                st = _lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                result.errors += 1
                continue
            if st.st_dev != root_dev:
                if name in dirnames:
                    dirnames.remove(name)
                continue
            is_real_dir = stat.S_ISDIR(st.st_mode)
            if not _is_stale(st, cutoff_ts):
                continue
            if protected is not None and path in protected:
                result.kept_in_use += 1
                continue
            entries.append((path, is_real_dir))
    return entries


def _remove_file(path: Path, cutoff_ts: float, result: SweepResult) -> None:
    """Unlink one stale file, re-checking its age first to close the race."""
    try:
        st = _lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        result.errors += 1
        return
    if not _is_stale(st, cutoff_ts):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        result.errors += 1
        return
    result.removed_files += 1
    if not stat.S_ISLNK(st.st_mode):
        result.freed_bytes += st.st_size


def _remove_dir(path: Path, result: SweepResult) -> None:
    """Remove a dir that was stale at scan time, only if it is now empty.

    No age re-check: unlinking its children bumped its mtime, so it would
    look fresh. ``rmdir`` itself refuses a non-empty dir, which is the guard.
    """
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in _NOT_EMPTY_ERRNOS:
            return
        result.errors += 1
        return
    result.removed_dirs += 1


def sweep_fallback_tmp(
    root: Path, *, cutoff: datetime, protected: frozenset[Path] | None
) -> SweepResult:
    """Remove stale, unprotected entries under *root*; never remove *root* itself.

    A missing root is a zero result. A root that is a symlink or not a directory
    raises ``ValueError`` (recorded as a failed run by the caller). Entries are
    scanned top-down, then removed deepest-first so an emptied dir can follow
    its children.
    """
    result = SweepResult(proc_guard=protected is not None)
    if not os.path.lexists(root):
        return result
    _check_root(root)
    cutoff_ts = cutoff.timestamp()
    root_dev = _lstat(root).st_dev
    entries = _scan(root, root_dev, cutoff_ts, protected, result)
    for path, is_real_dir in reversed(entries):
        if result.removed_files + result.removed_dirs >= MAX_REMOVALS_PER_RUN:
            result.capped = True
            break
        if is_real_dir:
            _remove_dir(path, result)
        else:
            _remove_file(path, cutoff_ts, result)
    return result


def held_paths(root: Path, proc_root: Path = _PROC_ROOT) -> frozenset[Path] | None:
    """Return every path under *root* a live process holds, plus its ancestors.

    Reads each numeric entry's ``cwd`` and ``fd/*`` symlinks. Returns None when
    *proc_root* is unreadable (macOS, or no ``/proc``), so the caller falls back
    to age alone. Vanished processes and permission errors are skipped.
    """
    try:
        pids = [entry.name for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    held: set[Path] = set()
    for pid in pids:
        for target in _process_targets(proc_root / pid):
            held.update(_protected_chain(target, root))
    return frozenset(held)


def _process_targets(pdir: Path) -> list[Path]:
    """Return the readable ``cwd`` and ``fd`` link targets of one process."""
    try:
        fds = list((pdir / "fd").iterdir())
    except OSError:
        fds = []
    targets: list[Path] = []
    for link in [pdir / "cwd", *fds]:
        try:
            targets.append(Path(link.readlink()))
        except OSError:
            continue
    return targets


def _protected_chain(target: Path, root: Path) -> list[Path]:
    """Return *target* and its ancestors strictly below *root*, if under *root*."""
    if target == root or root not in target.parents:
        return []
    chain: list[Path] = []
    for node in [target, *target.parents]:
        if node == root:
            break
        chain.append(node)
    return chain


def measure_fallback_tmp(root: Path, *, limit: int = MEASURE_LIMIT) -> TmpUsage:
    """Count files and bytes under *root*, stopping after *limit* entries."""
    if not os.path.lexists(root):
        return TmpUsage(files=0, bytes=0, truncated=False)
    files = 0
    size = 0
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*dirnames, *filenames]:
            if seen >= limit:
                return TmpUsage(files=files, bytes=size, truncated=True)
            seen += 1
            try:
                st = _lstat(Path(dirpath) / name)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                continue
            files += 1
            if not stat.S_ISLNK(st.st_mode):
                size += st.st_size
    return TmpUsage(files=files, bytes=size, truncated=False)


def _is_empty(root: Path) -> bool:
    """True when *root* is a real directory with no entries."""
    if root.is_symlink() or not root.is_dir():
        return False
    return next(root.iterdir(), None) is None


def _run_sweep(root: Path, cutoff: datetime) -> SweepResult:
    """Sweep *root*; a missing or empty root skips the ``/proc`` scan entirely."""
    if not os.path.lexists(root) or _is_empty(root):
        return SweepResult(proc_guard=False)
    return sweep_fallback_tmp(root, cutoff=cutoff, protected=held_paths(root))


def auto_sweep_fallback_tmp(
    config: OrchestratorConfig, *, now: datetime | None = None, force: bool = False
) -> SweepResult | None:
    """Run one retention sweep if enabled and due; return its result or None.

    None means disabled, not yet due, or the run failed (recorded in the
    sidecar, never raised). The caller must hold no guarded lock.
    """
    if not config.worker_tmp_fallback_retention_enabled:
        return None
    run_at = now or datetime.now(UTC)
    if not force and not _is_due(config, run_at):
        return None

    days = config.worker_tmp_fallback_retention_days
    root = resolve_fallback_worker_tmpdir()
    try:
        result = _run_sweep(root, run_at - timedelta(days=days))
    except (CwLockReentrancyError, CwLockOrderError):
        raise
    except (CwError, OSError, ValueError) as exc:
        _log.exception("fallback tmp retention failed; will retry next interval")
        _write_status(
            RetentionStatus(
                last_run_at=run_at,
                status="failed",
                retention_days=days,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        return None

    _write_status(
        RetentionStatus(
            last_run_at=run_at,
            status="ok",
            removed_files=result.removed_files,
            removed_dirs=result.removed_dirs,
            freed_bytes=result.freed_bytes,
            kept_in_use=result.kept_in_use,
            errors=result.errors,
            retention_days=days,
            capped=result.capped,
            proc_guard=result.proc_guard,
        )
    )
    if result.removed_files + result.removed_dirs > 0:
        _log.info(
            "fallback tmp retention removed %d files, %d dirs (%d bytes) from %s",
            result.removed_files,
            result.removed_dirs,
            result.freed_bytes,
            root,
        )
    return result
