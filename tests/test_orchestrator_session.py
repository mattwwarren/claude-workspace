"""Tests for :mod:`cw.orchestrator_session` (#3170)."""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

import pytest
from cw.orchestrator_session import (
    ORCHESTRATOR_AGENT,
    ORCHESTRATOR_CHANNEL,
    build_orchestrator_mcp_config,
    operator_channel_reachable,
    orchestrator_launch_args,
    orchestrator_mcp_config_path,
    remove_orchestrator_mcp_config,
    unreachable_warning,
    write_orchestrator_mcp_config,
)

from cw.config import state_dir

_EXAMPLE = (
    Path(__file__).parent.parent / "config" / "cw-operator-events.mcp.json.example"
)


def test_mcp_config_path_is_under_state_dir_keyed_by_session_id() -> None:
    path = orchestrator_mcp_config_path("abc12345")

    assert path == state_dir() / "orchestrator" / "abc12345.mcp.json"


def test_build_mcp_config_matches_example_shape() -> None:
    example = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    server = example["mcpServers"]["cw-operator"]
    server["args"][-1] = "my-client"
    server["env"]["CW_OPERATOR_EVENTS_BASE_URL"] = "http://127.0.0.1:9999"

    built = build_orchestrator_mcp_config("my-client", "http://127.0.0.1:9999")

    assert built == example


def test_write_mcp_config_creates_file_and_dir_with_exact_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CW_OPERATOR_EVENTS_BASE_URL", "http://127.0.0.1:7777")
    target_dir = state_dir() / "orchestrator"
    assert not target_dir.exists()

    path = write_orchestrator_mcp_config("sid00001", "acme")

    assert path == orchestrator_mcp_config_path("sid00001")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "cw-operator": {
                "command": "cw",
                "args": ["operator-channel", "proxy", "--client-id", "acme"],
                "env": {"CW_OPERATOR_EVENTS_BASE_URL": "http://127.0.0.1:7777"},
            }
        }
    }


def test_remove_mcp_config_deletes_and_is_idempotent() -> None:
    path = write_orchestrator_mcp_config("sid00002", "acme")
    assert path.exists()

    assert remove_orchestrator_mcp_config("sid00002") is True
    assert not path.exists()
    assert remove_orchestrator_mcp_config("sid00002") is True


def test_remove_mcp_config_swallows_oserror_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(self: Path, missing_ok: bool = False) -> None:
        msg = "denied"
        raise PermissionError(msg)

    monkeypatch.setattr(Path, "unlink", _boom)

    with caplog.at_level(logging.WARNING, logger="cw.orchestrator_session"):
        assert remove_orchestrator_mcp_config("sid00003") is False

    assert "sid00003" in caplog.text


def test_launch_args_order_and_no_strict_mcp_config(tmp_path: Path) -> None:
    config = tmp_path / "x.mcp.json"

    args = orchestrator_launch_args(config)

    assert args == [
        "--agent",
        ORCHESTRATOR_AGENT,
        "--mcp-config",
        str(config),
        "--dangerously-load-development-channels",
        ORCHESTRATOR_CHANNEL,
    ]
    assert ORCHESTRATOR_AGENT == "cw-orchestrator"
    assert ORCHESTRATOR_CHANNEL == "server:cw-operator"
    assert "--strict-mcp-config" not in args


def test_operator_channel_reachable_true_on_listening_socket() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        assert operator_channel_reachable(f"http://127.0.0.1:{port}") is True


def test_operator_channel_reachable_false_on_closed_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The socket is closed: nothing listens on *port* now.

    assert operator_channel_reachable(f"http://127.0.0.1:{port}", timeout=0.5) is False


@pytest.mark.parametrize("url", ["http://127.0.0.1:notaport", "not a url", ""])
def test_operator_channel_reachable_false_on_unparseable_url(url: str) -> None:
    assert operator_channel_reachable(url, timeout=0.2) is False


def test_unreachable_warning_names_serve_command_and_fallback() -> None:
    warning = unreachable_warning("http://127.0.0.1:8789")

    assert warning.startswith("warning: cw-operator channel server not reachable")
    assert "http://127.0.0.1:8789" in warning
    assert "cw queue-channel serve --host 127.0.0.1 --port 8789" in warning
    assert "cw orchestrate watch" in warning
