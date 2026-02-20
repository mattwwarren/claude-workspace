"""Desktop notifications for session lifecycle events."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.history import HistoryEvent

from cw.history import EventType

# Map event types to notification title/body templates
_EVENT_NOTIFICATIONS: dict[EventType, tuple[str, str]] = {
    EventType.SESSION_COMPLETED: ("Session Completed", "{session_name}"),
    EventType.SESSION_CRASHED: ("Session Crashed", "{session_name}"),
    EventType.SESSION_BACKGROUNDED: ("Session Backgrounded", "{session_name}"),
    EventType.SESSION_RESUMED: ("Session Resumed", "{session_name}"),
    EventType.SESSION_HANDOFF: ("Session Handoff", "{detail}"),
    EventType.QUEUE_ITEM_COMPLETED: ("Queue Item Completed", "{detail}"),
    EventType.QUEUE_ITEM_FAILED: ("Queue Item Failed", "{detail}"),
    EventType.DAEMON_STARTED: ("Daemon Started", "{client}/{purpose}"),
    EventType.DAEMON_STOPPED: ("Daemon Stopped", "{client}/{purpose}"),
}

# Global flag set by record_event when notifications are enabled
_notifications_enabled: bool = False


def set_notifications_enabled(enabled: bool) -> None:
    """Set the global notification flag (called during config loading)."""
    global _notifications_enabled  # noqa: PLW0603
    _notifications_enabled = enabled


def send_notification(
    title: str,
    body: str,
    *,
    urgency: str = "normal",
) -> bool:
    """Send a desktop notification via notify-send.

    Returns True if the notification was sent successfully, False otherwise.
    """
    if not shutil.which("notify-send"):
        return False

    try:
        subprocess.run(
            ["notify-send", f"--urgency={urgency}", "--app-name=cw", title, body],
            check=False,
            capture_output=True,
        )
    except OSError:
        return False
    return True


def notify_event(event: HistoryEvent) -> None:
    """Send a desktop notification for a history event."""
    template = _EVENT_NOTIFICATIONS.get(event.event_type)
    if template is None:
        return

    title_template, body_template = template
    format_vars = {
        "client": event.client,
        "session_name": event.session_name or "",
        "session_id": event.session_id or "",
        "purpose": event.purpose or "",
        "detail": event.detail or "",
    }
    title = title_template.format_map(format_vars)
    body = body_template.format_map(format_vars)

    urgency = "critical" if event.event_type == EventType.SESSION_CRASHED else "normal"
    send_notification(title, body, urgency=urgency)


def maybe_notify_event(event: HistoryEvent) -> None:
    """Send a notification if notifications are globally enabled."""
    if _notifications_enabled:
        notify_event(event)
