"""Tests for ``cw orchestrator-start`` (#295, #3170).

Moved out of ``tests/test_cli.py``. The session subscribes to the
``cw-operator`` channel through a per-session ``--mcp-config`` file, and the
client is inferred from the working directory when ``-c`` is omitted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cw.cli import main
from cw.config import state_dir
from cw.exceptions import CwError, SpawnUnregisteredError, WorkerLaunchedError
from cw.native_daemon import FakeNativeDaemonClient
from cw.orchestrator_session import orchestrator_mcp_config_path
from tests._clients_yaml import ClientSpec, write_clients_yaml

_SPAWNED_RE = re.compile(r"Spawned orchestrator session: (\S+)")


@pytest.fixture(autouse=True)
def _channel_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the reachability probe to "up" so no test opens a real socket."""
    monkeypatch.setattr(
        "cw.cli.orchestrate.operator_channel_reachable", lambda *_a, **_k: True
    )


@pytest.fixture
def daemon(
    mock_native_daemon: FakeNativeDaemonClient, monkeypatch: pytest.MonkeyPatch
) -> FakeNativeDaemonClient:
    monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: mock_native_daemon)
    return mock_native_daemon


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["orchestrator-start", *args])


def _spawned_id(result: Result) -> str:
    match = _SPAWNED_RE.search(result.stdout)
    assert match is not None, result.output
    return match.group(1)


def _orchestrator_files() -> list[Path]:
    target = state_dir() / "orchestrator"
    return sorted(target.glob("*")) if target.exists() else []


# ---------------------------------------------------------------------------
# Launch argv and the per-session MCP config
# ---------------------------------------------------------------------------


def test_launch_argv_loads_cw_operator_via_per_session_mcp_config(
    daemon: FakeNativeDaemonClient, make_git_repo: Callable[..., Path]
) -> None:
    ws = make_git_repo("ws-args")
    write_clients_yaml(ClientSpec("args-client", ws))

    result = _invoke("--client", "args-client")

    assert result.exit_code == 0, result.output
    sid = _spawned_id(result)
    extra = daemon.spawn_extra_args[0]
    assert extra is not None
    assert extra[-6:] == [
        "--agent",
        "cw-orchestrator",
        "--mcp-config",
        str(orchestrator_mcp_config_path(sid)),
        "--dangerously-load-development-channels",
        "server:cw-operator",
    ]
    assert "--strict-mcp-config" not in extra
    assert "server:cw-pr-events" not in extra
    assert daemon.spawn_permission_modes[0] == "acceptEdits"


def test_mcp_config_file_contents_and_location(
    daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CW_OPERATOR_EVENTS_BASE_URL", "http://127.0.0.1:8790")
    ws = make_git_repo("ws-file")
    write_clients_yaml(ClientSpec("file-client", ws))

    result = _invoke("-c", "file-client")

    assert result.exit_code == 0, result.output
    sid = _spawned_id(result)
    path = state_dir() / "orchestrator" / f"{sid}.mcp.json"
    assert _orchestrator_files() == [path]
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "cw-operator": {
                "command": "cw",
                "args": ["operator-channel", "proxy", "--client-id", "file-client"],
                "env": {"CW_OPERATOR_EVENTS_BASE_URL": "http://127.0.0.1:8790"},
            }
        }
    }
    assert daemon.spawn_calls


def test_repo_mcp_json_untouched(
    daemon: FakeNativeDaemonClient, make_git_repo: Callable[..., Path]
) -> None:
    ws = make_git_repo("ws-untouched")
    write_clients_yaml(ClientSpec("c", ws))

    result = _invoke("-c", "c")

    assert result.exit_code == 0, result.output
    assert not (ws / ".mcp.json").exists()
    # spawn_create_impl writes its Stop-hook files under <ws>/.claude/; no MCP
    # config may land there.
    assert list(ws.rglob("*.mcp.json")) == []
    assert daemon.spawn_calls


def test_unreachable_server_warns_and_still_spawns(
    daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cw.cli.orchestrate.operator_channel_reachable", lambda *_a, **_k: False
    )
    ws = make_git_repo("ws-down")
    write_clients_yaml(ClientSpec("c", ws))

    result = _invoke("-c", "c")

    assert result.exit_code == 0, result.output
    assert "cw-operator channel server not reachable" in result.stderr
    assert "cw queue-channel serve" in result.stderr
    assert len(daemon.spawn_calls) == 1


def test_reachable_server_prints_no_warning(
    daemon: FakeNativeDaemonClient, make_git_repo: Callable[..., Path]
) -> None:
    ws = make_git_repo("ws-up")
    write_clients_yaml(ClientSpec("c", ws))

    result = _invoke("-c", "c")

    assert result.exit_code == 0, result.output
    assert "not reachable" not in result.stderr
    assert len(daemon.spawn_calls) == 1


