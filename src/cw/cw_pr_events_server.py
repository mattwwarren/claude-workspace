"""PR channel server: MCP notifications to subscribed Claude sessions on PR events."""

from __future__ import annotations

import functools
import json
import logging
import os
import queue
import urllib.parse
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cw._util import MCP_EXTRA_MSG
from cw.event_bus import AckRequest, EventBus
from cw.gh import github_pr_url
from cw.models.pr_channel_event import (
    PREventKind,
    PREventMessage,
    PREventType,
    build_pr_event_message,
    kind_for,
    normalize_event_time,
    normalize_head_sha,
    normalize_source_id,
)
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
_ACCEPTED_EVENT_TYPES = sorted(e.value for e in PREventType)
_NOTIFICATION_TYPE = "cw-pr-event"
_EVENTS_FILE = "channel-events.jsonl"
_CURSORS_FILE = "channel-cursors.json"


class PREventRequest(BaseModel):
    """Inbound ``POST /pr-event`` body.

    Tolerant by design: ``extra="ignore"`` so version skew is not a 400. The
    optional identity fields accept any JSON type; wrong types are dropped to
    null with a warning in ``_build_message``, never rejected.
    """

    model_config = ConfigDict(extra="ignore")

    repo: str
    pr_number: int
    event_type: PREventType
    payload: dict[str, Any] = Field(default_factory=dict)
    source_id: Any = None
    head_sha: Any = None
    event_time: Any = None


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


def _normalize_identity_fields(event: PREventRequest) -> dict[str, Any]:
    """Normalize the optional identity fields; log each supplied-but-invalid one once.

    An absent (``None``) value is silent: it is the normal v1 workflow case.
    """
    normalized: dict[str, Any] = {}
    checks = (
        ("source_id", event.source_id, normalize_source_id),
        ("head_sha", event.head_sha, normalize_head_sha),
        ("event_time", event.event_time, normalize_event_time),
    )
    for field, raw, normalize in checks:
        value, reason = normalize(raw)
        if reason is not None:
            logger.warning(
                "pr-event %s dropped (%s): repo=%r pr_number=%d "
                "event_type=%s source_id=%r",
                field,
                reason,
                event.repo,
                event.pr_number,
                event.event_type.value,
                event.source_id if isinstance(event.source_id, str) else None,
            )
        normalized[field] = value
    return normalized


def _build_message(event: PREventRequest) -> PREventMessage:
    """Build the v1 ``PREventMessage`` for an accepted event (one normalize pass)."""
    fields = _normalize_identity_fields(event)
    return build_pr_event_message(
        repo=event.repo,
        pr_number=event.pr_number,
        event_type=event.event_type,
        payload=event.payload,
        source_id=fields["source_id"],
        head_sha=fields["head_sha"],
        event_time=fields["event_time"],
        received_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )


def _wrap_envelope(message: PREventMessage) -> dict[str, Any]:
    """Wrap an already-built message in the unchanged durable envelope."""
    return {
        "notification_type": _NOTIFICATION_TYPE,
        "message": json.dumps(message.model_dump(mode="json")),
        "title": (
            f"PR #{message.pr_number}: {message.event_type.value.replace('_', ' ')}"
        ),
    }


def _build_notification(event: PREventRequest) -> dict[str, Any]:
    return _wrap_envelope(_build_message(event))


def _unknown_event_type_body(raw_body: object) -> dict[str, Any]:
    """Build the 400 body for an unknown ``event_type`` (R9); logs the rejection."""
    raw_type = raw_body.get("event_type") if isinstance(raw_body, dict) else None
    raw_repo = raw_body.get("repo") if isinstance(raw_body, dict) else None
    raw_pr = raw_body.get("pr_number") if isinstance(raw_body, dict) else None
    accepted = ", ".join(_ACCEPTED_EVENT_TYPES)
    logger.warning(
        "pr-event rejected: unsupported event_type %r (repo=%r pr_number=%r); "
        "accepted: %s",
        raw_type,
        raw_repo,
        raw_pr,
        accepted,
    )
    return {
        "error": f"event_type must be one of {_ACCEPTED_EVENT_TYPES}",
        "accepted_event_types": list(_ACCEPTED_EVENT_TYPES),
    }


