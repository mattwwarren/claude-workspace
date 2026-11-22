"""The scope guard's merge base: the worktree context first (RFC 0020 S3a).

``resolve_scope_guard_default_branch`` reads ``<worktree>/.claude/cw-context.json``
for the row's base, then the client's merge base with no row, then the literal
``"main"`` with the existing warning. It never raises.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from cw.models import ClientConfig
from cw.worktree._scope import resolve_scope_guard_default_branch

if TYPE_CHECKING:
    from pathlib import Path

CLIENT = "client-a"
CLIENT_DEFAULT = "trunk"
CONTEXT_REL = (".claude", "cw-context.json")


def _client(tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        name=CLIENT,
        workspace_path=tmp_path,
        default_branch=CLIENT_DEFAULT,
        worktree_base=tmp_path / "wt",
    )


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch, clients: dict[str, ClientConfig]
) -> None:
    monkeypatch.setattr("cw.worktree._scope.load_effective_clients", lambda: clients)


def _write_context(worktree: Path, body: str) -> None:
    (worktree / CONTEXT_REL[0]).mkdir(parents=True, exist_ok=True)
    (worktree / CONTEXT_REL[0] / CONTEXT_REL[1]).write_text(body)


def _resolve(worktree: Path | None) -> str:
    return resolve_scope_guard_default_branch(
        CLIENT, log_context="session=s1", worktree_path=worktree
    )


def test_row_base_in_context_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    _write_context(tmp_path, json.dumps({"base_branch": "epic/x"}))
    assert _resolve(tmp_path) == "epic/x"


def test_v22_context_uses_origin_main_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    body = {"world_state_snapshot": {"origin_main_branch": "main"}}
    _write_context(tmp_path, json.dumps(body))
    assert _resolve(tmp_path) == "main"


def test_missing_context_falls_back_to_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    assert _resolve(tmp_path) == CLIENT_DEFAULT


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[1, 2, 3]",
        json.dumps({"base_branch": ""}),
        json.dumps({"base_branch": 7}),
    ],
)
def test_unusable_context_falls_back_to_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    _write_context(tmp_path, body)
    assert _resolve(tmp_path) == CLIENT_DEFAULT


def test_deeply_nested_context_falls_back_to_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # json.loads raises RecursionError (not ValueError) on pathological nesting;
    # the Stop hook must still get a base rather than lose its sentinel.
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    _write_context(tmp_path, "[" * 200_000)

    assert _resolve(tmp_path) == CLIENT_DEFAULT


def test_unreadable_context_falls_back_to_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    # A directory where the file should be: read_text raises IsADirectoryError.
    (tmp_path / CONTEXT_REL[0] / CONTEXT_REL[1]).mkdir(parents=True)
    assert _resolve(tmp_path) == CLIENT_DEFAULT


def test_no_worktree_uses_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    assert _resolve(None) == CLIENT_DEFAULT


def test_config_failure_with_context_base_still_uses_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> dict[str, ClientConfig]:
        msg = "clients.yaml is unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.worktree._scope.load_effective_clients", _boom)
    _write_context(tmp_path, json.dumps({"base_branch": "epic/x"}))
    assert _resolve(tmp_path) == "epic/x"


def test_config_failure_without_context_falls_back_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom() -> dict[str, ClientConfig]:
        msg = "clients.yaml is unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr("cw.worktree._scope.load_effective_clients", _boom)
    with caplog.at_level(logging.WARNING, logger="cw.worktree"):
        assert _resolve(tmp_path) == "main"
    assert "scope_verification_client_unresolved" in caplog.text


def test_missing_client_key_falls_back_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_clients(monkeypatch, {})
    with caplog.at_level(logging.WARNING, logger="cw.worktree"):
        assert _resolve(tmp_path) == "main"
    assert "client=client-a" in caplog.text