def test_spawn_failure_removes_mcp_config(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_git_repo("ws-fail")
    write_clients_yaml(ClientSpec("c", ws))
    seen: list[Path] = []

    def _boom(**_kwargs: object) -> str:
        seen.extend(_orchestrator_files())
        msg = "spawn failed"
        raise CwError(msg)

    monkeypatch.setattr("cw.cli.orchestrate.spawn_create_impl", _boom)

    result = _invoke("-c", "c")

    assert result.exit_code != 0
    assert "spawn failed" in result.output
    assert len(seen) == 1
    assert _orchestrator_files() == []


def test_worker_launched_error_keeps_mcp_config(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_git_repo("ws-live")
    write_clients_yaml(ClientSpec("c", ws))

    def _boom(**kwargs: object) -> str:
        sid = kwargs["session_id"]
        assert isinstance(sid, str)
        msg = "post-launch"
        raise WorkerLaunchedError(msg, session_id=sid, surface_ref="y")

    monkeypatch.setattr("cw.cli.orchestrate.spawn_create_impl", _boom)

    result = _invoke("-c", "c")

    assert result.exit_code != 0
    assert "post-launch" in result.output
    assert len(_orchestrator_files()) == 1


def test_spawn_unregistered_keeps_mcp_config(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = make_git_repo("ws-unreg")
    write_clients_yaml(ClientSpec("c", ws))
    captured: dict[str, object] = {}

    def _boom(**kwargs: object) -> str:
        captured.update(kwargs)
        msg = "never registered"
        raise SpawnUnregisteredError(msg)

    monkeypatch.setattr("cw.cli.orchestrate.spawn_create_impl", _boom)

    result = _invoke("-c", "c")

    assert result.exit_code != 0
    sid = captured["session_id"]
    assert isinstance(sid, str)
    assert orchestrator_mcp_config_path(sid).exists()


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def test_client_provided_used(
    daemon: FakeNativeDaemonClient, make_git_repo: Callable[..., Path]
) -> None:
    ws = make_git_repo("ws-explicit")
    other = make_git_repo("ws-other")
    write_clients_yaml(ClientSpec("other", other), ClientSpec("mytest", ws))

    result = _invoke("--client", "mytest")

    assert result.exit_code == 0, result.output
    assert "client inferred" not in result.stdout
    assert len(daemon.spawn_calls) == 1
    assert daemon.spawn_calls[0][0] == ws


def test_unknown_client_name_fails_listing_configured(tmp_path: Path) -> None:
    ws = tmp_path / "ws-unknown"
    ws.mkdir()
    write_clients_yaml(ClientSpec("real-client", ws))

    result = _invoke("--client", "unknown-name")

    assert result.exit_code != 0
    assert "unknown-name" in result.output
    assert "real-client" in result.output


def test_client_inferred_single_match_prints_inferred_line(
    daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_git_repo("ws-first")
    ws = make_git_repo("ws-second")
    write_clients_yaml(ClientSpec("first", first), ClientSpec("second", ws))
    sub = ws / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert f"client inferred from {sub.resolve()}: second" in result.stdout
    assert daemon.spawn_calls[0][0] == ws


def test_client_inferred_from_dispatch_worktree(
    daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = make_git_repo("ws-first")
    ws = make_git_repo("ws-dispatch")
    base = tmp_path / "wt-base"
    wt = base / "dev-3170"
    wt.mkdir(parents=True)
    write_clients_yaml(
        ClientSpec("first", first), ClientSpec("dispatch", ws, worktree_base=base)
    )
    monkeypatch.chdir(wt)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert "client inferred from" in result.stdout
    assert daemon.spawn_calls[0][0] == ws


def test_client_inferred_nested_warns_naming_both(
    daemon: FakeNativeDaemonClient,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer = make_git_repo("ws-outer")
    inner = make_git_repo("ws-inner", base=outer)
    write_clients_yaml(ClientSpec("outer", outer), ClientSpec("inner", inner))
    monkeypatch.chdir(inner)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert "'inner'" in result.stderr
    assert "'outer'" in result.stderr
    assert daemon.spawn_calls[0][0] == inner


def test_client_tie_refuses_naming_both_clients_and_path(
    daemon: FakeNativeDaemonClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "shared"
    write_clients_yaml(ClientSpec("a", ws), ClientSpec("b", ws), ensure_workspaces=True)
    monkeypatch.chdir(ws)

    result = _invoke()

    assert result.exit_code != 0
    assert "'a'" in result.output
    assert "'b'" in result.output
    assert str(ws.resolve()) in result.output
    assert daemon.spawn_calls == []
    assert _orchestrator_files() == []


def test_client_no_match_refuses_without_first_client_fallback(
    daemon: FakeNativeDaemonClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_clients_yaml(
        ClientSpec("first", tmp_path / "ws-first"),
        ClientSpec("second", tmp_path / "ws-second"),
        ensure_workspaces=True,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = _invoke()

    assert result.exit_code != 0
    assert "could not infer a client" in result.output
    assert "Configured clients: first, second" in result.output
    assert "defaulting to" not in result.output
    assert daemon.spawn_calls == []


def test_no_clients_configured_message_kept() -> None:
    result = _invoke()

    assert result.exit_code != 0
    assert "No clients configured" in result.output
