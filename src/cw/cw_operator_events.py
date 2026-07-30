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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from cw.dev_queue.lifecycle import HOLD_DISPOSITIONS
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

    from cw.models import DevQueueStore, TicketTask

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


def _find_held_task(
    task_by_ticket: dict[tuple[str, str], TicketTask], event: OrchestratorEvent
) -> TicketTask | None:
    """Resolve the live ``TicketTask`` a ``session.needs_attention`` event is
    about, or ``None`` if it isn't about any known task (RFC 0011 A6, #1162).

    Joins via the event payload's ``ticket_id``/``client`` fields -- not
    ``payload["paused_status"]`` (a distinct, disjoint-namespace string; see
    ``AWAITING_OPERATOR_DISPOSITION``'s docstring). A fleet-wide event with no
    owning ticket (``ticket_id`` absent/None -- e.g. ``gh_availability_outage``)
    and a ticket_id with no matching row (resolved/deleted since) both
    correctly return None here, which the caller treats as "not held."
    """
    ticket_id = event.payload.get("ticket_id")
    client = event.payload.get("client")
    if not isinstance(ticket_id, str) or not isinstance(client, str):
        return None
    return task_by_ticket.get((ticket_id, client))


def _is_held(task: TicketTask) -> bool:
    """True iff *task* is parked in one of the digest-eligible hold classes."""
    return task.disposition in HOLD_DISPOSITIONS


def _in_delivery_window(config: OrchestratorConfig, now: datetime) -> bool:
    """True iff *now* falls inside the configured local-timezone digest
    delivery window (R12, #1162). DST-aware: resolves *now* into the
    configured IANA zone and compares local wall-clock hours, never a fixed
    UTC offset.
    """
    local_now = now.astimezone(ZoneInfo(config.attention_digest_window_tz))
    return (
        config.attention_digest_window_start_hour
        <= local_now.hour
        < config.attention_digest_window_end_hour
    )


def _digest_entry(task: TicketTask) -> dict[str, Any]:
    """Build one digest entry for *task* (the committed 3-key schema)."""
    return {
        "ticket_id": task.ticket_id,
        "client": task.client,
        "breadcrumbs": task.blocked_reason,
    }


def _collect_flushable_digest(
    store: DevQueueStore, config: OrchestratorConfig, now: datetime
) -> list[dict[str, Any]] | None:
    """Return the digest entries to flush now, or ``None`` if nothing should
    flush (RFC 0011 A6, #1162).

    Re-derives the held set live from ``store.tasks`` at call time (R9) --
    never from stored events -- so a ticket resolved/deleted since it was
    buffered is excluded automatically (its
    ``attention_digest_buffered_at`` marker was already cleared by
    ``transition_task_status``'s unconditional-clear block). Returns ``None``
    when the delivery window is closed, nothing is currently buffered, or the
    most recently buffered arrival hasn't yet aged past the idle-drain floor
    -- the debounce is anchored to the NEWEST arrival, not the oldest, so a
    fresh held park arriving mid-batch pushes the flush back out and gives it
    a chance to land in the same digest (R5). On a real flush, clears
    ``attention_digest_buffered_at`` on every included task -- the caller is
    responsible for persisting the mutated *store*.
    """
    if not _in_delivery_window(config, now):
        return None
    buffered: list[TicketTask] = []
    arrivals: list[datetime] = []
    for task in store.tasks:
        arrival = task.attention_digest_buffered_at
        if arrival is not None and _is_held(task):
            buffered.append(task)
            arrivals.append(arrival)
    if not buffered:
        return None
    idle_elapsed = (now - max(arrivals)).total_seconds()
    if idle_elapsed < config.attention_digest_idle_floor_seconds:
        return None
    entries = [_digest_entry(task) for task in buffered]
    for task in buffered:
        task.attention_digest_buffered_at = None
    return entries


def _build_operator_digest_notification(
    entries: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    """Build the SSE notification envelope for a flushed digest (#1162).

    Sibling to ``_build_operator_notification``, but sources its inner
    ``message`` from a live entries list rather than a stored
    ``OrchestratorEvent`` -- there is no such event (R9: ephemeral, never
    persisted), so ``id``/``correlation_id`` are deliberately omitted rather
    than attributed to any one ticket.
    """
    message = {
        "event": "session.needs_attention.digest",
        "digest": True,
        "count": len(entries),
        "created_at": now.isoformat(),
        "entries": entries,
    }
    title = f"Operator: {len(entries)} ticket(s) awaiting operator attention"
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

    RFC 0011 A6 (#1162): an admitted ``session.needs_attention`` event whose
    ticket is currently held (``HOLD_DISPOSITIONS``) is buffered onto that
    task's ``attention_digest_buffered_at`` instead of forwarded immediately;
    every other admitted event (including a non-held/ticketless
    ``session.needs_attention``) forwards exactly as before, unbatched. A
    single ``dev_queue_lock()`` scope per tick covers both the per-event
    classify/buffer step and the post-loop flush check -- runs even on a tick
    with zero new events, since the flush condition (delivery window +
    idle-drain floor) is time-based, not event-triggered; the cursor is only
    advanced when there were events to advance past.
    """
    from cw.dev_queue.storage import dev_queue_lock, load_dev_queue, save_dev_queue
    from cw.events import advance_cursor, read_events

    forward = config.operator_channel_forward
    events = read_events(
        consumer=_OPERATOR_BRIDGE_CONSUMER,
        event_types=list(forward.event_types),
    )
    now = datetime.now(UTC)
    changed = False
    with dev_queue_lock():
        store = load_dev_queue()
        if events:
            task_by_ticket = {
                (task.ticket_id, task.client): task for task in store.tasks
            }
            for event in events:
                if not _admits(event, forward):
                    continue
                if event.type == OrchestratorEventType.SESSION_NEEDS_ATTENTION:
                    held_task = _find_held_task(task_by_ticket, event)
                    if held_task is not None and _is_held(held_task):
                        if held_task.attention_digest_buffered_at is None:
                            held_task.attention_digest_buffered_at = now
                            changed = True
                        continue
                broadcast(_build_operator_notification(event))
        entries = _collect_flushable_digest(store, config, now)
        if entries is not None:
            broadcast(_build_operator_digest_notification(entries, now))
            changed = True
        if changed:
            save_dev_queue(store)
    if events:
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
