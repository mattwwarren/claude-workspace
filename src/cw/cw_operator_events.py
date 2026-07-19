"""Operator-attention channel: filters the orchestrator event bus down to a

cw-operator SSE topic on the existing queue-events server (RFC 0008 W3, #1002).

Bridges two previously-separate event streams: the orchestrator inbox
(``cw.events``, ``~/.local/share/cw/events/inbox.jsonl``) and the queue-events
SSE channel (``cw_queue_events_server``, its own durable-replay files). This
module's append/subscribe/broadcast/cursor machinery is shared via
``EventBus`` (``cw.event_bus``, #1303), mirroring ``cw_queue_events_server``'s
and ``cw_pr_events_server``'s own migration onto the same core -- it still
owns its own subscriber registry and MCP route builder (``build_operator_routes``),
just backed by the shared bus rather than a hand-duplicated copy.
"""

from __future__ import annotations

import json
import logging
import queue
import urllib.parse
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from cw.event_bus import EventBus
from cw.models import (
    LivenessBucket,
    OperatorChannelForward,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

logger = logging.getLogger(__name__)

_NOTIFICATION_TYPE = "cw-operator-event"
_OPERATOR_BRIDGE_CONSUMER = "operator-channel-bridge"
_OPERATOR_EVENTS_FILE = "operator-channel-events.jsonl"
_OPERATOR_CURSORS_FILE = "operator-channel-cursors.json"

# LivenessBucket has no built-in ordering (closed StrEnum) -- this ladder lets
# _admits compare a session.liveness_changed event's new_bucket against the
# configured floor (OperatorChannelForward.liveness_min_bucket).
_LIVENESS_BUCKET_ORDER: list[LivenessBucket] = [
    LivenessBucket.LIVE,
    LivenessBucket.STALE_15M,
    LivenessBucket.STALE_30M,
    LivenessBucket.STALE_45M,
]


class AckRequest(BaseModel):
    client_id: str
    offset: int


# The generic append/subscribe/broadcast/cursor machinery now lives in
# EventBus (src/cw/event_bus.py, #1303). We bind the historic underscored
# module-level names to the bus's PUBLIC attributes/methods so every existing
# import site and every test that pokes _lock / _cursors / _event_offset in
# place keeps working unchanged. These are plain public-attribute reads (the
# bus side is not underscore-prefixed), so they do not trip ruff's SLF001.
_bus = EventBus(
    events_file=_OPERATOR_EVENTS_FILE,
    cursors_file=_OPERATOR_CURSORS_FILE,
    log_label="operator-events",
)

_lock = _bus.lock
_subscribers = _bus.subscribers
_file_lock = _bus.file_lock
_cursors = _bus.cursors
_event_offset = _bus.event_offset

subscribe = _bus.subscribe
unsubscribe = _bus.unsubscribe
broadcast = _bus.broadcast
_append_event = _bus.append_event
_read_events_from_offset = _bus.read_events_from_offset
subscribe_with_cursor = _bus.subscribe_with_cursor
ack_offset = _bus.ack_offset
_load_cursors = _bus.load_cursors
_load_offset_from_file = _bus.load_offset_from_file


def _admits(event: OrchestratorEvent, forward: OperatorChannelForward) -> bool:
    """Return True if *event* passes the operator-channel forward-set filter.

    ``event_types`` gates membership; task.transition and
    session.liveness_changed additionally apply their own sub-condition
    (new_status / new_bucket) on top of that gate. Every other admitted type
    forwards unconditionally once it is in ``event_types``.
    """
    if event.type not in forward.event_types:
        return False
    if event.type == OrchestratorEventType.TASK_TRANSITION:
        new_status = event.payload.get("new_status")
        return new_status in {s.value for s in forward.task_transition_statuses}
    if event.type == OrchestratorEventType.SESSION_LIVENESS_CHANGED:
        new_bucket = event.payload.get("new_bucket")
        if not isinstance(new_bucket, str):
            return False
        try:
            bucket = LivenessBucket(new_bucket)
        except ValueError:
            return False
        return _LIVENESS_BUCKET_ORDER.index(bucket) >= _LIVENESS_BUCKET_ORDER.index(
            forward.liveness_min_bucket
        )
    return True


def _build_operator_notification(event: OrchestratorEvent) -> dict[str, Any]:
    """Build the SSE notification envelope for an admitted operator event."""
    message = {
        "event": str(event.type),
        "id": event.id,
        "correlation_id": event.correlation_id,
        "created_at": event.created_at.isoformat(),
        **event.payload,
    }
    title = f"Operator: {str(event.type).replace('.', ' ')}"
    if event.correlation_id:
        title = f"{event.correlation_id}: {title}"
    return {
        "notification_type": _NOTIFICATION_TYPE,
        "message": json.dumps(message),
        "title": title,
    }


def poll_and_forward_operator_channel(config: OrchestratorConfig) -> None:
    """Read new orchestrator-bus events, filter, and forward to the operator channel.

    Called once per queue-events poller tick (``cw_queue_events_server._poller_tick``),
    outside ``_file_lock``, in its own try/except.  Advances the
    ``operator-channel-bridge`` consumer cursor past ALL read events regardless
    of whether each individually passed ``_admits`` -- the established
    orchestrator-bus consumer idiom (``dispatch.py``/``orchestrate.py``): a
    dropped event must never be re-scanned on the next tick.
    """
    from cw.events import advance_cursor, read_events

    forward = config.operator_channel_forward
    events = read_events(
        consumer=_OPERATOR_BRIDGE_CONSUMER,
        event_types=list(forward.event_types),
    )
    if not events:
        return
    for event in events:
        if _admits(event, forward):
            broadcast(_build_operator_notification(event))
    advance_cursor(_OPERATOR_BRIDGE_CONSUMER, events[-1].id)


async def handle_post_operator_ack(request: Request) -> Response:
    """Handle POST /operator/ack: advance the operator-channel per-subscriber cursor."""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
        req = AckRequest.model_validate(body)
    except (ValueError, TypeError) as exc:
        msg = str(exc)
        logger.warning("operator ack rejected: %s", msg)
        return JSONResponse({"error": msg}, status_code=400)
    ack_offset(req.client_id, req.offset)
    return JSONResponse({"status": "ok"})


_cursors.update(_load_cursors())
_event_offset[0] = _load_offset_from_file()


def build_operator_routes() -> list[BaseRoute]:
    """Build the three cw-operator Starlette routes for ``make_app()``.

    Instantiates its OWN MCP ``Server("cw-operator")`` + ``SseServerTransport``
    -- never reuses ``cw_queue_events_server``'s ``mcp_server``/``sse``
    instances, which would make agent-side ``<channel source="cw-operator">``
    injection silently resolve against the wrong server name. The returned
    routes must be PREPENDED before ``cw_queue_events_server.make_app()``'s
    existing three -- Starlette resolves routes by first match, and
    ``Mount("/sse", ...)`` would otherwise prefix-swallow ``/sse/operator``.
    See GitHub #1002.
    """
    import anyio
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification
    from starlette.routing import Mount, Route

    from cw._sse_util import _send_or_close

    mcp_server: Server[None, Any] = Server("cw-operator")
    sse = SseServerTransport("/messages/operator")

    async def _sse_asgi(  # pragma: no cover
        scope: Any, receive: Any, send: Any
    ) -> None:
        async with sse.connect_sse(scope, receive, send) as streams:
            read_stream, write_stream = streams
            query = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
            client_id_list = query.get("client_id", [])
            if client_id_list:
                q = subscribe_with_cursor(client_id_list[0])
            else:
                q = subscribe()
            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        mcp_server.run,
                        read_stream,
                        write_stream,
                        mcp_server.create_initialization_options(),
                    )

                    async def _drain() -> None:  # pragma: no cover
                        while True:
                            try:
                                notification = q.get_nowait()
                            except queue.Empty:
                                await anyio.sleep(0.05)
                                continue
                            json_rpc_notif = JSONRPCNotification(
                                jsonrpc="2.0",
                                method="notifications/message",
                                params={
                                    "level": "info",
                                    "logger": "cw-operator",
                                    "data": notification,
                                },
                            )
                            session_msg = SessionMessage(
                                message=JSONRPCMessage(json_rpc_notif)
                            )
                            if not await _send_or_close(write_stream, session_msg):
                                logger.debug("drain: peer stream closed, exiting")
                                return

                    tg.start_soon(_drain)
            finally:
                unsubscribe(q)

    return [
        Mount("/sse/operator", app=_sse_asgi),
        Mount("/messages/operator", app=sse.handle_post_message),
        Route("/operator/ack", handle_post_operator_ack, methods=["POST"]),
    ]
