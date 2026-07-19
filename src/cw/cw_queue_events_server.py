"""Queue channel server: MCP notifications to subscribed Claude sessions."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from cw._util import MCP_EXTRA_MSG
from cw.atomic import atomic_write_text
from cw.config import state_dir
from cw.event_bus import EventBus
from cw.models import OrchestratorConfig, QueueItemStatus, SessionStatus

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8789
DEFAULT_HOST = "127.0.0.1"
_NOTIFICATION_TYPE = "cw-queue-event"
POLL_INTERVAL_SECONDS = 2.0
_EVENTS_FILE = "queue-channel-events.jsonl"
_CURSORS_FILE = "queue-channel-cursors.json"
_SNAPSHOT_FILE = "channels/queue-events.snapshot.json"


class QueueSnapshot(BaseModel):
    task_statuses: dict[str, str] = Field(default_factory=dict)
    task_session_ids: dict[str, str | None] = Field(default_factory=dict)
    session_statuses: dict[str, str] = Field(default_factory=dict)
    # Tracks the last-seen reap_reason per session so the poller can fire
    # queue.session_reaped exactly once when reconcile stamps a new reason.
    session_reap_reasons: dict[str, str] = Field(default_factory=dict)


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
    events_file=_EVENTS_FILE,
    cursors_file=_CURSORS_FILE,
    log_label="queue-events",
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
_read_events_from_offset_locked = _bus.read_events_from_offset_locked
subscribe_with_cursor = _bus.subscribe_with_cursor
ack_offset = _bus.ack_offset
_load_cursors = _bus.load_cursors
_load_offset_from_file = _bus.load_offset_from_file


def _load_snapshot() -> QueueSnapshot:
    path = state_dir() / _SNAPSHOT_FILE
    if not path.exists():
        return QueueSnapshot()
    try:
        return QueueSnapshot.model_validate_json(path.read_text())
    except (json.JSONDecodeError, ValueError):
        logger.warning("queue-events snapshot: corrupt or invalid, resetting")
        return QueueSnapshot()


def _save_snapshot(snap: QueueSnapshot) -> None:
    path = state_dir() / _SNAPSHOT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, snap.model_dump_json())


def _get_last_result(session_id: str | None, state: Any) -> dict[str, Any] | None:
    """Return last_result dict for the session matching session_id, or None."""
    if session_id is None:
        return None
    for session in state.sessions:
        if session.id == session_id:
            lr = session.last_result
            return lr if isinstance(lr, dict) else None
    return None


def _get_sentinel_status(session_id: str | None, state: Any) -> str | None:
    lr = _get_last_result(session_id, state)
    return lr.get("status") if lr is not None else None


def _get_blocker_reason(session_id: str | None, state: Any) -> str | None:
    lr = _get_last_result(session_id, state)
    if lr is None:
        return None
    blocker = lr.get("blocker")
    return blocker.get("reason") if isinstance(blocker, dict) else None


def _compute_queue_deltas(
    old: QueueSnapshot,
    store: Any,
    state: Any,
) -> list[dict[str, Any]]:
    """Compute queue event dicts for changed TicketTask states."""
    events: list[dict[str, Any]] = []
    for task in store.tasks:
        tid = task.ticket_id
        prev = old.task_statuses.get(tid)
        curr = str(task.status)
        if prev is None:
            events.append(
                {
                    "event": "queue.ticket_enqueued",
                    "ticket_id": tid,
                    "client": task.client,
                    "status": curr,
                }
            )
        elif prev == QueueItemStatus.PENDING and curr == QueueItemStatus.RUNNING:
            events.append(
                {
                    "event": "queue.ticket_claimed",
                    "ticket_id": tid,
                    "client": task.client,
                    "session_id": task.session_id,
                }
            )
        elif prev == QueueItemStatus.RUNNING and curr == QueueItemStatus.COMPLETED:
            sentinel_status = _get_sentinel_status(task.session_id, state)
            events.append(
                {
                    "event": "queue.ticket_completed",
                    "ticket_id": tid,
                    "client": task.client,
                    "queue_status": QueueItemStatus.COMPLETED,
                    "sentinel_status": sentinel_status,
                }
            )
        elif prev == QueueItemStatus.RUNNING and curr == QueueItemStatus.FAILED:
            error = _get_blocker_reason(task.session_id, state)
            events.append(
                {
                    "event": "queue.ticket_failed",
                    "ticket_id": tid,
                    "client": task.client,
                    "error": error,
                    "attempts": task.attempts,
                }
            )
        # RUNNING→CANCELLED: NO EVENT (system-driven cleanup)
        # RUNNING→BLOCKED_ON_USER: NO EVENT (transient state)
    return events


def _compute_session_deltas(
    old: QueueSnapshot,
    state: Any,
) -> list[dict[str, Any]]:
    """Compute session event dicts for changed session states."""
    events: list[dict[str, Any]] = []
    for session in state.sessions:
        prev = old.session_statuses.get(session.id)
        curr = str(session.status)
        if prev == SessionStatus.ACTIVE and curr == SessionStatus.IDLE:
            events.append(
                {
                    "event": "queue.session_idled",
                    "session_id": session.id,
                    "session_name": session.name,
                }
            )
        # Emit queue.session_reaped when reconcile stamps a new reap_reason
        # (first non-None appearance). The reason is persisted on Session so
        # the poller can read it off the state snapshot rather than needing
        # out-of-band signalling. See GitHub #380.
        reap_reason = session.reap_reason
        if reap_reason is not None:
            prev_reason = old.session_reap_reasons.get(session.id)
            if prev_reason is None:
                events.append(
                    {
                        "event": "queue.session_reaped",
                        "session_id": session.id,
                        "surface_ref": session.surface_ref,
                        "origin": str(session.origin),
                        "reason": str(reap_reason),
                        "from_status": prev
                        if prev is not None
                        else str(session.status),
                        "to_status": curr,
                    }
                )
    return events


def _build_queue_notification(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("event", "unknown")
    ticket_id = event.get("ticket_id", "")
    title = f"Queue: {event_type.replace('queue.', '').replace('_', ' ')}"
    if ticket_id:
        title = f"{ticket_id}: {title}"
    return {
        "notification_type": _NOTIFICATION_TYPE,
        "message": json.dumps(event),
        "title": title,
    }


def _check_wedge() -> dict[str, Any] | None:
    """Check for wedged tasks. Returns wedge-event dict if detected, else None.

    TODO: implement when doctor-drift ships.
    """
    return None


def _poll_once(old: QueueSnapshot) -> tuple[QueueSnapshot, list[dict[str, Any]]]:
    """Load current state, compute deltas, return new snapshot + events."""
    from cw.config import load_state
    from cw.dev_queue import load_dev_queue

    store = load_dev_queue()
    state = load_state()

    events: list[dict[str, Any]] = []
    events.extend(_compute_queue_deltas(old, store, state))
    events.extend(_compute_session_deltas(old, state))

    # Wire wedge-detection hook — always called, but currently a stub
    wedge = _check_wedge()
    if wedge is not None:
        events.append(wedge)

    new_snap = QueueSnapshot(
        task_statuses={t.ticket_id: str(t.status) for t in store.tasks},
        task_session_ids={t.ticket_id: t.session_id for t in store.tasks},
        session_statuses={s.id: str(s.status) for s in state.sessions},
        session_reap_reasons={
            s.id: str(s.reap_reason)
            for s in state.sessions
            if s.reap_reason is not None
        },
    )
    return new_snap, events


def _poller_tick(config: OrchestratorConfig) -> None:
    """Run one poll tick: broadcast queue.* deltas, then bridge to the operator channel.

    The operator-bridge call sits in its OWN try/except, OUTSIDE ``_file_lock``
    and after the queue.* broadcast loop above, so a bug in the bridge can
    never block or suppress the queue.* broadcasts that already fired
    (RFC 0008 W3, #1002). Logs ``"operator-bridge error"`` -- distinct from
    this function's own caller's ``"poller error"`` message -- so the two
    failure modes are distinguishable in logs.
    """
    with _file_lock:
        # Guard load→compute→save with _file_lock: prevents concurrent poll
        # cycles from reading the same stale snapshot and emitting duplicate
        # queue.* events (#433 fix 4).
        current_snap = _load_snapshot()
        new_snap, events = _poll_once(current_snap)
        if events or (
            new_snap.task_statuses != current_snap.task_statuses
            or new_snap.session_statuses != current_snap.session_statuses
        ):
            _save_snapshot(new_snap)
    for event in events:
        broadcast(_build_queue_notification(event))

    try:
        from cw.cw_operator_events import poll_and_forward_operator_channel

        poll_and_forward_operator_channel(config)
    except Exception:
        logger.exception("operator-bridge error")


def _run_poller() -> None:
    """Background polling thread. Runs as daemon."""
    import time

    from cw.config import load_orchestrator_config

    last_good_config: OrchestratorConfig | None = None
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            last_good_config = load_orchestrator_config()
        except Exception:
            # Why: a config reload failure must NEVER starve queue.* broadcasting
            # (RFC 0008 W3, #1002, binding decision #1) -- fall back to the last
            # config that loaded successfully (or safe defaults, pre-_start_poller)
            # so _poller_tick below still runs this tick regardless.
            logger.exception("orchestrator config reload failed, using last-known-good")
        config = (
            last_good_config if last_good_config is not None else OrchestratorConfig()
        )
        try:
            _poller_tick(config)
        except Exception:
            logger.exception("poller error")


_poller_started: list[bool] = [False]


def _start_poller() -> None:
    from cw.config import load_orchestrator_config

    # Fail-loud config validation (RFC 0008 W3, #1002): re-validated on EVERY
    # call, including calls after the poller thread is already running -- the
    # guard below only prevents re-spawning the thread, not re-validation. A
    # malformed operator_channel_forward must crash `cw queue-channel serve`
    # at startup rather than silently under-forward.
    load_orchestrator_config()
    with _lock:
        if _poller_started[0]:
            return
        _poller_started[0] = True
    t = threading.Thread(target=_run_poller, daemon=True, name="queue-events-poller")
    t.start()


async def handle_post_ack(request: Request) -> Response:
    """Handle POST /ack: advance per-subscriber cursor."""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
        req = AckRequest.model_validate(body)
    except (ValueError, TypeError) as exc:
        msg = str(exc)
        logger.warning("ack rejected: %s", msg)
        return JSONResponse({"error": msg}, status_code=400)
    ack_offset(req.client_id, req.offset)
    return JSONResponse({"status": "ok"})


_cursors.update(_load_cursors())
_event_offset[0] = _load_offset_from_file()


class _SSESlashMiddleware:
    """Rewrite bare mount paths -> their slash-suffixed forms at ASGI scope level.

    Starlette's Mount(...) regex requires a trailing slash to produce a FULL
    match. Without this rewrite, a bare GET /sse/operator (no trailing slash)
    doesn't match Mount("/sse/operator", ...)'s own regex either -- it falls
    through to the NEXT route in the list, Mount("/sse", ...), whose regex
    happily matches "/sse" + remaining "/operator". That's a silent
    prefix-swallow into the WRONG channel, not a clean 404 (RFC 0008 W3,
    #1002) -- exactly the risk binding decision #4 called "the highest-risk
    detail." /messages/operator has the identical exposure against
    Mount("/messages", ...). We normalise the path internally so neither the
    client nor the router needs to care which form was used.
    """

    _BARE_PATHS = ("/sse", "/sse/operator", "/messages/operator")

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") in self._BARE_PATHS:
            scope = dict(scope, path=scope["path"] + "/")
        await self._app(scope, receive, send)


def make_app() -> Starlette:
    """Build and return the Starlette ASGI app with MCP SSE + /ack route."""
    try:
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Mount, Route
    except ImportError as exc:
        raise ImportError(MCP_EXTRA_MSG) from exc
    import anyio
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    from cw._sse_util import _send_or_close
    from cw.cw_operator_events import build_operator_routes

    mcp_server: Server[None, Any] = Server("cw-queue-events")
    sse = SseServerTransport("/messages")

    operator_routes = build_operator_routes()

    _start_poller()

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
                                    "logger": "cw-queue-events",
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

    app = Starlette(
        routes=[
            # Operator routes MUST be prepended: Starlette resolves routes by
            # first match, and Mount("/sse", ...) below would otherwise
            # prefix-swallow /sse/operator (RFC 0008 W3, #1002).
            *operator_routes,
            Mount("/sse", app=_sse_asgi),
            Mount("/messages", app=sse.handle_post_message),
            Route("/ack", handle_post_ack, methods=["POST"]),
        ],
        middleware=[Middleware(_SSESlashMiddleware)],
    )
    app.router.redirect_slashes = False
    return app


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Start the server. Blocks until interrupted."""
    import uvicorn

    uvicorn.run(make_app(), host=host, port=port)


if __name__ == "__main__":
    _port = int(os.environ.get("CW_QUEUE_EVENTS_PORT", str(DEFAULT_PORT)))
    serve(port=_port)
