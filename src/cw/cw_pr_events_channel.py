"""MCP stdio proxy: forwards cw-pr-events SSE notifications to Claude."""

from __future__ import annotations

import functools
from typing import Any

from cw import _events_channel_base

_DEFAULT_BASE_URL = "http://127.0.0.1:8788"
# Intentionally not imported from cw_pr_events_server to avoid triggering
# module-level I/O (_cursors.update/_event_offset init) at import time.
_NOTIFICATION_TYPE = "cw-pr-event"


def _build_meta(data: dict[str, Any]) -> dict[str, str]:
    """Build the meta dict for notifications/claude/channel params."""
    payload = data.get("payload") or {}
    meta: dict[str, str] = {
        "event_type": str(data.get("event_type", "")),
        "pr_number": str(data.get("pr_number", "")),
        "repo": str(data.get("repo", "")),
    }
    if "role" in payload:
        meta["role"] = str(payload["role"])
    if "client" in payload:
        meta["client"] = str(payload["client"])
    return {k: v for k, v in meta.items() if v}


_CONFIG = _events_channel_base.ChannelProxyConfig(
    server_name="cw-pr-events",
    default_base_url=_DEFAULT_BASE_URL,
    base_url_env="CW_PR_EVENTS_BASE_URL",
    client_id_env="CW_PR_EVENTS_CLIENT_ID",
    sse_path="/sse/",
    notification_type=_NOTIFICATION_TYPE,
    build_meta=_build_meta,
    # pr's proxy has never filtered by client -- preserve that behavior.
    filter_by_client=False,
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
