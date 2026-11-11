"""Unit tests for cw._state_backup: save-time legacy sessions.json backup (#2951)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import cw._state_backup
from cw._state_backup import backup_legacy_state, on_disk_schema_version
from cw.models import CW_STATE_SCHEMA_VERSION, CwState
from tests._shared.session_seeding import write_legacy_sessions_json

_FULL_PARSE_MSG = "full json.loads must not run on this path"
_DISK_FULL_MSG = "disk full"


@pytest.fixture
def state_dir(tmp_config_dir: Path) -> Path:
    path = tmp_config_dir / ".local" / "share" / "cw"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup(state_dir: Path) -> Path:
    return state_dir / ".sessions.json.0.x-backup"


def _no_full_parse(*_args: object, **_kwargs: object) -> None:
    raise AssertionError(_FULL_PARSE_MSG)


def test_legacy_file_backed_up_with_exact_bytes(state_dir: Path) -> None:
    sf = write_legacy_sessions_json(state_dir)
    backup_legacy_state(sf)
    assert _backup(state_dir).read_bytes() == sf.read_bytes()


def test_backup_is_idempotent_and_never_overwritten(state_dir: Path) -> None:
    sf = write_legacy_sessions_json(state_dir)
    backup_legacy_state(sf)
    first = _backup(state_dir)
    first_bytes = first.read_bytes()
    os.utime(first, ns=(1_000_000_000, 1_000_000_000))
    first_mtime = first.stat().st_mtime_ns

    sf.write_text(
        json.dumps({"schema_version": CW_STATE_SCHEMA_VERSION, "sessions": []})
    )
    backup_legacy_state(sf)

    assert first.read_bytes() == first_bytes
    assert first.stat().st_mtime_ns == first_mtime


def test_current_schema_never_backed_up(state_dir: Path) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text(CwState().model_dump_json(indent=2))
    backup_legacy_state(sf)
    assert not _backup(state_dir).exists()


def test_newer_schema_never_backed_up(state_dir: Path) -> None:
    sf = write_legacy_sessions_json(
        state_dir, schema_version=CW_STATE_SCHEMA_VERSION + 1
    )
    backup_legacy_state(sf)
    assert not _backup(state_dir).exists()


def test_missing_schema_version_key_is_backed_up(state_dir: Path) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text(json.dumps({"sessions": []}))
    backup_legacy_state(sf)
    assert _backup(state_dir).exists()


def test_schema_version_not_first_key_is_backed_up(state_dir: Path) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text(json.dumps({"sessions": [], "schema_version": 4}))
    backup_legacy_state(sf)
    assert _backup(state_dir).exists()


def test_null_schema_version_treated_as_zero(state_dir: Path) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text(json.dumps({"schema_version": None, "sessions": []}))
    backup_legacy_state(sf)
    assert _backup(state_dir).exists()


def test_peek_avoids_full_parse_for_cw_written_file(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text(CwState().model_dump_json(indent=2))
    monkeypatch.setattr(cw._state_backup.json, "loads", _no_full_parse)
    backup_legacy_state(sf)
    assert not _backup(state_dir).exists()


def test_peek_decides_legacy_without_full_parse(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sf = write_legacy_sessions_json(state_dir)
    monkeypatch.setattr(cw._state_backup.json, "loads", _no_full_parse)
    backup_legacy_state(sf)
    assert _backup(state_dir).exists()


def test_float_schema_version_falls_back_to_parse(state_dir: Path) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text('{\n  "schema_version": 19.5,\n  "sessions": []\n}')
    assert on_disk_schema_version(sf) == 19
    backup_legacy_state(sf)
    assert not _backup(state_dir).exists()


@pytest.mark.parametrize("content", ["{not json", ""])
def test_unparsable_state_file_skipped(state_dir: Path, content: str) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text(content)
    backup_legacy_state(sf)
    assert not _backup(state_dir).exists()


def test_non_dict_json_skipped(state_dir: Path) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text("[1, 2]")
    backup_legacy_state(sf)
    assert not _backup(state_dir).exists()


def test_non_numeric_schema_version_skipped(state_dir: Path) -> None:
    sf = state_dir / "sessions.json"
    sf.write_text(json.dumps({"schema_version": "abc"}))
    backup_legacy_state(sf)
    assert not _backup(state_dir).exists()


def test_missing_state_file_noop(state_dir: Path) -> None:
    backup_legacy_state(state_dir / "sessions.json")
    assert not _backup(state_dir).exists()


def test_copy_failure_propagates(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sf = write_legacy_sessions_json(state_dir)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(_DISK_FULL_MSG)

    monkeypatch.setattr(cw._state_backup.shutil, "copy2", _boom)
    with pytest.raises(OSError, match="disk full"):
        backup_legacy_state(sf)
