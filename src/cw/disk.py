"""Disk-space probing for the claim-time disk-pressure gate (#1887, split from #1858).

Standalone probe with no dispatch-specific knowledge, mirroring
:func:`cw.ssh.check_ssh_key_available` and :func:`cw.gh.check_gh_availability`:
the two existing preflight probes are plain functions imported into
``cw.dispatch.gating``, not inlined there.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path

# Bytes per gibibyte. shutil.disk_usage reports bytes; the gate compares
# against OrchestratorConfig.disk_pressure_min_free_gb, so the conversion
# lives here rather than at the call site.
_BYTES_PER_GB = 1024**3


class DiskUsage(NamedTuple):
    """Free/total space on the filesystem backing a probed path, in GB.

    Named fields (vs. a bare positional tuple) prevent a transposition mypy
    cannot catch -- ``total_gb`` and ``free_gb`` are both plain floats -- same
    rationale as ``cw.dispatch.tick._PreflightGateResult``.
    """

    total_gb: float
    free_gb: float


def _nearest_existing_ancestor(path: Path) -> Path:
    """Return *path* if it exists, else its nearest existing ancestor.

    A client's worktree base does not exist before its first-ever claim, and
    ``shutil.disk_usage`` raises ``FileNotFoundError`` on a missing path. The
    ancestor sits on the same filesystem the base will be created on (the
    only exception being a mount created between this call and the checkout,
    which no probe can anticipate), so its free space is the right answer.
    Terminates at the filesystem root, which always exists.
    """
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


def check_disk_usage(path: Path) -> DiskUsage:
    """Return the total/free space of the filesystem backing *path*, in GB.

    Walks up to the nearest existing ancestor first (see
    :func:`_nearest_existing_ancestor`) so a not-yet-created worktree base
    still probes the mount it will land on. Raises ``OSError`` on an
    unprobeable path -- the caller decides the fail-open/fail-closed posture,
    not this function.
    """
    usage = shutil.disk_usage(_nearest_existing_ancestor(path))
    return DiskUsage(
        total_gb=usage.total / _BYTES_PER_GB,
        free_gb=usage.free / _BYTES_PER_GB,
    )
