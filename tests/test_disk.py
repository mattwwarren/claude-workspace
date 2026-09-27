"""Tests for cw.disk - the claim-time disk-pressure probe (#1887)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from cw.disk import (
    DiskUsage,
    InodeUsage,
    _nearest_existing_ancestor,
    check_disk_usage,
    check_inode_usage,
    effective_min_free_inodes,
    inodes_exhausted,
)


class TestCheckDiskUsage:
    """``check_disk_usage`` reports free/total space for a worktree base.

    The probe backs the dispatch preflight disk-pressure gate (#1887): it must
    answer for a path that does not exist yet (a client's first-ever claim
    creates the worktree base) and must report in GB so the gate can compare
    against ``OrchestratorConfig.disk_pressure_min_free_gb`` directly.
    """

    def test_returns_free_and_total_gb_for_existing_path(self, tmp_path: Path) -> None:
        """A real, existing directory yields positive, self-consistent numbers."""
        usage = check_disk_usage(tmp_path)

        assert usage.free_gb > 0
        assert usage.total_gb >= usage.free_gb

    def test_walks_up_to_nearest_existing_ancestor_for_missing_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A not-yet-created worktree base still resolves via its ancestor.

        Asserts the *resolution* (which path the probe was asked about), not
        the measurement: comparing two live ``shutil.disk_usage`` readings for
        exact equality flaked whenever anything wrote to the filesystem
        between the two calls (#2091).
        """
        missing = tmp_path / "does" / "not" / "exist"
        probed: list[Path] = []
        real_disk_usage = shutil.disk_usage

        def _spy(path: Path) -> object:
            probed.append(path)
            return real_disk_usage(path)

        monkeypatch.setattr("cw.disk.shutil.disk_usage", _spy)

        usage = check_disk_usage(missing)

        assert probed == [tmp_path]
        assert usage.free_gb > 0
        assert usage.total_gb >= usage.free_gb

    def test_returns_plain_namedtuple_shape(self, tmp_path: Path) -> None:
        """Field names are part of the contract the gating call site reads."""
        usage = check_disk_usage(tmp_path)

        assert isinstance(usage, DiskUsage)
        assert usage._fields == ("total_gb", "free_gb")

    def test_ancestor_walk_terminates_at_filesystem_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With nothing on disk existing, the walk stops at root, not forever."""
        monkeypatch.setattr(Path, "exists", lambda _self: False)

        assert _nearest_existing_ancestor(tmp_path) == Path(tmp_path.anchor)


class TestCheckInodeUsage:
    """``check_inode_usage`` reports free/total inodes for a worktree base (#2470).

    A tmpfs can run out of inodes long before bytes -- the 2026-09-27 ENOSPC
    incident -- so the disk-pressure gate needs this second dimension.
    """

    def test_returns_free_and_total_inodes_for_existing_path(
        self, tmp_path: Path
    ) -> None:
        usage = check_inode_usage(tmp_path)

        assert usage.total_inodes >= usage.free_inodes >= 0

    def test_walks_up_to_nearest_existing_ancestor_for_missing_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserts the resolution, not the live measurement (see #2091)."""
        missing = tmp_path / "does" / "not" / "exist"
        probed: list[Path] = []
        real_statvfs = os.statvfs

        def _spy(path: Path) -> os.statvfs_result:
            probed.append(path)
            return real_statvfs(path)

        monkeypatch.setattr("cw.disk.os.statvfs", _spy)

        usage = check_inode_usage(missing)

        assert probed == [tmp_path]
        assert usage.total_inodes >= usage.free_inodes >= 0

    def test_returns_plain_namedtuple_shape(self, tmp_path: Path) -> None:
        usage = check_inode_usage(tmp_path)

        assert isinstance(usage, InodeUsage)
        assert usage._fields == ("total_inodes", "free_inodes")


class TestInodeFloor:
    """``max(min_free_inodes, fraction x total)`` -- R1's worked examples (#2470)."""

    @pytest.mark.parametrize(
        ("total", "expected_floor"),
        [
            # Small mount: the absolute floor dominates (5% would be 10K).
            (200_000, 50_000),
            # Preserve a non-integral fractional floor by rounding upward.
            (3, 2),
            # R1 example 1: 1M-inode tmpfs refuses below 50K free.
            (1_000_000, 50_000),
            # R1 example 2: 50M-inode ext4 mount refuses below 2.5M free.
            (50_000_000, 2_500_000),
        ],
    )
    def test_effective_min_free_inodes(self, total: int, expected_floor: int) -> None:
        assert (
            effective_min_free_inodes(
                total,
                min_free_inodes=0 if total == 3 else 50_000,
                min_free_inode_fraction=0.5 if total == 3 else 0.05,
            )
            == expected_floor
        )

    @pytest.mark.parametrize(
        ("usage", "exhausted"),
        [
            (InodeUsage(total_inodes=200_000, free_inodes=40_000), True),
            (InodeUsage(total_inodes=200_000, free_inodes=50_000), False),
            (InodeUsage(total_inodes=50_000_000, free_inodes=2_000_000), True),
            (InodeUsage(total_inodes=50_000_000, free_inodes=2_500_000), False),
            # btrfs (and other dynamic-inode filesystems) report 0/0: the
            # inode dimension does not apply, so it must never gate.
            (InodeUsage(total_inodes=0, free_inodes=0), False),
        ],
    )
    def test_inodes_exhausted(self, usage: InodeUsage, exhausted: bool) -> None:
        assert (
            inodes_exhausted(
                usage, min_free_inodes=50_000, min_free_inode_fraction=0.05
            )
            is exhausted
        )
