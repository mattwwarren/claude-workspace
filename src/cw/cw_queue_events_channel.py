"""MCP stdio proxy: forwards cw-queue-events SSE notifications to Claude."""

from __future__ import annotations

import functools
from typing import Any

from cw import _events_channel_base

_DEFAULT_BASE_URL = "http://127.0.0.1:8789"
# Intentionally not imported from cw_queue_events_server to avoid triggering
# module-level I/O (_cursors.update/_event_offset init) at import time.
_NOTIFICATION_TYPE = "cw-queue-event"

_CHANNEL_INSTRUCTIONS = """\
Queue events from cw-queue-events server. Event types:
- queue.ticket_enqueued: new ticket added to queue (ticket_id, client, status)
- queue.ticket_claimed: PENDING->RUNNING (ticket_id, client, session_id)
- queue.ticket_completed: RUNNING->COMPLETED (ticket_id, client, queue_status, \
sentinel_status or null)
- queue.ticket_failed: RUNNING->FAILED (ticket_id, client, error or null, attempts)
  Note: ticket_failed fires from two code paths: cli.py validation_failed \
(after 3 attempts)
  and queue.py mark_item_failed(). Subscribers should not assume broader \
failure coverage.
- queue.session_idled: session ACTIVE->IDLE (session_id, session_name)
- queue.session_reaped: reconcile disposed of a session (session_id, \
surface_ref or null, origin, reason, from_status, to_status)
  reason values: phantom_surface (dead daemon surface), idle_stall (watchdog \
recover, retried), usage_limit_cutoff (usage-limit hit, retried), \
retry_cap_parked (retry cap reached, BLOCKED_ON_USER), wall_clock_budget \
(wall-clock budget exceeded, retried), completed_backstop (backstop revert of \
TIMED_OUT or COMPLETED session with no prior reason), salvage_completed \
(git-state HIGH-path auto-PR), salvage_parked (git-state LOW-path flagged \
for human)
RUNNING->CANCELLED: no event (system-driven cleanup)\
"""


def _build_meta(data: dict[str, Any]) -> dict[str, str]:
    """Build the meta dict for notifications/claude/channel params."""
    meta: dict[str, str] = {
        "event_type": str(data.get("event", "")),
        "ticket_id": str(data.get("ticket_id", "")),
        "client": str(data.get("client", "")),
    }
    return {k: v for k, v in meta.items() if v}


_CONFIG = _events_channel_base.ChannelProxyConfig(
    server_name="cw-queue-events",
    default_base_url=_DEFAULT_BASE_URL,
    base_url_env="CW_QUEUE_EVENTS_BASE_URL",
    client_id_env="CW_QUEUE_EVENTS_CLIENT_ID",
    sse_path="/sse/",
    notification_type=_NOTIFICATION_TYPE,
    build_meta=_build_meta,
    instructions=_CHANNEL_INSTRUCTIONS,
)

_extract_payload = functools.partial(
    _events_channel_base.extract_payload, notification_type=_NOTIFICATION_TYPE
)
_build_outbound_notification = functools.partial(
    _events_channel_base.build_outbound_notification, build_meta=_build_meta
)
_relay_upstream = functools.partial(_events_channel_base.relay_upstream, config=_CONFIG)
run_proxy = functools.partial(_events_channel_base.run_proxy, config=_CONFIG)


if __name__ == "__main__":
    run_proxy()
