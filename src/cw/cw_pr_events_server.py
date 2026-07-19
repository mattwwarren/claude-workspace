"""PR channel server: MCP notifications to subscribed Claude sessions on PR events."""

from __future__ import annotations

import functools
import json
import logging
import os
import queue
import urllib.parse
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from cw._util import MCP_EXTRA_MSG
from cw.event_bus import EventBus
from cw.pr_events_auth import (
    CW_PR_EVENTS_HMAC_SECRET_ENV,
    SIGNATURE_HEADER,
    verify_signature,
    warn_if_unsigned_mode,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8788
DEFAULT_HOST = "127.0.0.1"
_VALID_EVENT_TYPES = frozenset(
    {"ci_failed", "review_received", "mergeable", "merged", "review_requested"}
)
_NOTIFICATION_TYPE = "cw-pr-event"
_EVENTS_FILE = "channel-events.jsonl"
_CURSORS_FILE = "channel-cursors.json"


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


# The generic append/subscribe/broadcast/cursor machinery now lives in
# EventBus (src/cw/event_bus.py, #1303). We bind the historic underscored
# module-level names to the bus's PUBLIC attributes/methods so every existing
# import site and every test that pokes _lock / _cursors / _event_offset in
# place keeps working unchanged. These are plain public-attribute reads (the
# bus side is not underscore-prefixed), so they do not trip ruff's SLF001.
_bus = EventBus(
    events_file=_EVENTS_FILE,
    cursors_file=_CURSORS_FILE,
    log_label="pr-events",
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


def _handle_review_requested_sync(
    *, repo: str, pr_number: int, payload: dict[str, Any]
) -> tuple[bool, str]:
    """Resolve identity + register a review_requested webhook (RFC 0011 S2).

    Runs entirely inside the ``anyio.to_thread.run_sync`` offload boundary
    because ``cached_gh_login()`` may shell out to ``gh api user`` (a blocking
    subprocess) and ``register_watched_pr`` takes a blocking ``fcntl.flock``.
    Returns ``(registered, reason)`` for the JSON response body.

    Payload contract (adopted #2): ``{"reviewer": {<node>},
    "requester_login": "<optional>"}`` — one reviewer node per delivery,
    mirroring one element of ``gh pr view --json reviewRequests``.
    """
    from cw.operator_identity import cached_gh_login
    from cw.pr_hydrate import resolve_and_register_review_request

    # Why: bypass resolve_operator_login's operator_github_login override — no
    # client context exists at this PR-scoped webhook entry point, so there is
    # no ClientConfig to consult. Honoring the override here is blocked on the
    # repo->client mapping and lands via follow-up #1171.
    operator_login = cached_gh_login()
    reviewer = payload.get("reviewer")
    reviewer_nodes = [reviewer] if isinstance(reviewer, dict) else []
    requester = payload.get("requester_login")
    requester_login = requester if isinstance(requester, str) else None
    pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    return resolve_and_register_review_request(
        repo=repo,
        pr_number=pr_number,
        pr_url=pr_url,
        reviewer_nodes=reviewer_nodes,
        operator_login=operator_login,
        source="webhook",
        requester_login=requester_login,
    )


async def handle_post_pr_event(request: Request) -> Response:
    """Handle POST /pr-event (#930): authenticate, validate, broadcast, observe.

    When ``CW_PR_EVENTS_HMAC_SECRET`` is set, requires a valid
    ``X-Cw-Signature`` header (401 otherwise) -- the endpoint is otherwise
    unauthenticated open internet-facing input via a relay tunnel. When the
    secret is unset, the endpoint default-denies with 401 (#1127) unless the
    app was built with ``allow_unsigned=True`` (``cw pr-channel serve
    --allow-unsigned``) -- a secret, when configured, is ALWAYS enforced
    regardless of ``allow_unsigned``; the flag only relaxes the "no secret
    configured" branch. Validates the JSON body (unchanged 400 contract),
    broadcasts the MCP notification (unchanged, additive), then routes the
    event through ``cw.pr_hydrate.observe_pushed_event`` -- offloaded onto a
    worker thread via ``anyio.to_thread.run_sync`` since ``dev_queue_lock``
    is a blocking ``fcntl.flock``, not asyncio-aware.
    """
    from starlette.responses import JSONResponse

    raw_body = await request.body()
    secret = os.environ.get(CW_PR_EVENTS_HMAC_SECRET_ENV)
    allow_unsigned = getattr(request.app.state, "allow_unsigned", False)
    if secret:
        if not verify_signature(
            raw_body,
            header_value=request.headers.get(SIGNATURE_HEADER),
            secret=secret,
        ):
            logger.warning("pr-event rejected: invalid or missing signature")
            return JSONResponse({"error": "invalid signature"}, status_code=401)
    elif not allow_unsigned:
        logger.warning(
            "pr-event rejected: no HMAC secret configured and --allow-unsigned not set"
        )
        return JSONResponse({"error": "unsigned requests not allowed"}, status_code=401)

    try:
        body = json.loads(raw_body)
        event = PREventRequest.model_validate(body)
    except (ValueError, TypeError) as exc:
        logger.warning("pr-event rejected: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=400)

    notification = _build_notification(event)
    broadcast(notification)

    import anyio

    if event.event_type == "review_requested":
        registered, reason = await anyio.to_thread.run_sync(
            functools.partial(
                _handle_review_requested_sync,
                repo=event.repo,
                pr_number=event.pr_number,
                payload=event.payload,
            )
        )
        return JSONResponse({"registered": registered, "reason": reason})

    from cw.pr_hydrate import observe_pushed_event

    await anyio.to_thread.run_sync(
        functools.partial(
            observe_pushed_event,
            repo=event.repo,
            pr_number=event.pr_number,
            wire_event_type=event.event_type,
            payload=event.payload,
        )
    )
    return JSONResponse({"status": "ok"})


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


def make_app(*, allow_unsigned: bool = False) -> Starlette:
    """Build and return the Starlette ASGI app with MCP SSE + /pr-event route.

    ``allow_unsigned`` (#1127) is stashed on ``app.state`` and read by
    ``handle_post_pr_event`` -- it only relaxes the "no secret configured"
    branch of that handler; a configured ``CW_PR_EVENTS_HMAC_SECRET`` is
    always enforced regardless of this flag.
    """
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
                            if not await _send_or_close(write_stream, session_msg):
                                logger.debug("drain: peer stream closed, exiting")
                                return

                    tg.start_soon(_drain)
            finally:
                unsubscribe(q)

    app = Starlette(
        routes=[
            Mount("/sse", app=_sse_asgi),
            Mount("/messages", app=sse.handle_post_message),
            Route("/pr-event", handle_post_pr_event, methods=["POST"]),
            Route("/ack", handle_post_ack, methods=["POST"]),
        ],
        middleware=[Middleware(_SSESlashMiddleware)],
    )
    app.router.redirect_slashes = False
    app.state.allow_unsigned = allow_unsigned
    return app


def serve(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, allow_unsigned: bool = False
) -> None:
    """Start the server. Blocks until interrupted.

    Why: warn_if_unsigned_mode() is called here, not from make_app(), so that
    tests and callers constructing the app directly (TestClient(make_app()))
    don't spam the unsigned-mode log on every app construction (#930).

    ``allow_unsigned`` (#1127) threads through to both -- it is the CLI-flag
    opt-in (``cw pr-channel serve --allow-unsigned``) that restores the old
    open behavior when ``CW_PR_EVENTS_HMAC_SECRET`` is unset.
    """
    import uvicorn

    warn_if_unsigned_mode(allow_unsigned=allow_unsigned)
    uvicorn.run(make_app(allow_unsigned=allow_unsigned), host=host, port=port)


if __name__ == "__main__":
    _port = int(os.environ.get("CW_PR_EVENTS_PORT", str(DEFAULT_PORT)))
    serve(port=_port)
