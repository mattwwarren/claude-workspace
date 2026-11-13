"""Tests for the ``worker-tmp-fallback`` doctor check (#2682)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from cw.config import state_dir
from cw.disk import DiskUsage, InodeUsage
from cw.doctor.worker_tmp_fallback import _check_worker_tmp_fallback
from cw.models import OrchestratorConfig
from cw.worker_tmp_retention import RetentionStatus, TmpUsage
from cw.worktree import resolve_fallback_worker_tmpdir

if TYPE_CHECKING:
    from pathlib import Path

_ROOMY_DISK = DiskUsage(total_gb=500.0, free_gb=250.0)
_ROOMY_INODES = InodeUsage(total_inodes=1_000_000, free_inodes=900_000)
_SIDECAR = "worker_tmp_retention.json"


@pytest.fixture
def root() -> Path:
    """The fallback dir under the per-test redirected HOME (not created)."""
    return resolve_fallback_worker_tmpdir()


def _patch_mount(
    monkeypatch: pytest.MonkeyPatch,
    *,
    disk: DiskUsage | None = None,
    inodes: InodeUsage | None = None,
    disk_error: OSError | None = None,
) -> None:
    def fake_disk(_path: Path) -> DiskUsage:
        if disk_error is not None:
            raise disk_error
        return disk or _ROOMY_DISK

    def fake_inodes(_path: Path) -> InodeUsage:
        return inodes or _ROOMY_INODES

    monkeypatch.setattr("cw.doctor.worker_tmp_fallback.check_disk_usage", fake_disk)
    monkeypatch.setattr("cw.doctor.worker_tmp_fallback.check_inode_usage", fake_inodes)


def _patch_config(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    config = OrchestratorConfig.model_validate(overrides)
    monkeypatch.setattr(
        "cw.doctor.worker_tmp_fallback.orchestrator_config_or_default", lambda: config
    )


def _status(**overrides: object) -> RetentionStatus:
    fields: dict[str, object] = {
        "last_run_at": datetime.now(UTC) - timedelta(minutes=5),
        "status": "ok",
        "retention_days": 7,
    }
    fields.update(overrides)
    return RetentionStatus.model_validate(fields)


def _write_sidecar(status: RetentionStatus) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / _SIDECAR).write_text(status.model_dump_json())


class TestWorkerTmpFallbackCheck:
    def test_missing_dir_is_ok_and_noted(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        result = _check_worker_tmp_fallback()
        assert result.name == "worker-tmp-fallback"
        assert result.ok is True
        assert "not created yet" in result.detail
        assert "retention not run yet" in result.detail

    def test_healthy_reports_tree_and_headroom(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        root.mkdir(parents=True)
        (root / "a.txt").write_text("12345")
        result = _check_worker_tmp_fallback()
        assert result.ok is True
        assert result.warn is False
        assert "1 files, 5B" in result.detail
        assert "mount free 250.0 GB (floor 5.0 GB)" in result.detail
        assert "900,000 free inodes (floor 50,000)" in result.detail
        assert "below" not in result.detail

    def test_low_gb_is_not_ok(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch, disk=DiskUsage(total_gb=500.0, free_gb=1.0))
        result = _check_worker_tmp_fallback()
        assert result.ok is False
        assert "mount free 1.0 GB below" in result.detail
        assert "mount free 1.0 GB (floor 5.0 GB)" in result.detail

    def test_low_inodes_is_not_ok_and_names_floor(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(
            monkeypatch, inodes=InodeUsage(total_inodes=1_000_000, free_inodes=10_000)
        )
        result = _check_worker_tmp_fallback()
        assert result.ok is False
        assert "mount free inodes 10,000 below floor 50,000" in result.detail

    def test_zero_total_inodes_noted_not_gated(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch, inodes=InodeUsage(total_inodes=0, free_inodes=0))
        result = _check_worker_tmp_fallback()
        assert result.ok is True
        assert "no fixed inode budget" in result.detail
        assert "free inodes" not in result.detail

    def test_probe_oserror_is_not_ok(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch, disk_error=OSError("statvfs failed"))
        result = _check_worker_tmp_fallback()
        assert result.ok is False
        assert "cannot probe" in result.detail
        assert "statvfs failed" in result.detail

    def test_truncated_measurement_is_noted(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        monkeypatch.setattr(
            "cw.doctor.worker_tmp_fallback.measure_fallback_tmp",
            lambda _root: TmpUsage(files=50_000, bytes=10, truncated=True),
        )
        result = _check_worker_tmp_fallback()
        assert "truncated at 50,000 entries" in result.detail

    def test_failed_last_run_warns(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _write_sidecar(
            RetentionStatus(
                last_run_at=datetime.now(UTC) - timedelta(minutes=5),
                status="failed",
                retention_days=7,
                error="OSError: boom",
            )
        )
        result = _check_worker_tmp_fallback()
        assert result.warn is True
        assert "FAILED: OSError: boom" in result.detail

    def test_ok_last_run_summarised(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _write_sidecar(
            RetentionStatus(
                last_run_at=datetime.now(UTC) - timedelta(minutes=5),
                status="ok",
                removed_files=3,
                removed_dirs=1,
                freed_bytes=99,
                kept_in_use=2,
                errors=0,
                retention_days=7,
            )
        )
        result = _check_worker_tmp_fallback()
        assert result.warn is False
        assert "removed 3 files, 1 dirs (99B freed), kept 2 in use" in result.detail

    def test_retention_disabled_is_noted(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _patch_config(monkeypatch, worker_tmp_fallback_retention_enabled=False)
        result = _check_worker_tmp_fallback()
        assert "retention disabled" in result.detail

    def test_disabled_retention_with_stale_failed_sidecar_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _patch_config(monkeypatch, worker_tmp_fallback_retention_enabled=False)
        _write_sidecar(_status(status="failed", error="OSError: boom"))
        result = _check_worker_tmp_fallback()
        assert result.warn is False
        assert "retention disabled" in result.detail

    def test_enabled_retention_with_failed_sidecar_still_warns(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _patch_config(monkeypatch, worker_tmp_fallback_retention_enabled=True)
        _write_sidecar(_status(status="failed", error="OSError: boom"))
        assert _check_worker_tmp_fallback().warn is True

    def test_gb_and_inodes_both_short_reports_both(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(
            monkeypatch,
            disk=DiskUsage(total_gb=500.0, free_gb=1.0),
            inodes=InodeUsage(total_inodes=1_000_000, free_inodes=10_000),
        )
        result = _check_worker_tmp_fallback()
        assert result.ok is False
        assert "mount free 1.0 GB below 5.0 GB" in result.detail
        assert "mount free inodes 10,000 below floor 50,000" in result.detail
        assert "10,000 free inodes (floor 50,000)" in result.detail


class TestProcGuardDetail:
    """The ok-run detail reports the ``/proc`` guard state (#3163), never warns."""

    def test_guard_on(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        _patch_mount(monkeypatch)
        _write_sidecar(_status(proc_guard=True))
        result = _check_worker_tmp_fallback()
        assert "/proc guard on" in result.detail
        assert "unreadable" not in result.detail
        assert "age-only" not in result.detail
        assert result.warn is False

    def test_guard_off_is_age_only(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _write_sidecar(_status(proc_guard=False))
        result = _check_worker_tmp_fallback()
        assert "age-only" in result.detail
        assert "no /proc guard" in result.detail
        assert result.ok is True
        assert result.warn is False

    def test_unreadable_processes_are_counted_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _write_sidecar(_status(proc_guard=True, unreadable_procs=12))
        result = _check_worker_tmp_fallback()
        assert "12 processes unreadable" in result.detail
        assert result.ok is True
        assert result.warn is False

    def test_failed_run_does_not_mention_the_guard(
        self, monkeypatch: pytest.MonkeyPatch, root: Path
    ) -> None:
        _patch_mount(monkeypatch)
        _write_sidecar(_status(status="failed", error="OSError: boom"))
        result = _check_worker_tmp_fallback()
        assert "/proc" not in result.detail
        assert "age-only" not in result.detail
