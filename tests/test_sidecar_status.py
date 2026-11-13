"""Tests for ``cw.sidecar_status``, the shared sidecar read/write/due helper (#3159)."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from cw.config import state_dir
from cw.sidecar_status import is_due, read_sidecar, sidecar_path, write_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_FILENAME = "probe_status.json"
_NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
_INTERVAL = timedelta(minutes=30)
_log = logging.getLogger("tests.sidecar_status")


class _Record(BaseModel):
    last_run_at: datetime
    note: str = ""


def _record(note: str = "hello") -> _Record:
    return _Record(last_run_at=_NOW, note=note)


class TestSidecarPath:
    def test_path_is_under_state_dir(self) -> None:
        assert sidecar_path(_FILENAME) == state_dir() / _FILENAME


class TestReadSidecar:
    def test_missing_file_returns_none(self) -> None:
        assert read_sidecar(_FILENAME, _Record) is None

    def test_corrupt_json_returns_none(self) -> None:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / _FILENAME).write_text("{not json")
        assert read_sidecar(_FILENAME, _Record) is None

    def test_schema_mismatch_returns_none(self) -> None:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / _FILENAME).write_text('{"note": "no timestamp"}')
        assert read_sidecar(_FILENAME, _Record) is None

    def test_directory_in_place_of_file_returns_none(self) -> None:
        # A directory where the sidecar should be raises IsADirectoryError (OSError).
        (state_dir() / _FILENAME).mkdir(parents=True)
        assert read_sidecar(_FILENAME, _Record) is None

    def test_valid_file_returns_model(self) -> None:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / _FILENAME).write_text(_record("seen").model_dump_json())
        got = read_sidecar(_FILENAME, _Record)
        assert got == _record("seen")


class TestWriteSidecar:
    def test_round_trips_and_creates_state_dir(self) -> None:
        shutil.rmtree(state_dir(), ignore_errors=True)
        assert not state_dir().exists()
        write_sidecar(_FILENAME, _record("round"), _log)
        assert read_sidecar(_FILENAME, _Record) == _record("round")

    def test_write_failure_is_swallowed_and_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def fail(_path: Path, _text: str) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.sidecar_status.atomic_write_text", fail)
        with caplog.at_level(logging.WARNING, logger=_log.name):
            write_sidecar(_FILENAME, _record(), _log)
        assert any(
            _FILENAME in r.getMessage() and r.name == _log.name for r in caplog.records
        )
        assert read_sidecar(_FILENAME, _Record) is None


class TestIsDue:
    def test_no_record_is_due(self) -> None:
        assert is_due(None, _INTERVAL, _NOW) is True

    def test_within_interval_is_not_due(self) -> None:
        last = _NOW - timedelta(minutes=5)
        assert is_due(last, _INTERVAL, _NOW) is False

    def test_exactly_the_interval_is_due(self) -> None:
        assert is_due(_NOW - _INTERVAL, _INTERVAL, _NOW) is True

    def test_future_last_run_is_due(self) -> None:
        assert is_due(_NOW + timedelta(hours=2), _INTERVAL, _NOW) is True

    def test_same_instant_is_not_due(self) -> None:
        assert is_due(_NOW, _INTERVAL, _NOW) is False
