"""MCP stdio proxy: forwards cw-operator SSE notifications to Claude."""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.parse
from typing import Any, cast

logger = logging.getLogger(__name__)

# Same host/port as cw-queue-events -- the operator channel is a distinct SSE
# topic on the EXISTING queue-events server (RFC 0008 W3, #1002), not a new
# server/port.
_DEFAULT_BASE_URL = "http://127.0.0.1:8789"
# Intentionally not imported from cw_operator_events to avoid triggering
# module-level I/O (_cursors.update/_event_offset init) at import time.
_NOTIFICATION_TYPE = "cw-operator-event"

# MCP types — deferred-import inside functions per project convention
# (avoids hard dep at module load; mcp is an optional extra)


def _extract_payload(session_msg: Any) -> dict[str, Any] | None:
    """Extract event dict from upstream SSE msg.

    Returns None for non-operator-event messages (type guard, not error).
    """
    from mcp.types import JSONRPCNotification

    root = session_msg.message.root
    if not isinstance(root, JSONRPCNotification):
        return None
    params = root.params or {}
    data = params.get("data")
    if data is None:
        return None
    if data.get("notification_type") != _NOTIFICATION_TYPE:
        return None
    raw = data.get("message")
    if raw is None:
        return None
    try:
        return cast("dict[str, Any]", json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None


def _build_meta(data: dict[str, Any]) -> dict[str, str]:
    """Build the meta dict for notifications/claude/channel params."""
    meta: dict[str, str] = {
        "event_type": str(data.get("event", "")),
        "correlation_id": str(data.get("correlation_id") or ""),
        "client": str(data.get("client") or ""),
    }
    return {k: v for k, v in meta.items() if v}


def _build_outbound_notification(data: dict[str, Any]) -> Any:
    """Build a SessionMessage to emit on the stdio MCP connection."""
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    return SessionMessage(
        message=JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method="notifications/claude/channel",
                params={"content": json.dumps(data), "meta": _build_meta(data)},
            )
        )
    )


async def _relay_upstream(
    sse_read: Any,
    stdio_write: Any,
    client_id: str | None = None,
) -> None:
    """Read from SSE stream, write operator-event notifications to stdio."""
    async for item in sse_read:
        if isinstance(item, Exception):
            logger.debug("sse read error: %s", item)
            continue
        payload = _extract_payload(item)
        if payload is None:
            continue
        # Why: pr.registered's payload has no "client" key (a pre-existing
        # producer gap this ticket is the first to surface as a forwarding
        # gap; fixing the producer is out of scope -- #1002). Always relay it
        # even when this proxy is scoped to one client via --client-id: a
        # scoped operator silently missing PR registrations is worse than
        # rare cross-client noise on this one low-volume event type.
        if payload.get("event") == "pr.registered":
            outbound = _build_outbound_notification(payload)
            await stdio_write.send(outbound)
            continue
        if client_id and payload.get("client") != client_id:
            continue
        outbound = _build_outbound_notification(payload)
        await stdio_write.send(outbound)


def run_proxy(client_id: str | None = None) -> None:
    """Start the stdio MCP proxy. Blocks until the SSE connection closes."""
    import anyio
    from mcp.client.sse import sse_client
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    base_url = os.environ.get("CW_OPERATOR_EVENTS_BASE_URL", _DEFAULT_BASE_URL)
    # filter_client: the cw client name to filter events by (None = relay all)
    filter_client = client_id or os.environ.get("CW_OPERATOR_EVENTS_CLIENT_ID")
    # cursor_id: the SSE subscription identity for replay cursor tracking
    cursor_id = filter_client or socket.gethostname()
    sse_url = f"{base_url}/sse/operator/?client_id={urllib.parse.quote(cursor_id)}"

    mcp_server: Server = Server("cw-operator")
    init_options = mcp_server.create_initialization_options(
        experimental_capabilities={"claude/channel": {}},
    )

    async def _main() -> None:
        async with (
            sse_client(sse_url) as (sse_read, _sse_write),
            stdio_server() as (stdio_read, stdio_write),
            anyio.create_task_group() as tg,
        ):
            tg.start_soon(mcp_server.run, stdio_read, stdio_write, init_options)
            tg.start_soon(_relay_upstream, sse_read, stdio_write, filter_client)

    anyio.run(_main)


if __name__ == "__main__":
    run_proxy()
