"""Tests for the client-resolution helpers in :mod:`cw.cli._base` (#3170)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cw._util import NO_CLIENTS_MSG
from cw.cli._base import _resolve_client, _resolve_client_or_infer
from cw.client_resolution import ClientInferenceError
from cw.exceptions import CwError, UnknownClientError
from tests._clients_yaml import ClientSpec, write_clients_yaml


def _two_clients(tmp_path: Path) -> tuple[Path, Path]:
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    write_clients_yaml(
        ClientSpec("client-a", ws_a),
        ClientSpec("client-b", ws_b),
        ensure_workspaces=True,
    )
    return ws_a, ws_b


# ---------------------------------------------------------------------------
# _resolve_client (first-client default now warns)
# ---------------------------------------------------------------------------


def test_resolve_client_explicit_name_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_clients(tmp_path)

    client = _resolve_client("client-b")

    assert client.name == "client-b"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_resolve_client_default_warns_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_clients(tmp_path)

    client = _resolve_client(None)

    assert client.name == "client-a"
    captured = capsys.readouterr()
    assert "no --client given; defaulting to client-a" in captured.err
    assert captured.out == ""


def test_resolve_client_no_clients_raises() -> None:
    with pytest.raises(CwError, match="No clients configured") as exc:
        _resolve_client(None)
    assert str(exc.value) == NO_CLIENTS_MSG


# ---------------------------------------------------------------------------
# _resolve_client_or_infer
# ---------------------------------------------------------------------------


def test_resolve_client_or_infer_provided_uses_it_without_inference_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws_a, _ = _two_clients(tmp_path)
    monkeypatch.chdir(ws_a)

    client = _resolve_client_or_infer("client-b")

    assert client.name == "client-b"
    captured = capsys.readouterr()
    assert "inferred" not in captured.out
    assert captured.err == ""


def test_resolve_client_or_infer_unknown_name_lists_configured(
    tmp_path: Path,
) -> None:
    _two_clients(tmp_path)

    with pytest.raises(UnknownClientError) as exc_info:
        _resolve_client_or_infer("nope")

    assert "client-a, client-b" in str(exc_info.value)


def test_resolve_client_or_infer_single_match_prints_inferred_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, ws_b = _two_clients(tmp_path)
    sub = ws_b / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    client = _resolve_client_or_infer(None)

    assert client.name == "client-b"
    captured = capsys.readouterr()
    assert f"client inferred from {sub.resolve()}: client-b" in captured.out
    assert captured.err == ""


def test_resolve_client_or_infer_dispatch_worktree_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "ws"
    base = tmp_path / "wt-base"
    wt = base / "dev-7"
    wt.mkdir(parents=True)
    write_clients_yaml(
        ClientSpec("other", tmp_path / "other"),
        ClientSpec("dispatched", ws, worktree_base=base),
        ensure_workspaces=True,
    )
    monkeypatch.chdir(wt)

    client = _resolve_client_or_infer(None)

    assert client.name == "dispatched"
    assert "client inferred from" in capsys.readouterr().out


def test_resolve_client_or_infer_nested_warns_naming_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "sub"
    write_clients_yaml(
        ClientSpec("outer", outer), ClientSpec("inner", inner), ensure_workspaces=True
    )
    monkeypatch.chdir(inner)

    client = _resolve_client_or_infer(None)

    assert client.name == "inner"
    captured = capsys.readouterr()
    assert "'inner'" in captured.err
    assert "'outer'" in captured.err
    assert "client inferred from" in captured.out


def test_resolve_client_or_infer_tie_refuses_naming_both_and_pass_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "shared"
    write_clients_yaml(ClientSpec("a", ws), ClientSpec("b", ws), ensure_workspaces=True)
    monkeypatch.chdir(ws)

    with pytest.raises(ClientInferenceError) as exc_info:
        _resolve_client_or_infer(None)

    msg = str(exc_info.value)
    assert "'a'" in msg
    assert "'b'" in msg
    assert "pass -c <name>" in msg


def test_resolve_client_or_infer_no_match_refuses_and_never_first_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_clients(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    with pytest.raises(ClientInferenceError) as exc_info:
        _resolve_client_or_infer(None)

    assert "Configured clients: client-a, client-b" in str(exc_info.value)
    assert "defaulting to" not in capsys.readouterr().err


def test_resolve_client_or_infer_resolves_symlinked_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, ws_b = _two_clients(tmp_path)
    link = tmp_path / "link-to-b"
    link.symlink_to(ws_b, target_is_directory=True)
    monkeypatch.chdir(link)

    client = _resolve_client_or_infer(None)

    assert client.name == "client-b"
    assert f"client inferred from {ws_b.resolve()}" in capsys.readouterr().out
