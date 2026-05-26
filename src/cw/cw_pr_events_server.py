"""PR channel server: MCP notifications to subscribed Claude sessions on PR events."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from cw.config import state_dir

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8788
DEFAULT_HOST = "127.0.0.1"
_VALID_EVENT_TYPES = frozenset({"ci_failed", "review_received", "mergeable", "merged"})
_NOTIFICATION_TYPE = "cw-pr-event"


class PREventRequest(BaseModel):
    repo: str
    pr_number: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        msg = f"event_type must be one of {sorted(_VALID_EVENT_TYPES)}"
        if v not in _VALID_EVENT_TYPES:
            raise ValueError(msg)
        return v


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
    logger.info("pr-events subscriber added, total=%d", len(_subscribers))
    return q


def unsubscribe(q: queue.SimpleQueue[dict[str, Any]]) -> None:
    """Remove a subscriber queue."""
    with _lock, contextlib.suppress(ValueError):
        _subscribers.remove(q)
    logger.info("pr-events subscriber removed, total=%d", len(_subscribers))


def _append_event(notification: dict[str, Any]) -> None:
    """Persist notification to channel-events.jsonl with a monotonic offset."""
    with _file_lock:
        path = state_dir() / "channel-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {**notification, "offset": _event_offset[0]}
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
        _event_offset[0] += 1


def _read_events_from_offset(from_offset: int) -> list[dict[str, Any]]:
    """Read all events with offset >= from_offset from channel-events.jsonl."""
    with _file_lock:
        path = state_dir() / "channel-events.jsonl"
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
                    "channel-events.jsonl: skipping malformed line: %r", stripped
                )
                continue
            if record.get("offset", -1) >= from_offset:
                results.append(record)
        return results


def _load_cursors() -> dict[str, int]:
    """Load per-subscriber cursors from disk."""
    with _file_lock:
        path = state_dir() / "channel-cursors.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                logger.warning("channel-cursors.json: unexpected shape, ignoring")
                return {}
            return {str(k): int(v) for k, v in data.items()}
        except json.JSONDecodeError:
            logger.warning("channel-cursors.json: corrupt, ignoring")
            return {}


def _load_offset_from_file() -> int:
    """Determine current offset from channel-events.jsonl on disk."""
    with _file_lock:
        path = state_dir() / "channel-events.jsonl"
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
        path = state_dir() / "channel-cursors.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_cursors))


def broadcast(notification: dict[str, Any]) -> None:
    """Fan-out notification to all subscriber queues."""
    _append_event(notification)  # FIRST: persist (file_lock only)
    with _lock:  # THEN: get subscribers
        subs = list(_subscribers)
    for s in subs:
        s.put_nowait(notification)
    logger.debug("pr-events broadcast to %d subscribers", len(subs))


def _build_notification(event: PREventRequest) -> dict[str, Any]:
    return {
        "notification_type": _NOTIFICATION_TYPE,
        "message": json.dumps(
            {
                "repo": event.repo,
                "pr_number": event.pr_number,
                "event_type": event.event_type,
                "payload": event.payload,
            }
        ),
        "title": f"PR #{event.pr_number}: {event.event_type.replace('_', ' ')}",
    }


async def handle_post_pr_event(request: Request) -> Response:
    """Handle POST /pr-event: validate JSON body and broadcast MCP notification."""
    try:
        body = await request.json()
        event = PREventRequest.model_validate(body)
    except (ValueError, TypeError) as exc:
        logger.warning("pr-event rejected: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=400)
    notification = _build_notification(event)
    broadcast(notification)
    return JSONResponse({"status": "ok"})


async def handle_post_ack(request: Request) -> Response:
    """Handle POST /ack: advance per-subscriber cursor."""
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


def make_app() -> Starlette:
    """Build and return the Starlette ASGI app with MCP SSE + /pr-event route."""
    import anyio  # noqa: PLC0415
    from mcp.server import Server  # noqa: PLC0415
    from mcp.server.sse import SseServerTransport  # noqa: PLC0415
    from mcp.shared.message import SessionMessage  # noqa: PLC0415
    from mcp.types import JSONRPCMessage, JSONRPCNotification  # noqa: PLC0415

    mcp_server: Server[None, Any] = Server("cw-pr-events")
    sse = SseServerTransport("/messages")

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
                                    "logger": "cw-pr-events",
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

    return Starlette(
        routes=[
            Mount("/sse", app=_sse_asgi),
            Mount("/messages", app=sse.handle_post_message),
            Route("/pr-event", handle_post_pr_event, methods=["POST"]),
            Route("/ack", handle_post_ack, methods=["POST"]),
        ]
    )


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Start the server. Blocks until interrupted."""
    import uvicorn  # noqa: PLC0415

    uvicorn.run(make_app(), host=host, port=port)


if __name__ == "__main__":
    _port = int(os.environ.get("CW_PR_EVENTS_PORT", str(DEFAULT_PORT)))
    serve(port=_port)
