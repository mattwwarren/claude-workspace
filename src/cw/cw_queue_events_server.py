"""Queue channel server: MCP notifications to subscribed Claude sessions."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from cw.atomic import atomic_write_text
from cw.config import state_dir

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

_MCP_EXTRA_MSG = (
    "channel server requires [mcp] extra; "
    "run 'uv pip install cw[mcp]' or 'uv sync --extra mcp'"
)

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
    logger.info("queue-events subscriber added, total=%d", len(_subscribers))
    return q


def unsubscribe(q: queue.SimpleQueue[dict[str, Any]]) -> None:
    """Remove a subscriber queue."""
    with _lock, contextlib.suppress(ValueError):
        _subscribers.remove(q)
    logger.info("queue-events subscriber removed, total=%d", len(_subscribers))


def _append_event(notification: dict[str, Any]) -> None:
    """Persist notification to queue-channel-events.jsonl with a monotonic offset."""
    with _file_lock:
        path = state_dir() / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {**notification, "offset": _event_offset[0]}
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
        _event_offset[0] += 1


def _read_events_from_offset(from_offset: int) -> list[dict[str, Any]]:
    """Read all events with offset >= from_offset from queue-channel-events.jsonl."""
    with _file_lock:
        path = state_dir() / _EVENTS_FILE
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
                    "%s: skipping malformed line: %r", _EVENTS_FILE, stripped
                )
                continue
            if record.get("offset", -1) >= from_offset:
                results.append(record)
        return results


def _load_cursors() -> dict[str, int]:
    """Load per-subscriber cursors from disk."""
    with _file_lock:
        path = state_dir() / _CURSORS_FILE
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                logger.warning("%s: unexpected shape, ignoring", _CURSORS_FILE)
                return {}
            return {str(k): int(v) for k, v in data.items()}
        except json.JSONDecodeError:
            logger.warning("%s: corrupt, ignoring", _CURSORS_FILE)
            return {}


def _load_offset_from_file() -> int:
    """Determine current offset from queue-channel-events.jsonl on disk."""
    with _file_lock:
        path = state_dir() / _EVENTS_FILE
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
    """Subscribe and replay any missed events since the client's last cursor."""
    q = subscribe()  # acquires+releases _lock (FIRST)
    with _file_lock:  # acquires+releases _file_lock (AFTER)
        cursor = _cursors.get(client_id, 0)
    missed = _read_events_from_offset(cursor)
    for record in missed:
        q.put_nowait(record)
    return q


def ack_offset(client_id: str, offset: int) -> None:
    """Advance the per-subscriber cursor and persist to disk."""
    with _file_lock:
        _cursors[client_id] = offset
        path = state_dir() / _CURSORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(_cursors))


def broadcast(notification: dict[str, Any]) -> None:
    """Fan-out notification to all subscriber queues."""
    _append_event(notification)  # FIRST: persist (file_lock only)
    with _lock:  # THEN: get subscribers
        subs = list(_subscribers)
    for s in subs:
        s.put_nowait(notification)
    logger.debug("queue-events broadcast to %d subscribers", len(subs))


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


def _get_sentinel_status(session_id: str | None, state: Any) -> str | None:
    if session_id is None:
        return None
    for session in state.sessions:
        if session.id == session_id:
            lr = session.last_result
            if lr and isinstance(lr, dict):
                return lr.get("status")
            return None
    return None


