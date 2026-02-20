"""Tests for cw.daemon - background daemon management."""

from __future__ import annotations

import os
import signal
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.daemon import (
    _ensure_not_running,
    _is_process_alive,
    _pid_path,
    daemon_status,
    stop_daemon,
)
from cw.exceptions import CwError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def tmp_daemons_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect DAEMONS_DIR to an isolated tmp_path location."""
    daemons_dir = tmp_path / "daemons"
    daemons_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.DAEMONS_DIR", daemons_dir)
    monkeypatch.setattr("cw.daemon.DAEMONS_DIR", daemons_dir)
    return daemons_dir


class TestPidPath:
    def test_returns_correct_path(self, tmp_daemons_dir: Path) -> None:
        result = _pid_path("acme", "debt")
        assert result == tmp_daemons_dir / "acme-debt.pid"

    def test_includes_client_and_purpose_in_name(self, tmp_daemons_dir: Path) -> None:
        result = _pid_path("my-client", "review")
        assert result.name == "my-client-review.pid"
        assert result.parent == tmp_daemons_dir

    def test_different_clients_produce_different_paths(
        self, tmp_daemons_dir: Path
    ) -> None:
        path_a = _pid_path("client-a", "debt")
        path_b = _pid_path("client-b", "debt")
        assert path_a != path_b

    def test_different_purposes_produce_different_paths(
        self, tmp_daemons_dir: Path
    ) -> None:
        path_debt = _pid_path("acme", "debt")
        path_review = _pid_path("acme", "review")
        assert path_debt != path_review


class TestIsProcessAlive:
    def test_returns_true_for_current_process(self, tmp_daemons_dir: Path) -> None:
        assert _is_process_alive(os.getpid()) is True

    def test_returns_false_for_nonexistent_pid(self, tmp_daemons_dir: Path) -> None:
        # PID 0 is the kernel scheduler and can't be signaled by userspace;
        # use a very large PID that is almost certainly unallocated.
        # os.kill raises ProcessLookupError for truly nonexistent PIDs.
        nonexistent_pid = 999_999_999
        assert _is_process_alive(nonexistent_pid) is False

    def test_returns_true_on_permission_error(self, tmp_daemons_dir: Path) -> None:
        with patch("os.kill", side_effect=PermissionError):
            assert _is_process_alive(1) is True

    def test_returns_false_on_process_lookup_error(
        self, tmp_daemons_dir: Path
    ) -> None:
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _is_process_alive(99999) is False


class TestEnsureNotRunning:
    def test_does_nothing_when_no_pid_file(self, tmp_daemons_dir: Path) -> None:
        # Should not raise
        _ensure_not_running("acme", "debt")

    def test_raises_when_daemon_is_running(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text(str(os.getpid()))

        with pytest.raises(CwError, match="Daemon already running for acme/debt"):
            _ensure_not_running("acme", "debt")

    def test_error_message_includes_pid(self, tmp_daemons_dir: Path) -> None:
        current_pid = os.getpid()
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text(str(current_pid))

        with pytest.raises(CwError, match=str(current_pid)):
            _ensure_not_running("acme", "debt")

    def test_cleans_stale_pid_file(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        # Write a PID for a process that doesn't exist
        with patch("cw.daemon._is_process_alive", return_value=False):
            pid_file.write_text("99999")
            _ensure_not_running("acme", "debt")

        assert not pid_file.exists()

    def test_cleans_corrupt_pid_file(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text("not-a-number")

        # Should not raise; corrupt file gets cleaned up
        _ensure_not_running("acme", "debt")
        assert not pid_file.exists()

    def test_cleans_empty_pid_file(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text("")

        _ensure_not_running("acme", "debt")
        assert not pid_file.exists()


class TestStopDaemon:
    def test_raises_when_no_pid_file(self, tmp_daemons_dir: Path) -> None:
        with pytest.raises(CwError, match="No daemon running for acme/debt"):
            stop_daemon("acme", "debt")

    def test_raises_with_correct_client_and_purpose(
        self, tmp_daemons_dir: Path
    ) -> None:
        with pytest.raises(CwError, match="No daemon running for my-client/review"):
            stop_daemon("my-client", "review")

    def test_sends_sigterm_to_running_process(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        fake_pid = 12345
        pid_file.write_text(str(fake_pid))

        kill_calls: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))

        with (
            patch("cw.daemon._is_process_alive", return_value=True),
            patch("os.kill", side_effect=fake_kill),
        ):
            stop_daemon("acme", "debt")

        assert len(kill_calls) == 1
        assert kill_calls[0] == (fake_pid, signal.SIGTERM)

    def test_cleans_stale_pid_file_without_killing(
        self, tmp_daemons_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text("99999")

        with patch("cw.daemon._is_process_alive", return_value=False):
            stop_daemon("acme", "debt")

        assert not pid_file.exists()
        output = capsys.readouterr().out
        assert "stale" in output.lower() or "not running" in output.lower()

    def test_default_purpose_is_debt(self, tmp_daemons_dir: Path) -> None:
        # No PID file for debt → raises with "debt" in message
        with pytest.raises(CwError, match="No daemon running for acme/debt"):
            stop_daemon("acme")

    def test_raises_on_invalid_pid_file_content(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text("not-a-number")

        with pytest.raises(CwError, match="Invalid PID file"):
            stop_daemon("acme", "debt")


class TestDaemonStatus:
    def test_returns_empty_when_no_pid_files(self, tmp_daemons_dir: Path) -> None:
        result = daemon_status()
        assert result == []

    def test_returns_empty_when_client_filter_has_no_match(
        self, tmp_daemons_dir: Path
    ) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text(str(os.getpid()))

        result = daemon_status(client="other-client")
        assert result == []

    def test_returns_daemon_info_from_pid_file(self, tmp_daemons_dir: Path) -> None:
        current_pid = os.getpid()
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text(str(current_pid))

        result = daemon_status()

        assert len(result) == 1
        entry = result[0]
        assert entry["client"] == "acme"
        assert entry["purpose"] == "debt"
        assert entry["pid"] == current_pid
        assert entry["alive"] is True

    def test_alive_false_for_dead_process(self, tmp_daemons_dir: Path) -> None:
        pid_file = tmp_daemons_dir / "acme-debt.pid"
        pid_file.write_text("99999")

        with patch("cw.daemon._is_process_alive", return_value=False):
            result = daemon_status()

        assert len(result) == 1
        assert result[0]["alive"] is False

    def test_filters_by_client(self, tmp_daemons_dir: Path) -> None:
        (tmp_daemons_dir / "acme-debt.pid").write_text(str(os.getpid()))
        (tmp_daemons_dir / "beta-debt.pid").write_text(str(os.getpid()))

        result = daemon_status(client="acme")

        assert len(result) == 1
        assert result[0]["client"] == "acme"

    def test_returns_all_clients_when_no_filter(self, tmp_daemons_dir: Path) -> None:
        (tmp_daemons_dir / "acme-debt.pid").write_text(str(os.getpid()))
        (tmp_daemons_dir / "beta-review.pid").write_text(str(os.getpid()))

        result = daemon_status()

        assert len(result) == 2
        clients = {entry["client"] for entry in result}
        assert clients == {"acme", "beta"}

    def test_skips_malformed_pid_files(self, tmp_daemons_dir: Path) -> None:
        (tmp_daemons_dir / "acme-debt.pid").write_text("not-a-pid")

        result = daemon_status()

        assert result == []

    def test_skips_pid_files_with_malformed_stem(
        self, tmp_daemons_dir: Path
    ) -> None:
        # A file whose stem can't be split into exactly two parts
        (tmp_daemons_dir / "nodash.pid").write_text(str(os.getpid()))

        result = daemon_status()

        assert result == []

    def test_multiple_daemons_same_client(self, tmp_daemons_dir: Path) -> None:
        current_pid = os.getpid()
        (tmp_daemons_dir / "acme-debt.pid").write_text(str(current_pid))
        (tmp_daemons_dir / "acme-review.pid").write_text(str(current_pid))

        result = daemon_status(client="acme")

        assert len(result) == 2
        purposes = {entry["purpose"] for entry in result}
        assert purposes == {"debt", "review"}

    def test_creates_daemons_dir_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemons_dir = tmp_path / "new" / "daemons"
        monkeypatch.setattr("cw.daemon.DAEMONS_DIR", daemons_dir)

        result = daemon_status()

        assert daemons_dir.exists()
        assert result == []
