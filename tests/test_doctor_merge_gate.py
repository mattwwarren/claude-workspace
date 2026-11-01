"""Tests for cw.doctor.merge_gate (#2851)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.doctor import run_doctor
from cw.doctor.merge_gate import _check_merge_gate_disabled
from cw.models import ClientConfig


def _client(name: str, *, enabled: bool | None = None) -> ClientConfig:
    if enabled is None:
        return ClientConfig(name=name, workspace_path=Path("/dev/null"))
    return ClientConfig(
        name=name, workspace_path=Path("/dev/null"), merge_gate_enabled=enabled
    )


def test_empty_clients_returns_no_results() -> None:
    assert _check_merge_gate_disabled({}) == []


def test_default_and_explicit_true_clients_emit_nothing() -> None:
    clients = {"a": _client("a"), "b": _client("b", enabled=True)}
    assert _check_merge_gate_disabled(clients) == []


def test_each_disabled_client_is_listed_as_an_advisory_warning() -> None:
    clients = {
        "on": _client("on", enabled=True),
        "off-one": _client("off-one", enabled=False),
        "off-two": _client("off-two", enabled=False),
    }

    results = _check_merge_gate_disabled(clients)

    assert [r.name for r in results] == ["merge-gate/off-one", "merge-gate/off-two"]
    for result in results:
        assert result.ok is True
        assert result.warn is True
        assert "Stage 4a merge gate is DISABLED" in result.detail
        assert "manual rebase" in result.detail
    assert "client 'off-one'" in results[0].detail


def test_run_doctor_lists_a_disabled_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.doctor import _deps

    monkeypatch.setattr(
        _deps, "load_clients", lambda: {"off": _client("off", enabled=False)}
    )

    report = run_doctor()

    names = [check.name for check in report.checks]
    assert "merge-gate/off" in names
