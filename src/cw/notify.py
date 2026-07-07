"""Push notification helper for sessions that need operator attention."""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger(__name__)

_PEON_TIMEOUT = 5  # seconds
_HOOK_EVENT_NAME = "Notification"
_NOTIFICATION_TYPE = "input.required"

_SUBPROCESS_ERRORS = (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError)

# RFC 0008 capstone (#1015) — cw.watchdog's desktop-notification timeout.
# Separate constant from _PEON_TIMEOUT: same value today, but the two call
# sites (fire_push_notification's async peon-ping/notify-send pair vs.
# send_desktop_notification's synchronous notify-send/osascript pair) are
# independent and may need to diverge later.
_DESKTOP_NOTIFY_TIMEOUT = 5  # seconds


@lru_cache(maxsize=1)
def _peon_sh_path() -> Path | None:
    """Locate peon.sh in the standard install location."""
    p = Path.home() / ".claude" / "hooks" / "peon-ping" / "peon.sh"
    return p if p.is_file() else None


def _fire_push_notification_sync(session_name: str, client: str, cwd: str = "") -> None:
    """Blocking implementation — called in a daemon thread by fire_push_notification."""
    payload = json.dumps(
        {
            "hook_event_name": _HOOK_EVENT_NAME,
            "notification_type": _NOTIFICATION_TYPE,
            "session_name": session_name,
            "client": client,
            "cwd": cwd,
        }
    )
    peon = _peon_sh_path()
    if peon is not None:
        try:
            subprocess.run(
                ["bash", str(peon)],
                input=payload,
                text=True,
                timeout=_PEON_TIMEOUT,
                capture_output=True,
                check=False,
            )
        except _SUBPROCESS_ERRORS as e:
            _log.debug("notify peon-ping failed (acceptable): %s", e)

    try:
        subprocess.run(
            [
                "notify-send",
                "cw: session needs attention",
                f"{client}: {session_name}",
            ],
            timeout=_PEON_TIMEOUT,
            capture_output=True,
            check=False,
        )
    except _SUBPROCESS_ERRORS as e:
        _log.debug("notify notify-send failed (acceptable): %s", e)


def fire_push_notification(session_name: str, client: str, *, cwd: str = "") -> None:
    """Fire a push notification for a session that needs operator attention.

    Runs in a daemon thread — non-blocking. Calls peon-ping (if installed) then
    tries notify-send as a Linux fallback. All failures are swallowed.
    """
    threading.Thread(
        target=_fire_push_notification_sync,
        args=(session_name, client, cwd),
        daemon=True,
    ).start()


def _escape_applescript_string(value: str) -> str:
    """Escape backslashes/quotes for embedding in an AppleScript string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def send_desktop_notification(title: str, message: str) -> None:
    """Fire a SYNCHRONOUS desktop notification (RFC 0008 capstone, #1015).

    A NEW, separate helper from :func:`fire_push_notification` — deliberately
    NOT a retrofit of it. ``fire_push_notification`` backgrounds into a daemon
    thread because its caller (a long-running reconcile tick inside the
    dispatch loop) keeps running after the call returns. ``cw.watchdog``'s
    one-shot ``tick`` invocation IS the whole process lifetime: if this fired
    on a daemon thread, the process could exit (systemd/launchd tears down
    the unit once the foreground command returns) before the notification
    ever sends. Dispatches to ``notify-send`` on Linux and ``osascript`` on
    macOS (:func:`platform.system` returns ``"Darwin"``); any other platform
    falls through to the Linux path and swallows the resulting failure like
    everything else here. All failures are swallowed — a notification-delivery
    failure must never fail ``cw watchdog tick``.
    """
    try:
        if platform.system() == "Darwin":
            script = (
                f'display notification "{_escape_applescript_string(message)}"'
                f' with title "{_escape_applescript_string(title)}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                timeout=_DESKTOP_NOTIFY_TIMEOUT,
                capture_output=True,
                check=False,
            )
        else:
            subprocess.run(
                ["notify-send", title, message],
                timeout=_DESKTOP_NOTIFY_TIMEOUT,
                capture_output=True,
                check=False,
            )
    except _SUBPROCESS_ERRORS as e:
        _log.debug("send_desktop_notification failed (acceptable): %s", e)
