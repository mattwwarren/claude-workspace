"""Operator-attention channel: filters the orchestrator event bus down to a

cw-operator SSE topic on the existing queue-events server (RFC 0008 W3, #1002).

Bridges two previously-separate event streams: the orchestrator inbox
(``cw.events``, ``~/.local/share/cw/events/inbox.jsonl``) and the queue-events
SSE channel (``cw_queue_events_server``, its own durable-replay files). This
module owns its own subscriber registry and durable-replay cursor/offset
plumbing -- a full duplication of the ``cw_pr_events_server``/
``cw_queue_events_server`` pattern, not a shared abstraction (see the ticket's
pre-flight discovery: the two existing sibling channel servers never share
state either).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from cw.atomic import atomic_write_text
from cw.config import state_dir
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


_lock = threading.Lock()
_subscribers: list[queue.SimpleQueue[dict[str, Any]]] = []

_file_lock = threading.Lock()
_cursors: dict[str, int] = {}
_event_offset: list[int] = [0]


def subscribe() -> queue.SimpleQueue[dict[str, Any]]:
    """Register a subscriber queue and return it."""
    q: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
    with _lock:
        _subscribers.append(q)
    logger.info("operator-events subscriber added, total=%d", len(_subscribers))
    return q


def unsubscribe(q: queue.SimpleQueue[dict[str, Any]]) -> None:
    """Remove a subscriber queue."""
    with _lock, contextlib.suppress(ValueError):
        _subscribers.remove(q)
    logger.info("operator-events subscriber removed, total=%d", len(_subscribers))


def _append_event(notification: dict[str, Any]) -> None:
    """Persist notification to operator-channel-events.jsonl with a monotonic offset."""
    with _file_lock:
        path = state_dir() / _OPERATOR_EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {**notification, "offset": _event_offset[0]}
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _event_offset[0] += 1


def _read_events_from_offset_locked(from_offset: int) -> list[dict[str, Any]]:
    """Read events with offset >= from_offset. Caller MUST hold ``_file_lock``.

    The non-re-entrant read core. ``subscribe_with_cursor`` calls this while
    already holding ``_file_lock`` (the public wrapper below would deadlock —
    ``_file_lock`` is a plain, non-reentrant ``threading.Lock``).
    """
    path = state_dir() / _OPERATOR_EVENTS_FILE
    if not path.exists():
        return []
    results: list[dict[str, Any]] = []
    for raw_line in path.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            record: dict[str, Any] = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning(
                "%s: skipping malformed line: %r", _OPERATOR_EVENTS_FILE, stripped
            )
            continue
        if record.get("offset", -1) >= from_offset:
            results.append(record)
    return results


def _read_events_from_offset(from_offset: int) -> list[dict[str, Any]]:
    """Read all events with offset >= from_offset from operator-channel-events.jsonl."""
    with _file_lock:
        return _read_events_from_offset_locked(from_offset)


def _load_cursors() -> dict[str, int]:
    """Load per-subscriber cursors from disk."""
    with _file_lock:
        path = state_dir() / _OPERATOR_CURSORS_FILE
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                logger.warning("%s: unexpected shape, ignoring", _OPERATOR_CURSORS_FILE)
                return {}
            return {str(k): int(v) for k, v in data.items()}
        except json.JSONDecodeError:
            logger.warning("%s: corrupt, ignoring", _OPERATOR_CURSORS_FILE)
            return {}


def _load_offset_from_file() -> int:
    """Determine current offset from operator-channel-events.jsonl on disk."""
    with _file_lock:
        path = state_dir() / _OPERATOR_EVENTS_FILE
        if not path.exists():
            return 0
        max_offset = -1
        for raw_line in path.read_text().splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record: dict[str, Any] = json.loads(stripped)
                offset = record.get("offset", -1)
                if isinstance(offset, int) and offset > max_offset:
                    max_offset = offset
            except json.JSONDecodeError:
                continue
        return max_offset + 1


def subscribe_with_cursor(client_id: str) -> queue.SimpleQueue[dict[str, Any]]:
    """Subscribe and replay any missed events since the client's last cursor.

    Mirrors the TOCTOU fix in ``cw_queue_events_server`` (#433): register the
    subscriber AND read the replay backlog while holding ``_file_lock``, so no
    ``broadcast()`` can append+fan-out in the gap.
    """
    with _file_lock:
        cursor = _cursors.get(client_id, 0)
        q = subscribe()
        missed = _read_events_from_offset_locked(cursor)
        for record in missed:
            q.put_nowait(record)
    return q


def ack_offset(client_id: str, offset: int) -> None:
    """Advance the per-subscriber cursor and persist to disk."""
    with _file_lock:
        _cursors[client_id] = offset
        path = state_dir() / _OPERATOR_CURSORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(_cursors))


def broadcast(notification: dict[str, Any]) -> None:
    """Fan-out notification to all subscriber queues."""
    _append_event(notification)  # FIRST: persist (file_lock only)
    with _lock:  # THEN: get subscribers
        subs = list(_subscribers)
    for s in subs:
        s.put_nowait(notification)
    logger.debug("operator-events broadcast to %d subscribers", len(subs))


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
