"""Tests for cw.doctor.usage_limit, the active usage-limit window advisory (#3547)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.doctor import _check_usage_limit_windows, run_doctor
from cw.lane_pause import iso_z
from tests._shared.dispatch_fixtures import seed_usage_limit_window

_UNTIL = datetime.now(UTC) + timedelta(hours=2)


def test_no_window_emits_nothing(tmp_config_dir: Path) -> None:
    assert _check_usage_limit_windows() == []


def test_each_active_window_is_an_advisory(tmp_config_dir: Path) -> None:
    seed_usage_limit_window("zeta", _UNTIL)
    seed_usage_limit_window("acme", _UNTIL)

    results = _check_usage_limit_windows()

    assert [r.name for r in results] == ["usage-limit/acme", "usage-limit/zeta"]
    for result, client in zip(results, ("acme", "zeta"), strict=True):
        assert result.ok is True
        assert result.warn is True
        assert result.detail == (
            f"usage-limit window for {client} is active until {iso_z(_UNTIL)};"
            f" lift it early with: cw dev-queue usage-limit clear -c {client}"
        )


def test_client_absent_from_clients_yaml_still_reports(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw.doctor import _deps

    monkeypatch.setattr(_deps, "load_clients", dict)
    seed_usage_limit_window("ghost", _UNTIL)

    names = [check.name for check in run_doctor().checks]

    assert "usage-limit/ghost" in names


def test_sidecar_read_error_yields_no_check(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> dict[str, datetime]:
        msg = "unreadable"
        raise OSError(msg)

    monkeypatch.setattr("cw.usage_limit_window.load_usage_limited_until", _boom)

    assert _check_usage_limit_windows() == []
