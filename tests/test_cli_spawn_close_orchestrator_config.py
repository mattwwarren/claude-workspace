"""``cw spawn close`` removes an orchestrator's per-session MCP config (#3170).

Maps to :func:`cw.cli.spawn._spawn_close_impl`.
"""

from __future__ import annotations

from pathlib import Path

from cw.cli.spawn import _spawn_close_impl
from cw.models import SessionStatus
from cw.native_daemon import FakeNativeDaemonClient
from cw.orchestrator_session import (
    orchestrator_mcp_config_path,
    write_orchestrator_mcp_config,
)
from tests.conftest import _make_daemon_session, _seed_daemon_session, _seed_sessions


def test_close_removes_orchestrator_mcp_config(
    tmp_path: Path, tmp_config_dir: Path, mock_native_daemon: FakeNativeDaemonClient
) -> None:
    _seed_daemon_session(tmp_path, tmp_config_dir, session_id="orch0001")
    path = write_orchestrator_mcp_config("orch0001", "test-client")

    _spawn_close_impl(session_id="orch0001", native_daemon=mock_native_daemon)

    assert not path.exists()


def test_close_already_completed_session_still_removes_file(
    mock_native_daemon: FakeNativeDaemonClient,
) -> None:
    _seed_sessions(_make_daemon_session(id="orch0002", status=SessionStatus.COMPLETED))
    path = write_orchestrator_mcp_config("orch0002", "client-a")

    _spawn_close_impl(session_id="orch0002", native_daemon=mock_native_daemon)

    assert not path.exists()


def test_close_without_file_is_noop(
    tmp_path: Path, tmp_config_dir: Path, mock_native_daemon: FakeNativeDaemonClient
) -> None:
    _seed_daemon_session(tmp_path, tmp_config_dir, session_id="plain001")
    path = orchestrator_mcp_config_path("plain001")
    assert not path.exists()

    _spawn_close_impl(session_id="plain001", native_daemon=mock_native_daemon)

    assert not path.exists()
    assert not path.parent.exists()


def test_close_leaves_other_sessions_files(
    mock_native_daemon: FakeNativeDaemonClient,
) -> None:
    _seed_sessions(
        _make_daemon_session(id="orch0003", name="client-a/cw-orchestrator"),
        _make_daemon_session(id="orch0004", name="client-a/cw-orchestrator"),
    )
    closed = write_orchestrator_mcp_config("orch0003", "client-a")
    kept = write_orchestrator_mcp_config("orch0004", "client-a")

    _spawn_close_impl(session_id="orch0003", native_daemon=mock_native_daemon)

    assert not closed.exists()
    assert kept.exists()