def _parse_pr_event(
    raw_body: bytes,
) -> tuple[PREventRequest | None, dict[str, Any] | None]:
    """Parse the body. Returns ``(event, None)`` or ``(None, 400 body)``."""
    try:
        body = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        logger.warning("pr-event rejected: %s", exc)
        return None, {"error": str(exc)}
    try:
        return PREventRequest.model_validate(body), None
    except ValidationError as exc:
        if any(err.get("loc") == ("event_type",) for err in exc.errors()):
            return None, _unknown_event_type_body(body)
        logger.warning("pr-event rejected: %s", exc)
        return None, {"error": str(exc)}


def _authenticate(request: Request, raw_body: bytes) -> Response | None:
    """Return a 401 response when the request fails auth, else None."""
    from starlette.responses import JSONResponse

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
        return None
    if not allow_unsigned:
        logger.warning(
            "pr-event rejected: no HMAC secret configured and --allow-unsigned not set"
        )
        return JSONResponse({"error": "unsigned requests not allowed"}, status_code=401)
    return None


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

    Honors ``OrchestratorConfig.operator_github_login_by_repo`` (RFC 0011
    follow-up #1171) via ``resolve_operator_login_for_repo``. The config load
    is wrapped fail-open (deliberate choice, not a copy of the CLI call
    site's unguarded shape): this webhook is a shared, multi-repo/
    multi-client path with no caller-side exception handler
    (``handle_post_pr_event`` only catches ``(ValueError, TypeError)``), so a
    malformed ``orchestrator.yaml`` must not crash every delivery -- it falls
    back to the process gh identity alone, mirroring
    ``cw_queue_events_server.py._run_poller``'s established fail-open pattern
    for config-load failures on shared server paths (RFC 0008 W3, #1002).
    """
    from cw.config import load_orchestrator_config
    from cw.exceptions import ConfigValidationError
    from cw.operator_identity import cached_gh_login, resolve_operator_login_for_repo
    from cw.pr_hydrate import resolve_and_register_review_request

    self_login = cached_gh_login()
    try:
        config = load_orchestrator_config()
    except ConfigValidationError:
        logger.exception(
            "orchestrator config load failed for review_requested webhook,"
            " falling back to process gh identity"
        )
        operator_login = self_login
    else:
        operator_login = resolve_operator_login_for_repo(
            repo, config, fallback=self_login
        )
    reviewer = payload.get("reviewer")
    reviewer_nodes = [reviewer] if isinstance(reviewer, dict) else []
    requester = payload.get("requester_login")
    requester_login = requester if isinstance(requester, str) else None
    pr_url = github_pr_url(repo, pr_number)
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
    denied = _authenticate(request, raw_body)
    if denied is not None:
        return denied

    event, error_body = _parse_pr_event(raw_body)
    if event is None:
        return JSONResponse(error_body or {"error": "invalid body"}, status_code=400)

    message = _build_message(event)
    broadcast(_wrap_envelope(message))
    logger.info(
        "pr-event accepted: repo=%r pr_number=%d event_type=%s kind=%s "
        "source_id=%r event_id=%s",
        event.repo,
        event.pr_number,
        event.event_type.value,
        message.kind,
        message.source_id,
        message.event_id,
    )

    import anyio

    if kind_for(event.event_type) is PREventKind.HINT:
        logger.debug(
            "pr-event kind=hint broadcast only: repo=%r pr_number=%d "
            "event_type=%s source_id=%r",
            event.repo,
            event.pr_number,
            event.event_type.value,
            message.source_id,
        )
        return JSONResponse({"status": "ok"})

    if event.event_type is PREventType.REVIEW_REQUESTED:
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
            wire_event_type=event.event_type.value,
            payload=event.payload,
        )
    )
    return JSONResponse({"status": "ok"})


async def handle_post_ack(request: Request) -> Response:
    """Handle POST /ack: record the subscriber's declared durable prefix.

    The cursor is the next offset to read. A duplicate or regressing ack is a
    200 no-op returning the effective cursor; an invalid request or an offset
    beyond the next-offset frontier is a 400 with state unchanged (#3172).
    """
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
        req = AckRequest.model_validate(body)
        cursor = ack_offset(req.client_id, req.offset)
    except (ValueError, TypeError) as exc:
        msg = str(exc)
        logger.warning("ack rejected: %s", msg)
        return JSONResponse({"error": msg}, status_code=400)
    return JSONResponse({"status": "ok", "cursor": cursor})


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

    from cw._events_channel_base import build_server_notification
    from cw._sse_util import _send_or_close

    mcp_server: Server[None] = Server("cw-pr-events")
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
                            session_msg = build_server_notification(
                                "cw-pr-events", notification
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
