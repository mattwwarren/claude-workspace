"""Tests for cw.doctor.bindings (#3702, RFC 0019 R1).

``bindings/<client>`` compares ``bindings.tickets.system`` with the project's
``tracking.primary.system``. Config and project-config reads only: no ``gh``
or ``claude`` launch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.doctor.bindings import _check_bindings
from cw.doctor.core import _client_config_checks
from cw.models import ClientConfig
from cw.models.bindings import Bindings
from cw.models.client import TrackerMcpGateConfig
from tests._doctor_helpers import make_client
from tests._shared.cli_preflight import _write_tracker_config

if TYPE_CHECKING:
    from pathlib import Path


def _tickets(system: str | None) -> Bindings:
    body: dict[str, object] = {"mcp": {"codex": {"server": "linear"}}}
    if system is not None:
        body["system"] = system
    return Bindings.model_validate({"tickets": body})


def _bound_client(root: Path, system: str | None) -> ClientConfig:
    return make_client(bindings=_tickets(system), workspace_path=root)


def test_agreeing_systems_are_ok(tmp_path: Path) -> None:
    _write_tracker_config(tmp_path, "linear")
    [result] = _check_bindings({"client-a": _bound_client(tmp_path, "linear")})
    assert result.name == "bindings/client-a"
    assert result.ok is True
    assert result.warn is False


def test_disagreeing_systems_fail_naming_both(tmp_path: Path) -> None:
    _write_tracker_config(tmp_path, "github-issues")
    [result] = _check_bindings({"client-a": _bound_client(tmp_path, "linear")})
    assert result.ok is False
    assert result.warn is False
    assert "linear" in result.detail
    assert "github-issues" in result.detail


def test_unset_binding_system_warns(tmp_path: Path) -> None:
    _write_tracker_config(tmp_path, "linear")
    [result] = _check_bindings({"client-a": _bound_client(tmp_path, None)})
    assert (result.ok, result.warn) == (True, True)
    assert "set bindings.tickets.system" in result.detail
    assert "pin tracking.primary.system" not in result.detail


def test_missing_project_config_warns(tmp_path: Path) -> None:
    [result] = _check_bindings({"client-a": _bound_client(tmp_path, "linear")})
    assert (result.ok, result.warn) == (True, True)
    assert "pin tracking.primary.system" in result.detail
    assert "set bindings.tickets.system" not in result.detail


def test_empty_project_system_warns(tmp_path: Path) -> None:
    _write_tracker_config(tmp_path, '""')
    [result] = _check_bindings({"client-a": _bound_client(tmp_path, "linear")})
    assert (result.ok, result.warn) == (True, True)


def test_client_without_tickets_binding_emits_nothing(tmp_path: Path) -> None:
    assert _check_bindings({"client-a": make_client(workspace_path=tmp_path)}) == []


def test_legacy_migrated_client_warns(tmp_path: Path) -> None:
    _write_tracker_config(tmp_path, "linear")
    client = make_client(
        tracker_mcp_gate=TrackerMcpGateConfig(plugin_id="linear@acme"),
        workspace_path=tmp_path,
    )
    [result] = _check_bindings({"client-a": client})
    assert (result.ok, result.warn) == (True, True)


def test_repo_path_takes_precedence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
    _write_tracker_config(repo, "linear")
    _write_tracker_config(workspace, "github-issues")
    client = ClientConfig(
        name="client-a",
        repo_path=repo,
        branch="main",
        workspace_path=workspace,
        bindings=_tickets("linear"),
    )
    [result] = _check_bindings({"client-a": client})
    assert (result.ok, result.warn) == (True, False)


def test_client_config_checks_include_the_row(tmp_path: Path) -> None:
    _write_tracker_config(tmp_path, "linear")
    names = [
        result.name
        for result in _client_config_checks(
            {"client-a": _bound_client(tmp_path, "linear")}
        )
    ]
    assert "bindings/client-a" in names