def _get_blocker_reason(session_id: str | None, state: Any) -> str | None:
    if session_id is None:
        return None
    for session in state.sessions:
        if session.id == session_id:
            lr = session.last_result
            if lr and isinstance(lr, dict):
                blocker = lr.get("blocker")
                if blocker and isinstance(blocker, dict):
                    return blocker.get("reason")
            return None
    return None


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
        elif prev == "pending" and curr == "running":
            events.append(
                {
                    "event": "queue.ticket_claimed",
                    "ticket_id": tid,
                    "client": task.client,
                    "session_id": task.session_id,
                }
            )
        elif prev == "running" and curr == "completed":
            sentinel_status = _get_sentinel_status(task.session_id, state)
            events.append(
                {
                    "event": "queue.ticket_completed",
                    "ticket_id": tid,
                    "client": task.client,
                    "queue_status": "completed",
                    "sentinel_status": sentinel_status,
                }
            )
        elif prev == "running" and curr == "failed":
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
        if prev == "active" and curr == "idle":
            events.append(
                {
                    "event": "queue.session_idled",
                    "session_id": session.id,
                    "session_name": session.name,
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


def _check_wedge() -> None:
    """Check for wedged tasks. TODO: implement when doctor-drift ships."""
    return


def _poll_once(old: QueueSnapshot) -> tuple[QueueSnapshot, list[dict[str, Any]]]:
    """Load current state, compute deltas, return new snapshot + events."""
    from cw.config import load_state  # noqa: PLC0415
    from cw.dev_queue import load_dev_queue  # noqa: PLC0415

    store = load_dev_queue()
    state = load_state()

    events: list[dict[str, Any]] = []
    events.extend(_compute_queue_deltas(old, store, state))
    events.extend(_compute_session_deltas(old, state))

    new_snap = QueueSnapshot(
        task_statuses={t.ticket_id: str(t.status) for t in store.tasks},
        task_session_ids={t.ticket_id: t.session_id for t in store.tasks},
        session_statuses={s.id: str(s.status) for s in state.sessions},
    )
    return new_snap, events


def _run_poller() -> None:
    """Background polling thread. Runs as daemon."""
    import time  # noqa: PLC0415

    snapshot = _load_snapshot()
    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            new_snap, events = _poll_once(snapshot)
            for event in events:
                broadcast(_build_queue_notification(event))
            if events:
                _save_snapshot(new_snap)
                snapshot = new_snap
            elif (
                new_snap.task_statuses != snapshot.task_statuses
                or new_snap.session_statuses != snapshot.session_statuses
            ):
                # State changed but no events — update snapshot silently
                _save_snapshot(new_snap)
                snapshot = new_snap
        except Exception:
            logger.exception("poller error")


def _start_poller() -> None:
    t = threading.Thread(target=_run_poller, daemon=True, name="queue-events-poller")
    t.start()


async def handle_post_ack(request: Request) -> Response:
    """Handle POST /ack: advance per-subscriber cursor."""
    from starlette.responses import JSONResponse  # noqa: PLC0415

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
    """Rewrite bare /sse → /sse/ at ASGI scope level.

    Starlette's Mount("/sse") regex requires a trailing slash to produce a
    FULL match.  Without this rewrite, a GET /sse request falls through to
    the redirect_slashes handler and returns a 307.  We normalise the path
    internally so neither the client nor the router needs to care which form
    was used.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/sse":
            scope = dict(scope, path="/sse/")
        await self._app(scope, receive, send)


def make_app() -> Starlette:
    """Build and return the Starlette ASGI app with MCP SSE + /ack route."""
    try:
        from starlette.applications import Starlette  # noqa: PLC0415
        from starlette.middleware import Middleware  # noqa: PLC0415
        from starlette.routing import Mount, Route  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(_MCP_EXTRA_MSG) from exc
    import anyio  # noqa: PLC0415
    from mcp.server import Server  # noqa: PLC0415
    from mcp.server.sse import SseServerTransport  # noqa: PLC0415
    from mcp.shared.message import SessionMessage  # noqa: PLC0415
    from mcp.types import JSONRPCMessage, JSONRPCNotification  # noqa: PLC0415

    mcp_server: Server[None, Any] = Server("cw-queue-events")
    sse = SseServerTransport("/messages")

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
                            await write_stream.send(session_msg)

                    tg.start_soon(_drain)
            finally:
                unsubscribe(q)

    app = Starlette(
        routes=[
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
    import uvicorn  # noqa: PLC0415

    uvicorn.run(make_app(), host=host, port=port)


if __name__ == "__main__":
    _port = int(os.environ.get("CW_QUEUE_EVENTS_PORT", str(DEFAULT_PORT)))
    serve(port=_port)
