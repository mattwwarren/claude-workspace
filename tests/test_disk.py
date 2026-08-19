"""Tests for cw.disk - the claim-time disk-pressure probe (#1887)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.disk import DiskUsage, check_disk_usage

if TYPE_CHECKING:
    from pathlib import Path


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
        self, tmp_path: Path
    ) -> None:
        """A not-yet-created worktree base still resolves via its ancestor."""
        missing = tmp_path / "does" / "not" / "exist"

        usage = check_disk_usage(missing)

        assert usage == check_disk_usage(tmp_path)

    def test_returns_plain_namedtuple_shape(self, tmp_path: Path) -> None:
        """Field names are part of the contract the gating call site reads."""
        usage = check_disk_usage(tmp_path)

        assert isinstance(usage, DiskUsage)
        assert usage._fields == ("total_gb", "free_gb")
