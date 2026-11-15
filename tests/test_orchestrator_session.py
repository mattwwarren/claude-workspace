"""Tests for :mod:`cw.orchestrator_session` (#3170)."""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

import pytest

from cw.config import state_dir
from cw.orchestrator_session import (
    ORCHESTRATOR_AGENT,
    ORCHESTRATOR_CHANNEL,
    attach_command,
    attach_session,
    build_orchestrator_mcp_config,
    operator_channel_reachable,
    orchestrator_launch_args,
    orchestrator_mcp_config_path,
    remove_orchestrator_mcp_config,
    stdio_is_tty,
    unreachable_warning,
    write_orchestrator_mcp_config,
)
from tests._tool_fakes import GhReply, RecordingShim, prepend_to_path, restore_real_run

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


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:notaport", "not a url", "", "ftp://127.0.0.1"]
)
def test_operator_channel_reachable_false_on_unparseable_url(url: str) -> None:
    assert operator_channel_reachable(url, timeout=0.2) is False


def test_unreachable_warning_names_serve_command_and_fallback() -> None:
    warning = unreachable_warning("http://127.0.0.1:8789")

    assert warning.startswith("warning: cw-operator channel server not reachable")
    assert "http://127.0.0.1:8789" in warning
    assert "cw queue-channel serve --host 127.0.0.1 --port 8789" in warning
    assert "cw orchestrate watch" in warning


def test_unreachable_warning_unparseable_url_names_bare_serve() -> None:
    warning = unreachable_warning("not a url")

    assert "start it with: cw queue-channel serve." in warning


def test_operator_channel_reachable_https_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int]] = []

    def _connect(address: tuple[str, int], timeout: float) -> socket.socket:
        seen.append(address)
        msg = "refused"
        raise ConnectionRefusedError(msg)

    monkeypatch.setattr(socket, "create_connection", _connect)

    assert operator_channel_reachable("https://example.invalid") is False
    assert seen == [("example.invalid", 443)]


# ---------------------------------------------------------------------------
# Default attach helpers (#3667)
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_stdio_is_tty_requires_both_streams(
    monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stdout_tty: bool, expected: bool
) -> None:
    monkeypatch.setattr("sys.stdin", _Stream(tty=stdin_tty))
    monkeypatch.setattr("sys.stdout", _Stream(tty=stdout_tty))
    assert stdio_is_tty() is expected


def test_stdio_is_tty_false_for_stream_without_isatty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", object())
    monkeypatch.setattr("sys.stdout", _Stream(tty=True))
    assert stdio_is_tty() is False


def test_attach_command_uses_short_id() -> None:
    assert attach_command("00000001") == "claude attach 00000001"


def test_attach_session_success_records_one_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = RecordingShim(tmp_path, "claude", responses={"attach": GhReply()})
    restore_real_run(monkeypatch)
    prepend_to_path(monkeypatch, shim.bin_dir)

    assert attach_session("00000001") == 0
    assert shim.calls == [["attach", "00000001"]]


def test_attach_session_returns_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = RecordingShim(
        tmp_path, "claude", responses={"attach": GhReply(returncode=3)}
    )
    restore_real_run(monkeypatch)
    prepend_to_path(monkeypatch, shim.bin_dir)

    assert attach_session("00000001") == 3


@pytest.mark.parametrize(
    "error", [FileNotFoundError("claude"), PermissionError("claude")]
)
def test_attach_session_launch_failure_returns_none_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: OSError,
) -> None:
    def _raise(*_a: object, **_k: object) -> None:
        raise error

    monkeypatch.setattr("cw.orchestrator_session.subprocess.run", _raise)
    with caplog.at_level(logging.WARNING, logger="cw.orchestrator_session"):
        assert attach_session("00000001") is None
    assert any("claude attach" in r.getMessage() for r in caplog.records)
