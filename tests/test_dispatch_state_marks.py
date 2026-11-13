"""Tests for cw.dispatch_state_marks: clear-mark loader and clear primitive (#3547)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cw.dispatch_state_marks import (
    clear_usage_limited_until,
    load_usage_limit_clear_marks,
)
from cw.dispatch_state_marks_parse import CLEAR_MARK_RETENTION, CLEAR_MARKS_KEY

import cw.dispatch_state
from cw.dispatch_state import (
    AvailabilityProbeCache,
    load_usage_limit_armed_at,
    load_usage_limited_until,
    save_availability_probe_cache,
    save_usage_limit_armed_at,
)
from tests._shared.dispatch_fixtures import seed_usage_limit_window
from tests._shared.lock_helpers import DISPATCH_STATE_LOCK_NAME, record_state_locks

_NOW = datetime.now(UTC)
_UNTIL = _NOW + timedelta(hours=2)


def _raw() -> dict[str, object]:
    data: dict[str, object] = json.loads(
        cw.dispatch_state.DISPATCH_STATE_FILE.read_text()
    )
    return data


class TestLoadMarks:
    def test_reads_marks_through_the_sidecar(self, tmp_config_dir: Path) -> None:
        clear_usage_limited_until("acme", cleared_at=_NOW)

        assert load_usage_limit_clear_marks() == {"acme": _NOW}

    def test_absent_key_or_corrupt_file_gives_empty(self, tmp_config_dir: Path) -> None:
        assert load_usage_limit_clear_marks() == {}
        seed_usage_limit_window("acme", _UNTIL)
        assert load_usage_limit_clear_marks() == {}
        cw.dispatch_state.DISPATCH_STATE_FILE.write_text("{not json")
        assert load_usage_limit_clear_marks() == {}

    def test_takes_no_lock(self, tmp_config_dir: Path) -> None:
        clear_usage_limited_until("acme", cleared_at=_NOW)

        with record_state_locks() as recorder:
            load_usage_limit_clear_marks()

        assert recorder.acquires(DISPATCH_STATE_LOCK_NAME) == 0


class TestClearUsageLimitedUntil:
    def test_pops_only_that_client_and_keeps_other_keys(
        self, tmp_config_dir: Path
    ) -> None:
        seed_usage_limit_window("acme", _UNTIL)
        seed_usage_limit_window("beta", _UNTIL)
        save_usage_limit_armed_at(_NOW)
        save_availability_probe_cache(
            AvailabilityProbeCache(probed_at=_NOW, available=True, latched=False)
        )

        old = clear_usage_limited_until("acme", cleared_at=_NOW)

        assert old == _UNTIL
        assert load_usage_limited_until() == {"beta": _UNTIL}
        assert load_usage_limit_clear_marks() == {"acme": _NOW}
        assert load_usage_limit_armed_at() == _NOW
        assert "availability_probe" in _raw()

    def test_no_window_returns_none_and_still_writes_the_mark(
        self, tmp_config_dir: Path
    ) -> None:
        assert clear_usage_limited_until("acme", cleared_at=_NOW) is None
        assert load_usage_limit_clear_marks() == {"acme": _NOW}

    def test_prunes_marks_older_than_the_retention(self, tmp_config_dir: Path) -> None:
        stale = _NOW - CLEAR_MARK_RETENTION - timedelta(minutes=1)
        recent = _NOW - CLEAR_MARK_RETENTION + timedelta(minutes=1)
        clear_usage_limited_until("old", cleared_at=stale)
        clear_usage_limited_until("young", cleared_at=recent)

        clear_usage_limited_until("acme", cleared_at=_NOW)

        assert load_usage_limit_clear_marks() == {"young": recent, "acme": _NOW}

    def test_pop_and_mark_share_one_lock_acquisition(
        self, tmp_config_dir: Path
    ) -> None:
        seed_usage_limit_window("acme", _UNTIL)

        with record_state_locks() as recorder:
            clear_usage_limited_until("acme", cleared_at=_NOW)

        assert recorder.acquires(DISPATCH_STATE_LOCK_NAME) == 1

    def test_os_error_propagates(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_usage_limit_window("acme", _UNTIL)

        def _boom(_path: Path, _text: str) -> None:
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr("cw.dispatch_state_marks.atomic_write_text", _boom)

        with pytest.raises(OSError, match="disk full"):
            clear_usage_limited_until("acme", cleared_at=_NOW)
        assert load_usage_limited_until() == {"acme": _UNTIL}

    def test_writes_the_isolated_path(self, tmp_config_dir: Path) -> None:
        """The path is read at call time, so the autouse patch reaches it."""
        clear_usage_limited_until("acme", cleared_at=_NOW)

        path = cw.dispatch_state.DISPATCH_STATE_FILE
        assert path.is_relative_to(tmp_config_dir)
        assert _raw()[CLEAR_MARKS_KEY] == {"acme": _NOW.isoformat()}


class TestImportDirection:
    def test_dispatch_state_does_not_import_the_marks_module(self) -> None:
        source = Path(cw.dispatch_state.__file__).read_text()
        modules = [
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]

        assert "cw.dispatch_state_marks_parse" in modules
        assert "cw.dispatch_state_marks" not in modules

    @pytest.mark.parametrize(
        "order",
        [
            ("cw.dispatch_state", "cw.dispatch_state_marks"),
            ("cw.dispatch_state_marks", "cw.dispatch_state"),
        ],
    )
    def test_both_import_orders_succeed_in_a_fresh_interpreter(
        self, order: tuple[str, str]
    ) -> None:
        code = f"import {order[0]}; import {order[1]}"

        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
