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

from cw.auto_dev_result import AutoDevResult
from cw.cli.stop_hook import sentinel
from cw.models import ClientConfig
from cw.reconcile._shared import _scope_probes
from cw.reconcile._shared._scope_probes import (
    CapturedScope,
    ScopeKey,
    apply_prefetched_scope,
    capture_scope_probe,
    new_scope_probes,
)
from cw.worktree._scope import resolve_scope_guard_default_branch
from tests._reconcile_helpers import _inflate_scope, _stage_complete_payload
from tests._shared.session_seeding import _make_daemon_session

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import Session

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


# --- caller wiring: the worktree reaches the resolver (S3a) -----------------


def _inflated() -> AutoDevResult:
    return AutoDevResult.model_validate(_inflate_scope(_stage_complete_payload()))


def _session(worktree: Path) -> Session:
    return _make_daemon_session(id="sess-ctx", client=CLIENT, worktree_path=worktree)


def _spy_compute(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    calls: list[tuple[Path, str]] = []

    def _compute(worktree: Path, base: str) -> None:
        calls.append((worktree, base))

    monkeypatch.setattr(_scope_probes, "compute_branch_diff_scope", _compute)
    return calls


def test_read_scope_captures_the_context_base_not_the_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    _write_context(tmp_path, json.dumps({"base_branch": "epic/x"}))
    measured_against = _spy_compute(monkeypatch)
    now: datetime = _session(tmp_path).started_at

    captured = _scope_probes._read_scope(_session(tmp_path), _inflated(), tmp_path, now)

    assert captured.base_branch == "epic/x"
    assert measured_against == [(tmp_path, "epic/x")]


def test_read_scope_without_a_context_base_keeps_the_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    measured_against = _spy_compute(monkeypatch)
    now: datetime = _session(tmp_path).started_at

    captured = _scope_probes._read_scope(_session(tmp_path), _inflated(), tmp_path, now)

    assert captured.base_branch == CLIENT_DEFAULT
    assert measured_against == [(tmp_path, CLIENT_DEFAULT)]


def test_apply_prefetched_scope_applies_the_captured_context_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_clients(monkeypatch, {CLIENT: _client(tmp_path)})
    _write_context(tmp_path, json.dumps({"base_branch": "epic/x"}))
    _spy_compute(monkeypatch)
    applied: list[str] = []

    def _apply(
        result: AutoDevResult, _measured: object, **kwargs: object
    ) -> AutoDevResult:
        applied.append(str(kwargs["default_branch"]))
        return result

    monkeypatch.setattr(_scope_probes, "apply_measured_scope", _apply)
    session = _session(tmp_path)
    result = _inflated()
    probes = new_scope_probes()
    assert capture_scope_probe(probes, session, result) is True
    assert probes.lookup(ScopeKey(session.id, tmp_path)).base_branch == "epic/x"

    apply_prefetched_scope(result, session, probes)

    assert applied == ["epic/x"]


def test_apply_prefetched_scope_uses_the_stored_base_over_the_client_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    result = _inflated()
    probes = new_scope_probes()
    probes.capture(
        ScopeKey(session.id, tmp_path),
        read=lambda _at: CapturedScope(
            result=result, base_branch="epic/x", measured=None
        ),
    )
    applied: list[str] = []

    def _apply(
        res: AutoDevResult, _measured: object, **kwargs: object
    ) -> AutoDevResult:
        applied.append(str(kwargs["default_branch"]))
        return res

    monkeypatch.setattr(_scope_probes, "apply_measured_scope", _apply)

    apply_prefetched_scope(result, session, probes)

    assert applied == ["epic/x"]


def test_verify_headless_scope_passes_the_session_worktree_to_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver_kwargs: dict[str, object] = {}
    reconcile_kwargs: dict[str, object] = {}

    def _resolve(client_name: str, **kwargs: object) -> str:
        resolver_kwargs.update(kwargs, client_name=client_name)
        return "epic/x"

    def _reconcile(result: AutoDevResult, **kwargs: object) -> AutoDevResult:
        reconcile_kwargs.update(kwargs)
        return result

    monkeypatch.setattr(sentinel, "resolve_scope_guard_default_branch", _resolve)
    monkeypatch.setattr(sentinel, "reconcile_result_scope", _reconcile)
    session = _session(tmp_path)

    sentinel._verify_headless_scope(_inflated(), session)

    assert resolver_kwargs["worktree_path"] == tmp_path
    assert resolver_kwargs["client_name"] == CLIENT
    assert reconcile_kwargs["default_branch"] == "epic/x"
