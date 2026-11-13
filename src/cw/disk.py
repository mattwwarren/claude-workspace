"""Disk-space and inode probing for the claim-time disk-pressure gate.

Bytes since #1887 (split from #1858); inodes since #2470.

Standalone probe with no dispatch-specific knowledge, mirroring
:func:`cw.ssh.check_ssh_key_available` and :func:`cw.gh.check_gh_availability`:
the two existing preflight probes are plain functions imported into
``cw.dispatch.gating.disk_pressure``, not inlined there.
"""

from __future__ import annotations

import os
import shutil
from math import ceil
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


class InodeUsage(NamedTuple):
    """Free/total inodes on the filesystem backing a probed path (#2470).

    Separate from :class:`DiskUsage` because a tmpfs can exhaust inodes while
    bytes remain plentiful -- the 2026-09-27 ENOSPC incident. Named fields for
    the same transposition-safety reason as :class:`DiskUsage`.
    """

    total_inodes: int
    free_inodes: int


def check_inode_usage(path: Path) -> InodeUsage:
    """Return the total/free inode counts of the filesystem backing *path*.

    Same nearest-existing-ancestor walk and same raise-on-``OSError``
    contract as :func:`check_disk_usage`. ``f_ffree`` (not ``f_favail``) is
    the count reported, matching ``df -i``'s ``IFree`` column.
    """
    stat = os.statvfs(_nearest_existing_ancestor(path))
    return InodeUsage(total_inodes=stat.f_files, free_inodes=stat.f_ffree)


def effective_min_free_inodes(
    total_inodes: int, *, min_free_inodes: float, min_free_inode_fraction: float
) -> int:
    """Return ``max(min_free_inodes, min_free_inode_fraction * total_inodes)``.

    The absolute floor protects a small mount (a 1M-inode tmpfs refuses below
    50K free at the defaults); the fraction scales to a large one (a
    50M-inode mount refuses below 2.5M free). Shared by the dispatch-time
    gate and both ``cw doctor`` worker-tmp checks (through :func:`assess_mount`)
    so they never disagree.
    """
    return ceil(max(min_free_inodes, min_free_inode_fraction * total_inodes))


def inodes_exhausted(
    usage: InodeUsage, *, min_free_inodes: float, min_free_inode_fraction: float
) -> bool:
    """True when *usage* has fewer free inodes than the effective floor.

    A filesystem with dynamic inode allocation (btrfs) reports
    ``f_files == f_ffree == 0``; there is no fixed inode budget to exhaust,
    so a zero total never counts as exhausted -- otherwise every client on a
    btrfs mount would be held PENDING forever.
    """
    if usage.total_inodes == 0:
        return False
    floor = effective_min_free_inodes(
        usage.total_inodes,
        min_free_inodes=min_free_inodes,
        min_free_inode_fraction=min_free_inode_fraction,
    )
    return usage.free_inodes < floor


def space_exhausted(usage: DiskUsage, *, min_free_gb: float) -> bool:
    """True when *usage* has fewer free GB than *min_free_gb* (#3179).

    The byte-dimension sibling of :func:`inodes_exhausted`, so the dispatch gate
    and the doctor checks compare against the floor one way.
    """
    return usage.free_gb < min_free_gb


class MountAssessment(NamedTuple):
    """Whether a mount is below the disk-pressure floors (#3179).

    ``inode_floor`` is None when the mount reports no fixed inode budget
    (``total_inodes == 0``), in which case ``inode_short`` is always False.
    """

    space_short: bool
    inode_short: bool
    inode_floor: int | None


def assess_mount(
    disk: DiskUsage,
    inodes: InodeUsage,
    *,
    min_free_gb: float,
    min_free_inodes: float,
    min_free_inode_fraction: float,
) -> MountAssessment:
    """Assess one mount against the disk-pressure floors, both dimensions at once.

    The single place the ``cw doctor`` worker-tmp checks apply the thresholds the
    dispatch gate uses. Wording stays in each check.
    """
    inode_floor = (
        None
        if inodes.total_inodes == 0
        else effective_min_free_inodes(
            inodes.total_inodes,
            min_free_inodes=min_free_inodes,
            min_free_inode_fraction=min_free_inode_fraction,
        )
    )
    return MountAssessment(
        space_short=space_exhausted(disk, min_free_gb=min_free_gb),
        inode_short=inodes_exhausted(
            inodes,
            min_free_inodes=min_free_inodes,
            min_free_inode_fraction=min_free_inode_fraction,
        ),
        inode_floor=inode_floor,
    )


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


def same_filesystem(a: Path, b: Path) -> bool:
    """True when *a* and *b* live on the same device (#2682).

    Compares ``st_dev`` of each path's nearest existing ancestor, so a
    not-yet-created path resolves to the mount it will land on. ``OSError``
    propagates: the caller decides the posture (the disk-pressure gate probes
    the second mount anyway).
    """
    return (
        _nearest_existing_ancestor(a).stat().st_dev
        == _nearest_existing_ancestor(b).stat().st_dev
    )
