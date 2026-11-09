"""Tests for the shared ~/.cw/tmp fallback retention sweep (#2682).

Ages are modelled with ``cutoff`` in the future (ctime cannot be backdated, so a
freshly created file is "stale" against a future cutoff) and with ``os.utime``
pushed into the future to model a fresh entry.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw import worker_tmp_retention as wtr
from cw.exceptions import CwLockOrderError, CwLockReentrancyError
from cw.models import OrchestratorConfig
from cw.worker_tmp_retention import (
    RetentionStatus,
    auto_sweep_fallback_tmp,
    held_paths,
    measure_fallback_tmp,
    read_retention_status,
    sweep_fallback_tmp,
)
from cw.worktree import resolve_fallback_worker_tmpdir

if TYPE_CHECKING:
    from collections.abc import Iterator

_FUTURE_MTIME = 4_000_000_000  # year 2096: "fresh" against any near cutoff


def _future_cutoff() -> datetime:
    """A cutoff one day ahead: anything created now reads as stale."""
    return datetime.now(UTC) + timedelta(days=1)


def _make_file(path: Path, body: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _make_fresh(path: Path) -> None:
    os.utime(path, (_FUTURE_MTIME, _FUTURE_MTIME))


@pytest.fixture
def root() -> Path:
    """The real fallback root under the per-test redirected HOME."""
    tmp = resolve_fallback_worker_tmpdir()
    tmp.mkdir(parents=True)
    return tmp


class TestSweepRemovals:
    def test_stale_file_removed(self, root: Path) -> None:
        f = _make_file(root / "a.txt", "hello")
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert not f.exists()
        assert result.removed_files == 1
        assert result.freed_bytes == len("hello")

    def test_stale_symlink_unlinked_target_untouched(
        self, root: Path, tmp_path: Path
    ) -> None:
        target = _make_file(tmp_path / "outside.txt", "keep me")
        link = root / "link"
        link.symlink_to(target)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert not link.is_symlink()
        assert target.read_text() == "keep me"
        assert result.removed_files == 1

    def test_fresh_file_kept(self, root: Path) -> None:
        f = _make_file(root / "fresh.txt")
        _make_fresh(f)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert f.exists()
        assert result.removed_files == 0

    def test_stale_dir_emptied_is_removed(self, root: Path) -> None:
        _make_file(root / "d" / "child.txt")
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert not (root / "d").exists()
        assert result.removed_files == 1
        assert result.removed_dirs == 1

    def test_stale_dir_with_fresh_child_kept(self, root: Path) -> None:
        child = _make_file(root / "d" / "child.txt")
        _make_fresh(child)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert (root / "d").is_dir()
        assert child.exists()
        assert result.removed_dirs == 0
        assert result.errors == 0

    def test_fresh_empty_dir_kept(self, root: Path) -> None:
        d = root / "empty"
        d.mkdir()
        os.utime(d, (_FUTURE_MTIME, _FUTURE_MTIME))
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert d.is_dir()
        assert result.removed_dirs == 0

    def test_root_never_removed(self, root: Path) -> None:
        sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert root.is_dir()

    def test_missing_root_is_zero_result(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent"
        result = sweep_fallback_tmp(missing, cutoff=_future_cutoff(), protected=None)
        assert result.removed_files == 0
        assert result.errors == 0
        assert not missing.exists()

    def test_root_symlink_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="refused"):
            sweep_fallback_tmp(link, cutoff=_future_cutoff(), protected=None)

    def test_root_file_refused(self, tmp_path: Path) -> None:
        f = _make_file(tmp_path / "not-a-dir")
        with pytest.raises(ValueError, match="refused"):
            sweep_fallback_tmp(f, cutoff=_future_cutoff(), protected=None)

    def test_cross_device_entry_skipped(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = _make_file(root / "other-dev.txt")
        real_lstat = os.lstat

        def fake_lstat(path: str | os.PathLike[str]) -> os.stat_result:
            st = real_lstat(path)
            if str(path) == str(other):
                fields = list(st)
                fields[2] = st.st_dev + 1
                return os.stat_result(fields[:10])
            return st

        monkeypatch.setattr(wtr, "_lstat", fake_lstat)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert other.exists()
        assert result.removed_files == 0

    def test_entry_fresh_again_before_unlink_is_kept(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _make_file(root / "race.txt")
        real_lstat = os.lstat
        seen = {"n": 0}

        def fake_lstat(path: str | os.PathLike[str]) -> os.stat_result:
            if str(path) == str(target):
                seen["n"] += 1
                if seen["n"] == 2:
                    # A worker touched the file between scan and unlink.
                    _make_fresh(target)
            return real_lstat(path)

        monkeypatch.setattr(wtr, "_lstat", fake_lstat)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert target.exists()
        assert result.removed_files == 0

    def test_file_vanished_before_unlink_is_ignored(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _make_file(root / "gone.txt")
        real_lstat = os.lstat
        seen = {"n": 0}

        def fake_lstat(path: str | os.PathLike[str]) -> os.stat_result:
            if str(path) == str(target):
                seen["n"] += 1
                if seen["n"] == 2:
                    raise FileNotFoundError(str(path))
            return real_lstat(path)

        monkeypatch.setattr(wtr, "_lstat", fake_lstat)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert result.errors == 0
        assert result.removed_files == 0

    def test_unlink_oserror_counted_and_sweep_continues(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = _make_file(root / "bad.txt")
        good = _make_file(root / "good.txt")
        real_unlink = os.unlink

        def fake_unlink(path: object, *a: object, **k: object) -> None:
            if str(path) == str(bad):
                raise PermissionError(str(path))
            real_unlink(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "unlink", fake_unlink)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert bad.exists()
        assert not good.exists()
        assert result.errors == 1
        assert result.removed_files == 1

    def test_rmdir_other_oserror_counted(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_file(root / "d" / "c.txt")
        real_rmdir = os.rmdir

        def fake_rmdir(path: object, *a: object, **k: object) -> None:
            if str(path) == str(root / "d"):
                raise PermissionError(str(path))
            real_rmdir(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "rmdir", fake_rmdir)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert result.errors == 1
        assert (root / "d").is_dir()

    def test_rmdir_enotempty_is_silent(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_file(root / "d" / "c.txt")
        import errno

        real_rmdir = os.rmdir

        def fake_rmdir(path: object, *a: object, **k: object) -> None:
            if str(path) == str(root / "d"):
                raise OSError(errno.ENOTEMPTY, "not empty")
            real_rmdir(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "rmdir", fake_rmdir)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert result.errors == 0

    def test_removal_cap_sets_capped(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wtr, "MAX_REMOVALS_PER_RUN", 1)
        _make_file(root / "one.txt")
        _make_file(root / "two.txt")
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert result.removed_files == 1
        assert result.capped is True

    def test_scan_cap_sets_capped(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wtr, "MAX_SCAN_ENTRIES", 1)
        _make_file(root / "one.txt")
        _make_file(root / "two.txt")
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=None)
        assert result.capped is True

    def test_protected_stale_entry_kept_and_counted(self, root: Path) -> None:
        f = _make_file(root / "held.txt")
        result = sweep_fallback_tmp(
            root, cutoff=_future_cutoff(), protected=frozenset({f})
        )
        assert f.exists()
        assert result.kept_in_use == 1
        assert result.removed_files == 0
        assert result.proc_guard is True


def _proc_entry(proc: Path, pid: str, *, cwd: Path | None, fds: list[Path]) -> None:
    pdir = proc / pid
    (pdir / "fd").mkdir(parents=True)
    if cwd is not None:
        (pdir / "cwd").symlink_to(cwd)
    for i, target in enumerate(fds):
        (pdir / "fd" / str(i)).symlink_to(target)


class TestHeldPaths:
    @pytest.fixture
    def proc(self, tmp_path: Path) -> Iterator[Path]:
        p = tmp_path / "proc"
        p.mkdir()
        return p

    def test_open_fd_protects_file_and_ancestors(self, root: Path, proc: Path) -> None:
        f = _make_file(root / "a" / "b" / "file.txt")
        _proc_entry(proc, "123", cwd=None, fds=[f])
        held = held_paths(root, proc_root=proc)
        assert held is not None
        assert f in held
        assert root / "a" / "b" in held
        assert root / "a" in held
        assert root not in held

    def test_cwd_under_dir_protects_dir(self, root: Path, proc: Path) -> None:
        d = root / "work"
        d.mkdir()
        _proc_entry(proc, "456", cwd=d, fds=[])
        held = held_paths(root, proc_root=proc)
        assert held is not None
        assert d in held

    def test_entries_outside_root_not_protected(
        self, root: Path, proc: Path, tmp_path: Path
    ) -> None:
        outside = _make_file(tmp_path / "elsewhere.txt")
        _proc_entry(proc, "789", cwd=tmp_path, fds=[outside])
        held = held_paths(root, proc_root=proc)
        assert held == frozenset()

    def test_non_numeric_proc_entries_ignored(self, root: Path, proc: Path) -> None:
        f = _make_file(root / "x.txt")
        _proc_entry(proc, "self", cwd=None, fds=[f])
        held = held_paths(root, proc_root=proc)
        assert held == frozenset()

    def test_vanished_pid_ignored(self, root: Path, proc: Path) -> None:
        (proc / "999").mkdir()
        held = held_paths(root, proc_root=proc)
        assert held == frozenset()

    def test_missing_proc_returns_none(self, root: Path, tmp_path: Path) -> None:
        assert held_paths(root, proc_root=tmp_path / "no-proc") is None

    def test_held_file_survives_sweep(self, root: Path, proc: Path) -> None:
        f = _make_file(root / "live" / "scratch.txt")
        _proc_entry(proc, "42", cwd=None, fds=[f])
        held = held_paths(root, proc_root=proc)
        result = sweep_fallback_tmp(root, cutoff=_future_cutoff(), protected=held)
        assert f.exists()
        assert (root / "live").is_dir()
        assert result.kept_in_use >= 1


class TestMeasure:
    def test_counts_files_and_bytes(self, root: Path) -> None:
        _make_file(root / "a.txt", "12345")
        _make_file(root / "sub" / "b.txt", "123")
        usage = measure_fallback_tmp(root)
        assert usage.files == 2
        assert usage.bytes == 8
        assert usage.truncated is False

    def test_truncates_at_limit(self, root: Path) -> None:
        for i in range(5):
            _make_file(root / f"f{i}.txt")
        usage = measure_fallback_tmp(root, limit=3)
        assert usage.truncated is True
        assert usage.files <= 3

    def test_missing_root_zeros(self, tmp_path: Path) -> None:
        usage = measure_fallback_tmp(tmp_path / "absent")
        assert usage.files == 0
        assert usage.bytes == 0
        assert usage.truncated is False


class TestAutoSweep:
    def _config(self, **overrides: object) -> OrchestratorConfig:
        return OrchestratorConfig(**overrides)  # type: ignore[arg-type]

    def test_disabled_returns_none_and_writes_no_sidecar(self, root: Path) -> None:
        cfg = self._config(worker_tmp_fallback_retention_enabled=False)
        assert auto_sweep_fallback_tmp(cfg) is None
        assert read_retention_status() is None

    def test_missing_root_records_ok_zero_run(self) -> None:
        result = auto_sweep_fallback_tmp(self._config())
        assert result is not None
        assert result.removed_files == 0
        status = read_retention_status()
        assert status is not None
        assert status.status == "ok"

    def test_not_due_returns_none(self, root: Path) -> None:
        now = datetime.now(UTC)
        status = RetentionStatus(
            last_run_at=now - timedelta(minutes=5),
            status="ok",
            retention_days=7,
            capped=False,
            proc_guard=False,
        )
        wtr._write_status(status)
        cfg = self._config(worker_tmp_fallback_retention_interval_minutes=60)
        assert auto_sweep_fallback_tmp(cfg, now=now) is None

    def test_due_run_removes_and_writes_sidecar(self, root: Path) -> None:
        _make_file(root / "old.txt", "abc")
        now = datetime.now(UTC) + timedelta(days=30)
        result = auto_sweep_fallback_tmp(self._config(), now=now)
        assert result is not None
        assert result.removed_files == 1
        assert not (root / "old.txt").exists()
        status = read_retention_status()
        assert status is not None
        assert status.status == "ok"
        assert status.removed_files == 1
        assert status.freed_bytes == 3
        assert status.retention_days == 7
        assert status.proc_guard is True

    def test_future_last_run_counts_as_due(self, root: Path) -> None:
        now = datetime.now(UTC)
        wtr._write_status(
            RetentionStatus(
                last_run_at=now + timedelta(hours=2),
                status="ok",
                retention_days=7,
                capped=False,
                proc_guard=False,
            )
        )
        assert auto_sweep_fallback_tmp(self._config(), now=now) is not None

    def test_sweep_oserror_recorded_as_failed_not_raised(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_file(root / "x.txt")

        def boom(*_a: object, **_k: object) -> None:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr(wtr, "sweep_fallback_tmp", boom)
        now = datetime.now(UTC) + timedelta(days=30)
        assert auto_sweep_fallback_tmp(self._config(), now=now) is None
        status = read_retention_status()
        assert status is not None
        assert status.status == "failed"
        assert status.error is not None
        assert "boom" in status.error

    def test_symlinked_root_recorded_as_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tmp_path / "real-root"
        real.mkdir()
        _make_file(real / "x.txt")
        home_cw = Path.home() / ".cw"
        home_cw.mkdir(parents=True, exist_ok=True)
        (home_cw / "tmp").symlink_to(real)
        now = datetime.now(UTC) + timedelta(days=30)
        assert auto_sweep_fallback_tmp(self._config(), now=now) is None
        status = read_retention_status()
        assert status is not None
        assert status.status == "failed"
        assert "refused" in (status.error or "")

    def test_sidecar_write_failure_is_swallowed(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(_path: Path, _text: str) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(wtr, "atomic_write_text", fail)
        assert auto_sweep_fallback_tmp(self._config()) is not None

    def test_lock_reentrancy_reraised(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_file(root / "x.txt")

        def lock_bug(*_a: object, **_k: object) -> None:
            msg = "reentered"
            raise CwLockReentrancyError(
                msg, lock_name="sessions", path=Path("/x"), held=()
            )

        monkeypatch.setattr(wtr, "sweep_fallback_tmp", lock_bug)
        now = datetime.now(UTC) + timedelta(days=30)
        with pytest.raises(CwLockReentrancyError):
            auto_sweep_fallback_tmp(self._config(), now=now)

    def test_lock_order_reraised(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_file(root / "x.txt")

        def lock_bug(*_a: object, **_k: object) -> None:
            msg = "out of order"
            raise CwLockOrderError(
                msg,
                lock_name="sessions",
                path=Path("/x"),
                rank_name="STATE",
                held=(),
            )

        monkeypatch.setattr(wtr, "sweep_fallback_tmp", lock_bug)
        now = datetime.now(UTC) + timedelta(days=30)
        with pytest.raises(CwLockOrderError):
            auto_sweep_fallback_tmp(self._config(), now=now)

    def test_no_guarded_lock_held_during_sweep(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw._lock_guard import held_locks

        _make_file(root / "x.txt")
        seen: dict[str, object] = {}
        real = wtr.sweep_fallback_tmp

        def spy(*a: object, **k: object) -> object:
            seen["held"] = held_locks()
            return real(*a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(wtr, "sweep_fallback_tmp", spy)
        now = datetime.now(UTC) + timedelta(days=30)
        auto_sweep_fallback_tmp(self._config(), now=now)
        assert seen["held"] == ()

    def test_status_json_round_trip(self, root: Path) -> None:
        auto_sweep_fallback_tmp(self._config())
        raw = json.loads((wtr.state_dir() / "worker_tmp_retention.json").read_text())
        assert raw["status"] == "ok"


class TestReadStatus:
    def test_missing_returns_none(self) -> None:
        assert read_retention_status() is None

    def test_corrupt_returns_none(self) -> None:
        wtr.state_dir().mkdir(parents=True, exist_ok=True)
        (wtr.state_dir() / "worker_tmp_retention.json").write_text("{not json")
        assert read_retention_status() is None

    def test_unreadable_returns_none(self) -> None:
        # A directory where the sidecar should be raises IsADirectoryError (OSError).
        (wtr.state_dir() / "worker_tmp_retention.json").mkdir(parents=True)
        assert read_retention_status() is None
