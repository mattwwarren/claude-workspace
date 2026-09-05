"""Shared MCP stdio channel-proxy base for cw's SSE-to-Claude event proxies.

Extracted from cw_queue_events_channel.py, cw_pr_events_channel.py, and
cw_operator_events_channel.py (#1305), which had ~90% identical
payload-extraction / notification-building / relay / proxy-lifecycle logic.
Two constraints from the original three modules are preserved here:

1. Every ``mcp.*``/``anyio`` import stays function-local (never hoisted to
   module top level) so this module -- like its predecessors -- has no hard
   dependency on the optional ``[mcp]`` extra at import time.
2. This module has no reference whatsoever to cw_queue_events_server,
   cw_pr_events_server, or cw_operator_events (avoids triggering their
   module-level I/O at import time) and no dependency on any other cw.*
   domain model -- each shim keeps its own literal constants and, where
   needed (operator's OrchestratorEventType), its own domain imports.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import socket
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelProxyConfig:
    """Per-channel wiring for the shared stdio MCP proxy.

    ``build_meta`` and ``always_relay`` are pure in-process callables, never
    loaded from YAML/JSON/CLI input and never serialized -- hence a frozen
    dataclass rather than the repo's pydantic ``*Config`` idiom (matches the
    existing frozen-dataclass config carriers in doctor.py/dispatch.py).
    """

    server_name: str
    default_base_url: str
    base_url_env: str
    client_id_env: str
    sse_path: str
    notification_type: str
    build_meta: Callable[[dict[str, Any]], dict[str, str]]
    filter_by_client: bool = True
    always_relay: Callable[[dict[str, Any]], bool] | None = None
    instructions: str = ""


def extract_payload(session_msg: Any, notification_type: str) -> dict[str, Any] | None:
    """Extract the event dict from an upstream SSE msg.

    Returns None for messages that aren't a matching-typed event (type
    guard, not error).
    """
    from mcp.types import JSONRPCNotification

    message = session_msg.message
    if not isinstance(message, JSONRPCNotification):
        return None
    params = message.params or {}
    data = params.get("data")
    if data is None:
        return None
    if data.get("notification_type") != notification_type:
        return None
    raw = data.get("message")
    if raw is None:
        return None
    try:
        return cast("dict[str, Any]", json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return None


def build_outbound_notification(
    data: dict[str, Any], build_meta: Callable[[dict[str, Any]], dict[str, str]]
) -> Any:
    """Build a SessionMessage to emit on the stdio MCP connection."""
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCNotification

    return SessionMessage(
        message=JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={"content": json.dumps(data), "meta": build_meta(data)},
        )
    )


async def relay_upstream(
    sse_read: Any,
    stdio_write: Any,
    client_id: str | None = None,
    *,
    config: ChannelProxyConfig,
) -> None:
    """Read from SSE stream, write matching-type notifications to stdio."""
    async for item in sse_read:
        if isinstance(item, Exception):
            logger.debug("sse read error: %s", item)
            continue
        payload = extract_payload(item, config.notification_type)
        if payload is None:
            continue
        if config.always_relay is not None and config.always_relay(payload):
            outbound = build_outbound_notification(payload, config.build_meta)
            await stdio_write.send(outbound)
            continue
        # Proxy-side client filter: skip events for other clients
        if config.filter_by_client and client_id and payload.get("client") != client_id:
            continue
        outbound = build_outbound_notification(payload, config.build_meta)
        await stdio_write.send(outbound)


def run_proxy(client_id: str | None = None, *, config: ChannelProxyConfig) -> None:
    """Start the stdio MCP proxy. Blocks until the SSE connection closes."""
    import anyio
    from mcp.client.sse import sse_client
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    base_url = os.environ.get(config.base_url_env, config.default_base_url)
    # filter_client: the cw client name to filter events by (None = relay all)
    filter_client = client_id or os.environ.get(config.client_id_env)
    # cursor_id: the SSE subscription identity for replay cursor tracking
    cursor_id = filter_client or socket.gethostname()
    sse_url = f"{base_url}{config.sse_path}?client_id={urllib.parse.quote(cursor_id)}"

    mcp_server: Server = Server(config.server_name)
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
            tg.start_soon(
                functools.partial(relay_upstream, config=config),
                sse_read,
                stdio_write,
                filter_client,
            )

    anyio.run(_main)
