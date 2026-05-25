"""Tests for cw.atomic — atomic file writing helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cw.atomic import atomic_write_text


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
