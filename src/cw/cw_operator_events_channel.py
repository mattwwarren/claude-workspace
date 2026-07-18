"""MCP stdio proxy: forwards cw-operator SSE notifications to Claude."""

from __future__ import annotations

import functools
from typing import Any

from cw import _events_channel_base
from cw.models import OrchestratorEventType

# Same host/port as cw-queue-events -- the operator channel is a distinct SSE
# topic on the EXISTING queue-events server (RFC 0008 W3, #1002), not a new
# server/port.
_DEFAULT_BASE_URL = "http://127.0.0.1:8789"
# Intentionally not imported from cw_operator_events to avoid triggering
# module-level I/O (_cursors.update/_event_offset init) at import time.
# cw.models has no such side effects, so OrchestratorEventType is imported
# directly above rather than duplicated as a raw string.
_NOTIFICATION_TYPE = "cw-operator-event"


def _build_meta(data: dict[str, Any]) -> dict[str, str]:
    """Build the meta dict for notifications/claude/channel params."""
    meta: dict[str, str] = {
        "event_type": str(data.get("event", "")),
        "correlation_id": str(data.get("correlation_id") or ""),
        "client": str(data.get("client") or ""),
    }
    return {k: v for k, v in meta.items() if v}


def _always_relay(payload: dict[str, Any]) -> bool:
    # Why: pr.registered's payload has no "client" key (a pre-existing
    # producer gap this ticket is the first to surface as a forwarding
    # gap; fixing the producer is out of scope -- #1002). Always relay it
    # even when this proxy is scoped to one client via --client-id: a
    # scoped operator silently missing PR registrations is worse than
    # rare cross-client noise on this one low-volume event type.
    return payload.get("event") == OrchestratorEventType.PR_REGISTERED.value


_CONFIG = _events_channel_base.ChannelProxyConfig(
    server_name="cw-operator",
    default_base_url=_DEFAULT_BASE_URL,
    base_url_env="CW_OPERATOR_EVENTS_BASE_URL",
    client_id_env="CW_OPERATOR_EVENTS_CLIENT_ID",
    sse_path="/sse/operator/",
    notification_type=_NOTIFICATION_TYPE,
    build_meta=_build_meta,
    always_relay=_always_relay,
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
