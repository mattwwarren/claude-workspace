"""Tests for cw.notify — push notification helper."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from cw.notify import _peon_sh_path, fire_push_notification


class TestPeonShPath:
    def test_returns_path_when_exists(self, tmp_path: Path) -> None:
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        peon_sh = peon_dir / "peon.sh"
        peon_sh.write_text("#!/bin/bash")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _peon_sh_path() == peon_sh

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _peon_sh_path() is None


class TestFirePushNotification:
    def test_calls_peon_ping_with_correct_json(self, tmp_path: Path) -> None:
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        peon_sh = peon_dir / "peon.sh"
        peon_sh.write_text("#!/bin/bash")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            fire_push_notification("my-client/auto-dev/123", "my-client")
            assert mock_run.called
            # First call should be peon-ping
            first_call = mock_run.call_args_list[0]
            assert "peon.sh" in str(first_call)
            payload = json.loads(first_call.kwargs.get("input", "{}"))
            assert payload["hook_event_name"] == "Notification"
            assert payload["notification_type"] == "input.required"

    def test_swallows_peon_failure(self, tmp_path: Path) -> None:
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        (peon_dir / "peon.sh").write_text("#!/bin/bash")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run", side_effect=OSError("boom")),
        ):
            fire_push_notification("x", "y")  # must not raise

    def test_notify_send_fallback(self, tmp_path: Path) -> None:
        # No peon.sh → notify-send only
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            fire_push_notification("x", "y")
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("notify-send" in c for c in calls)

    def test_swallows_notify_send_failure(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run", side_effect=OSError("no notify-send")),
        ):
            fire_push_notification("x", "y")  # must not raise

    def test_payload_includes_cwd(self, tmp_path: Path) -> None:
        """cwd parameter is included in the peon-ping payload."""
        peon_dir = tmp_path / ".claude" / "hooks" / "peon-ping"
        peon_dir.mkdir(parents=True)
        (peon_dir / "peon.sh").write_text("#!/bin/bash")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            fire_push_notification("my-session", "my-client", cwd="/workspace/foo")
            first_call = mock_run.call_args_list[0]
            payload = json.loads(first_call.kwargs.get("input", "{}"))
            assert payload["cwd"] == "/workspace/foo"
            assert payload["session_name"] == "my-session"
            assert payload["client"] == "my-client"
