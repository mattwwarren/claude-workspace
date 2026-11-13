"""Doctor check for the shared ``~/.cw/tmp`` fallback worker TMPDIR (#2682).

A sibling of the per-client ``worker-tmp/<client>`` checks, not an extension of
them: those probe the worktree-base mount and say so in their messages, while
this one probes the mount that holds the shared fallback dir. It reports the
mount's free GB and free inodes against the same ``disk_pressure_*`` thresholds
the dispatch gate uses, a bounded tree measurement, and the last retention run
from its ``worker_tmp_retention.json`` sidecar. ``ok=False`` on a shortfall;
``warn=True`` only when the last retention run failed.

Leaf module apart from ``_shared.orchestrator_config_or_default``, the one
degrade-to-defaults config loader every doctor check shares.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw._util import format_compact_age
from cw.disk import assess_mount, check_disk_usage, check_inode_usage
from cw.doctor._shared import CheckResult, orchestrator_config_or_default
from cw.worker_tmp_retention import (
    MEASURE_LIMIT,
    RetentionStatus,
    measure_fallback_tmp,
    read_retention_status,
)
from cw.worktree import resolve_fallback_worker_tmpdir

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import OrchestratorConfig

_CHECK_NAME = "worker-tmp-fallback"


def _mount_findings(
    root: Path, config: OrchestratorConfig
) -> tuple[str, list[str], list[str]]:
    """Return ``(headroom, shortfalls, notes)`` for the mount backing *root*.

    *headroom* is always reported: free GB and free inodes with their floors, so
    a healthy mount still shows how much room is left. Raises ``OSError`` when
    the mount cannot be probed; the caller reports that. A missing *root* is not
    an error: the probe walks to its nearest ancestor.
    """
    shortfalls: list[str] = []
    notes: list[str] = []
    if not root.exists():
        notes.append("not created yet")
    disk = check_disk_usage(root)
    headroom = (
        f"mount free {disk.free_gb:.1f} GB"
        f" (floor {config.disk_pressure_min_free_gb:.1f} GB)"
    )
    inode = check_inode_usage(root)
    assessment = assess_mount(
        disk,
        inode,
        min_free_gb=config.disk_pressure_min_free_gb,
        min_free_inodes=config.disk_pressure_min_free_inodes,
        min_free_inode_fraction=config.disk_pressure_min_free_inode_fraction,
    )
    if assessment.space_short:
        shortfalls.append(
            f"mount free {disk.free_gb:.1f} GB below"
            f" {config.disk_pressure_min_free_gb:.1f} GB"
        )
    floor = assessment.inode_floor
    if floor is None:
        notes.append("mount reports no fixed inode budget")
        return headroom, shortfalls, notes
    headroom += f", {inode.free_inodes:,} free inodes (floor {floor:,})"
    if assessment.inode_short:
        shortfalls.append(
            f"mount free inodes {inode.free_inodes:,} below floor {floor:,}"
        )
    return headroom, shortfalls, notes


def _guard_note(status: RetentionStatus) -> str:
    """Describe the open-file guard of an ok run (informational, never a warn)."""
    if not status.proc_guard:
        return "age-only (no /proc guard)"
    if status.unreadable_procs:
        return f"/proc guard on, {status.unreadable_procs} processes unreadable"
    return "/proc guard on"


def _retention_detail(
    status: RetentionStatus | None, config: OrchestratorConfig, now: datetime
) -> str:
    if not config.worker_tmp_fallback_retention_enabled:
        return "retention disabled (worker_tmp_fallback_retention_enabled)"
    if status is None:
        return (
            "retention not run yet"
            f" (every {config.worker_tmp_fallback_retention_interval_minutes}m,"
            f" retain {config.worker_tmp_fallback_retention_days}d)"
        )
    age = format_compact_age(now - status.last_run_at)
    if status.status == "failed":
        return f"last retention run {age} ago FAILED: {status.error}"
    return (
        f"last retention run {age} ago: removed {status.removed_files} files,"
        f" {status.removed_dirs} dirs ({status.freed_bytes}B freed),"
        f" kept {status.kept_in_use} in use, {status.errors} errors;"
        f" {_guard_note(status)}"
    )


def _check_worker_tmp_fallback() -> CheckResult:
    """Report the ``~/.cw/tmp`` mount, its tree size and the last retention run."""
    config = orchestrator_config_or_default()
    root = resolve_fallback_worker_tmpdir()
    try:
        headroom, shortfalls, notes = _mount_findings(root, config)
    except OSError as exc:
        return CheckResult(_CHECK_NAME, ok=False, detail=f"cannot probe {root}: {exc}")

    usage = measure_fallback_tmp(root)
    tree = f"{usage.files} files, {usage.bytes}B"
    if usage.truncated:
        tree += f" (truncated at {MEASURE_LIMIT:,} entries)"
    status = read_retention_status()
    now = datetime.now(UTC)
    parts = [
        f"{root}: {tree}",
        headroom,
        *notes,
        _retention_detail(status, config, now),
    ]
    if shortfalls:
        parts.insert(1, "; ".join(shortfalls))
    return CheckResult(
        _CHECK_NAME,
        ok=not shortfalls,
        warn=status is not None and status.status == "failed",
        detail="; ".join(parts),
    )
