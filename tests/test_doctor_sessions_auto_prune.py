"""Tests for cw.doctor.sessions_auto_prune — the sessions-auto-prune check (#2965).

The ``run_doctor()`` registration and ``--json`` cases live in
``tests/test_doctor.py`` (``_stub_claude_version_ok`` is private to that file).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from cw.config import state_dir
from cw.doctor._shared import CheckResult
from cw.doctor.sessions_auto_prune import _check_sessions_auto_prune
from cw.exceptions import ConfigValidationError
from cw.models import OrchestratorConfig
from cw.session_auto_prune import AutoPruneStatus

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_CUTOFF = _NOW - timedelta(days=30)


def _write_status(**overrides: object) -> AutoPruneStatus:
    fields: dict[str, object] = {
        "last_run_at": _NOW - timedelta(hours=2),
        "status": "ok",
        "archived_count": 3,
        "deleted_count": 0,
        "kept_count": 10,
        "cutoff": _CUTOFF,
        "age_days": 30,
        "max_per_run": 500,
        "capped": False,
        "bytes_before": 9000,
        "bytes_after": 4000,
    }
    fields.update(overrides)
    status = AutoPruneStatus.model_validate(fields)
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "sessions_auto_prune.json").write_text(status.model_dump_json())
    return status


def _patch_config(monkeypatch: pytest.MonkeyPatch, config: OrchestratorConfig) -> None:
    monkeypatch.setattr(
        "cw.doctor.sessions_auto_prune.load_orchestrator_config", lambda: config
    )


class TestCheckSessionsAutoPrune:
    def test_check_absent_sidecar_enabled(self, tmp_config_dir: Path) -> None:
        result = _check_sessions_auto_prune()

        assert isinstance(result, CheckResult)
        assert result.name == "sessions-auto-prune"
        assert result.ok is True
        assert result.warn is False
        assert "not run yet" in result.detail
        assert "30" in result.detail
        assert "500" in result.detail

    def test_check_absent_sidecar_disabled(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_config(
            monkeypatch, OrchestratorConfig(sessions_auto_prune_enabled=False)
        )

        result = _check_sessions_auto_prune()

        assert result.ok is True
        assert result.warn is False
        assert "disabled" in result.detail
        assert "sessions_auto_prune_enabled" in result.detail

    def test_check_reports_last_run(self, tmp_config_dir: Path) -> None:
        _write_status()

        with freeze_time(_NOW):
            result = _check_sessions_auto_prune()

        assert result.ok is True
        assert result.warn is False
        assert "archived 3" in result.detail
        assert _CUTOFF.isoformat() in result.detail
        assert "9000B->4000B" in result.detail
        assert "2h ago" in result.detail
        assert "capped" not in result.detail

    def test_check_disabled_with_recorded_run_prefixes_last_run(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_status()
        _patch_config(
            monkeypatch, OrchestratorConfig(sessions_auto_prune_enabled=False)
        )

        with freeze_time(_NOW):
            result = _check_sessions_auto_prune()

        assert result.ok is True
        assert result.warn is False
        assert result.detail.startswith("disabled via sessions_auto_prune_enabled; ")
        assert "archived 3" in result.detail

    def test_check_reports_capped_run(self, tmp_config_dir: Path) -> None:
        _write_status(capped=True)

        with freeze_time(_NOW):
            result = _check_sessions_auto_prune()

        assert result.warn is False
        assert "capped; more eligible" in result.detail

    def test_check_failed_last_run_warns_ok_true(self, tmp_config_dir: Path) -> None:
        _write_status(status="failed", error="OSError: disk full", archived_count=0)

        with freeze_time(_NOW):
            result = _check_sessions_auto_prune()

        assert result.ok is True
        assert result.warn is True
        assert "OSError: disk full" in result.detail

    def test_check_corrupt_sidecar_fails_open(self, tmp_config_dir: Path) -> None:
        (state_dir() / "sessions_auto_prune.json").write_text("{not json")

        result = _check_sessions_auto_prune()

        assert result.ok is True
        assert result.warn is False

    def test_check_bad_orchestrator_config_degrades_to_defaults(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise() -> OrchestratorConfig:
            msg = "simulated bad orchestrator.yaml"
            raise ConfigValidationError(msg)

        monkeypatch.setattr(
            "cw.doctor.sessions_auto_prune.load_orchestrator_config", _raise
        )

        result = _check_sessions_auto_prune()

        assert result.ok is True
        assert result.warn is False

    def test_check_never_calls_load_state_or_prunes(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            msg = "the doctor check must stay read-only"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.config.load_state", _boom)
        monkeypatch.setattr("cw.session_retention.prune_sessions", _boom)
        _write_status()

        with freeze_time(_NOW):
            result = _check_sessions_auto_prune()

        assert result.ok is True
