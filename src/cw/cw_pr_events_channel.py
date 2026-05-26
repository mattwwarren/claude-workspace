"""MCP stdio proxy: forwards cw-pr-events SSE notifications to Claude."""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.parse
from typing import Any, cast

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8788"
_NOTIFICATION_TYPE = "cw-pr-event"

# MCP types — deferred-import inside functions per project convention
# (avoids hard dep at module load; mcp is an optional extra)


def _extract_payload(session_msg: Any) -> dict[str, Any] | None:
    """Extract {repo, pr_number, event_type, payload} from upstream SSE msg.

    Returns None for non-PR-event messages (type guard, not error).
    """
    from mcp.types import JSONRPCNotification  # noqa: PLC0415

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


def _build_outbound_notification(data: dict[str, Any]) -> Any:
    """Build a SessionMessage to emit on the stdio MCP connection."""
    from mcp.shared.message import SessionMessage  # noqa: PLC0415
    from mcp.types import JSONRPCMessage, JSONRPCNotification  # noqa: PLC0415

    return SessionMessage(
        message=JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method="notifications/message",
                params={
                    "level": "info",
                    "logger": "cw-pr-events",
                    "data": data,
                },
            )
        )
    )


async def _relay_upstream(sse_read: Any, stdio_write: Any) -> None:
    """Read from SSE stream, write PR-event notifications to stdio."""
    async for item in sse_read:
        if isinstance(item, Exception):
            logger.debug("sse read error: %s", item)
            continue
        payload = _extract_payload(item)
        if payload is None:
            continue
        outbound = _build_outbound_notification(payload)
        await stdio_write.send(outbound)


def run_proxy(client_id: str | None = None) -> None:
    """Start the stdio MCP proxy. Blocks until the SSE connection closes."""
    import anyio  # noqa: PLC0415
    from mcp.client.sse import sse_client  # noqa: PLC0415
    from mcp.server.stdio import stdio_server  # noqa: PLC0415

    base_url = os.environ.get("CW_PR_EVENTS_BASE_URL", _DEFAULT_BASE_URL)
    if client_id is None:
        client_id = os.environ.get("CW_PR_EVENTS_CLIENT_ID", socket.gethostname())
    sse_url = f"{base_url}/sse?client_id={urllib.parse.quote(client_id)}"

    async def _main() -> None:
        async with (
            sse_client(sse_url) as (sse_read, _sse_write),
            stdio_server() as (_stdio_read, stdio_write),
        ):
            await _relay_upstream(sse_read, stdio_write)

    anyio.run(_main)


if __name__ == "__main__":
    run_proxy()
