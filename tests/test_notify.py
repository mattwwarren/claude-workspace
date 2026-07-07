"""Tests for cw.notify — push notification helper."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    import pytest

from cw.notify import (
    _fire_push_notification_sync,
    _peon_sh_path,
    fire_push_notification,
    send_desktop_notification,
)


class TestPeonShPath:
    def test_returns_path_when_exists(self, tmp_path: Path) -> None:
        _peon_sh_path.cache_clear()
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        peon_sh = peon_dir / "peon.sh"
        peon_sh.write_text("#!/bin/bash")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _peon_sh_path() == peon_sh

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        _peon_sh_path.cache_clear()
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _peon_sh_path() is None


class TestFirePushNotification:
    def test_calls_peon_ping_with_correct_json(self, tmp_path: Path) -> None:
        _peon_sh_path.cache_clear()
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        peon_sh = peon_dir / "peon.sh"
        peon_sh.write_text("#!/bin/bash")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _fire_push_notification_sync("my-client/auto-dev/123", "my-client")
            assert mock_run.called
            # First call should be peon-ping
            first_call = mock_run.call_args_list[0]
            assert "peon.sh" in str(first_call)
            payload = json.loads(first_call.kwargs.get("input", "{}"))
            assert payload["hook_event_name"] == "Notification"
            assert payload["notification_type"] == "input.required"

    def test_swallows_peon_failure(self, tmp_path: Path) -> None:
        _peon_sh_path.cache_clear()
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        (peon_dir / "peon.sh").write_text("#!/bin/bash")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run", side_effect=OSError("boom")),
        ):
            _fire_push_notification_sync("x", "y")  # must not raise

    def test_notify_send_fallback(self, tmp_path: Path) -> None:
        _peon_sh_path.cache_clear()
        # No peon.sh → notify-send only
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _fire_push_notification_sync("x", "y")
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("notify-send" in c for c in calls)

    def test_swallows_notify_send_failure(self, tmp_path: Path) -> None:
        _peon_sh_path.cache_clear()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run", side_effect=OSError("no notify-send")),
        ):
            _fire_push_notification_sync("x", "y")  # must not raise

    def test_payload_includes_cwd(self, tmp_path: Path) -> None:
        """cwd parameter is included in the peon-ping payload."""
        _peon_sh_path.cache_clear()
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        (peon_dir / "peon.sh").write_text("#!/bin/bash")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _fire_push_notification_sync("my-session", "my-client", "/workspace/foo")
            first_call = mock_run.call_args_list[0]
            payload = json.loads(first_call.kwargs.get("input", "{}"))
            assert payload["cwd"] == "/workspace/foo"
            assert payload["session_name"] == "my-session"
            assert payload["client"] == "my-client"

    def test_fire_push_notification_is_non_blocking(self) -> None:
        """fire_push_notification starts a daemon thread."""
        with patch("cw.notify.threading") as mock_threading:
            mock_thread = MagicMock()
            mock_threading.Thread.return_value = mock_thread
            fire_push_notification("s", "c", cwd="/tmp")
            mock_threading.Thread.assert_called_once_with(
                target=_fire_push_notification_sync,
                args=("s", "c", "/tmp"),
                daemon=True,
            )
            mock_thread.start.assert_called_once()

    def test_peon_failure_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A peon-ping failure logs at DEBUG level; function returns cleanly."""
        _peon_sh_path.cache_clear()
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        (peon_dir / "peon.sh").write_text("#!/bin/bash")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run", side_effect=OSError("peon-ping exploded")),
            caplog.at_level(logging.DEBUG, logger="cw.notify"),
        ):
            _fire_push_notification_sync("my-session", "my-client")
        assert any(
            "peon-ping" in r.message and "acceptable" in r.message
            for r in caplog.records
        ), f"Expected peon-ping debug log, got: {[r.message for r in caplog.records]}"

    def test_notify_send_failure_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A notify-send failure logs at DEBUG level; function returns cleanly."""
        _peon_sh_path.cache_clear()
        # No peon.sh → only notify-send path runs
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run", side_effect=OSError("notify-send missing")),
            caplog.at_level(logging.DEBUG, logger="cw.notify"),
        ):
            _fire_push_notification_sync("my-session", "my-client")
        assert any(
            "notify-send" in r.message and "acceptable" in r.message
            for r in caplog.records
        ), f"Expected notify-send debug log, got: {[r.message for r in caplog.records]}"


class TestSendDesktopNotification:
    """RFC 0008 capstone (#1015): synchronous watchdog desktop notification."""

    def test_linux_dispatches_notify_send(self) -> None:
        with (
            patch("cw.notify.platform.system", return_value="Linux"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            send_desktop_notification("cw watchdog", "gate parked 46m")
            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            assert args == ["notify-send", "cw watchdog", "gate parked 46m"]

    def test_macos_dispatches_osascript(self) -> None:
        with (
            patch("cw.notify.platform.system", return_value="Darwin"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            send_desktop_notification("cw watchdog", "gate parked 46m")
            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            assert args[0] == "osascript"
            assert args[1] == "-e"
            assert "display notification" in args[2]
            assert "gate parked 46m" in args[2]
            assert "cw watchdog" in args[2]

    def test_macos_escapes_quotes_in_message(self) -> None:
        with (
            patch("cw.notify.platform.system", return_value="Darwin"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            send_desktop_notification("title", 'has "quotes" in it')
            script = mock_run.call_args.args[0][2]
            assert '\\"quotes\\"' in script

    def test_unknown_platform_falls_back_to_notify_send(self) -> None:
        with (
            patch("cw.notify.platform.system", return_value="FreeBSD"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            send_desktop_notification("t", "m")
            assert mock_run.call_args.args[0][0] == "notify-send"

    def test_swallows_failure_linux(self) -> None:
        with (
            patch("cw.notify.platform.system", return_value="Linux"),
            patch("subprocess.run", side_effect=OSError("no notify-send")),
        ):
            send_desktop_notification("t", "m")  # must not raise

    def test_swallows_failure_macos(self) -> None:
        with (
            patch("cw.notify.platform.system", return_value="Darwin"),
            patch("subprocess.run", side_effect=OSError("no osascript")),
        ):
            send_desktop_notification("t", "m")  # must not raise

    def test_failure_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("cw.notify.platform.system", return_value="Linux"),
            patch("subprocess.run", side_effect=OSError("boom")),
            caplog.at_level(logging.DEBUG, logger="cw.notify"),
        ):
            send_desktop_notification("t", "m")
        assert any(
            "send_desktop_notification" in r.message and "acceptable" in r.message
            for r in caplog.records
        )
