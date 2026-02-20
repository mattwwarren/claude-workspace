"""Tests for cw.notify module."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from cw.history import EventType, HistoryEvent
from cw.notify import notify_event, send_notification


def test_send_notification_no_notify_send() -> None:
    """Returns False when notify-send is not installed."""
    with patch("cw.notify.shutil.which", return_value=None):
        assert send_notification("Test", "Body") is False


def test_send_notification_success() -> None:
    """Returns True when notify-send succeeds."""
    with (
        patch("cw.notify.shutil.which", return_value="/usr/bin/notify-send"),
        patch("cw.notify.subprocess.run") as mock_run,
    ):
        result = send_notification("Title", "Body", urgency="normal")
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "notify-send"
        assert "Title" in args
        assert "Body" in args


def test_send_notification_os_error() -> None:
    """Returns False on OSError."""
    with (
        patch("cw.notify.shutil.which", return_value="/usr/bin/notify-send"),
        patch("cw.notify.subprocess.run", side_effect=OSError("fail")),
    ):
        assert send_notification("Title", "Body") is False


def test_send_notification_timeout() -> None:
    """Returns False on subprocess timeout."""
    with (
        patch("cw.notify.shutil.which", return_value="/usr/bin/notify-send"),
        patch(
            "cw.notify.subprocess.run",
            side_effect=subprocess.TimeoutExpired("notify-send", 5),
        ),
    ):
        assert send_notification("Title", "Body") is False


def test_notify_event_crash_uses_critical() -> None:
    """Crash events use critical urgency."""
    event = HistoryEvent(
        event_type=EventType.SESSION_CRASHED,
        client="test",
        session_name="test/impl",
    )
    with patch("cw.notify.send_notification") as mock_send:
        notify_event(event)
        mock_send.assert_called_once()
        _args, kwargs = mock_send.call_args
        assert kwargs["urgency"] == "critical"


def test_notify_event_normal_urgency() -> None:
    """Non-crash events use normal urgency."""
    event = HistoryEvent(
        event_type=EventType.SESSION_COMPLETED,
        client="test",
        session_name="test/impl",
    )
    with patch("cw.notify.send_notification") as mock_send:
        notify_event(event)
        mock_send.assert_called_once()
        _args, kwargs = mock_send.call_args
        assert kwargs["urgency"] == "normal"


def test_notify_event_skips_session_started() -> None:
    """SESSION_STARTED is not in notification map, so no notification."""
    event = HistoryEvent(
        event_type=EventType.SESSION_STARTED,
        client="test",
    )
    with patch("cw.notify.send_notification") as mock_send:
        notify_event(event)
        mock_send.assert_not_called()


def test_notify_event_daemon_format() -> None:
    """Daemon events format client/purpose into the body."""
    event = HistoryEvent(
        event_type=EventType.DAEMON_STARTED,
        client="sigma",
        purpose="debt",
    )
    with patch("cw.notify.send_notification") as mock_send:
        notify_event(event)
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        assert "sigma/debt" in args[1]
