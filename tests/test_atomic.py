"""Tests for cw.atomic — atomic file writing helpers."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from cw.atomic import _BACKUP_SUFFIX, atomic_write_text, rotate_backup


def test_atomic_write_text_writes_payload(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_text(target, '{"k": 1}')
    assert target.read_text(encoding="utf-8") == '{"k": 1}'


def test_atomic_write_text_cleans_temp_on_rename_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``Path.replace`` raises, the temp file must be unlinked."""
    target = tmp_path / "state.json"

    leaked: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def capturing_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        leaked.append(name)
        return fd, name

    monkeypatch.setattr("cw.atomic.tempfile.mkstemp", capturing_mkstemp)

    def failing_replace(self: Path, target_path: Path) -> Path:
        msg = "simulated rename failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(Path, "replace", failing_replace)

    with pytest.raises(RuntimeError, match="simulated rename failure"):
        atomic_write_text(target, "payload")

    assert leaked, "mkstemp should have been called"
    for name in leaked:
        assert not Path(name).exists(), f"temp file {name} should have been removed"
    assert not target.exists()


def _backup_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}{_BACKUP_SUFFIX}*"))


def test_rotate_backup_noop_when_path_missing(tmp_path: Path) -> None:
    target = tmp_path / "dev_queue.json"
    rotate_backup(target)
    assert not target.exists()
    assert _backup_files(target) == []


def test_rotate_backup_creates_backup_before_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "dev_queue.json"
    target.write_text("A", encoding="utf-8")

    rotate_backup(target)

    backups = _backup_files(target)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "A"


def test_rotate_backup_prunes_to_keep_n(tmp_path: Path) -> None:
    target = tmp_path / "dev_queue.json"
    keep = 3
    for i in range(keep + 2):
        target.write_text(str(i), encoding="utf-8")
        rotate_backup(target, keep=keep)

    backups = _backup_files(target)
    assert len(backups) == keep
    contents = {b.read_text(encoding="utf-8") for b in backups}
    # Each iteration backs up the payload just written (write-then-rotate,
    # unlike production's rotate-then-write order), so after 5 iterations
    # (payloads "0".."4") the 3 most recent snapshots are "2", "3", "4".
    assert contents == {"2", "3", "4"}


def test_rotate_backup_default_keep_is_five(tmp_path: Path) -> None:
    target = tmp_path / "dev_queue.json"
    for i in range(7):
        target.write_text(str(i), encoding="utf-8")
        rotate_backup(target)

    backups = _backup_files(target)
    assert len(backups) == 5


def test_rotate_backup_swallows_oserror_on_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = tmp_path / "dev_queue.json"
    target.write_text("A", encoding="utf-8")

    def failing_copy2(*_args: object, **_kwargs: object) -> None:
        msg = "simulated copy failure"
        raise OSError(msg)

    monkeypatch.setattr("cw.atomic.shutil.copy2", failing_copy2)

    with caplog.at_level(logging.WARNING, logger="cw.atomic"):
        rotate_backup(target)

    assert _backup_files(target) == []
    assert any(record.levelno == logging.WARNING for record in caplog.records), (
        "expected a warning to be logged on copy failure"
    )


def test_rotate_backup_names_dont_collide_with_manual_snapshots(
    tmp_path: Path,
) -> None:
    target = tmp_path / "dev_queue.json"
    target.write_text("A", encoding="utf-8")
    manual_snapshot = tmp_path / "dev_queue.json.clobbered-testdata-20260705"
    manual_snapshot.write_text("unrelated", encoding="utf-8")

    rotate_backup(target)

    backups = _backup_files(target)
    assert manual_snapshot not in backups
    assert len(backups) == 1


def test_rotate_backup_swallows_oserror_on_prune_listing_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A backup file vanishing between glob() and .stat() (e.g. a concurrent
    unlocked writer, modeled here as a broken symlink) must not raise out of
    rotate_backup — the primary write it precedes must never be blocked by a
    prune-step failure."""
    target = tmp_path / "dev_queue.json"
    target.write_text("A", encoding="utf-8")
    rotate_backup(target)  # seed one real backup

    broken = tmp_path / f"dev_queue.json{_BACKUP_SUFFIX}broken"
    broken.symlink_to(tmp_path / "does-not-exist")

    with caplog.at_level(logging.WARNING, logger="cw.atomic"):
        rotate_backup(target)  # glob now includes the broken symlink

    assert any(record.levelno == logging.WARNING for record in caplog.records), (
        "expected a warning to be logged on prune-listing failure"
    )
