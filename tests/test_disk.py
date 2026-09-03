"""Tests for cw.disk - the claim-time disk-pressure probe (#1887)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cw.disk import DiskUsage, _nearest_existing_ancestor, check_disk_usage


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
