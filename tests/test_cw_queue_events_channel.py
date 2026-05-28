"""Tests for cw_queue_events_channel: payload extraction, notification building."""

from __future__ import annotations

import json
import socket
import urllib.parse
from typing import Any, cast

import pytest
from click.testing import CliRunner

starlette = pytest.importorskip(
    "starlette", reason="requires mcp extras: pip install 'cw[mcp]'"
)

from cw.cw_queue_events_channel import (  # noqa: E402
    _DEFAULT_BASE_URL,
    _NOTIFICATION_TYPE,
    _build_meta,
    _build_outbound_notification,
    _extract_payload,
    _relay_upstream,
)
from mcp.shared.message import SessionMessage  # noqa: E402
from mcp.types import JSONRPCMessage, JSONRPCNotification, JSONRPCResponse  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue_session_message(
    ticket_id: str,
    client: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> SessionMessage:
    """Build a server-shaped SessionMessage for testing the proxy."""
    extra: dict[str, Any] = payload or {}
    data = {
        "notification_type": "cw-queue-event",
        "message": json.dumps(
            {"event": event_type, "ticket_id": ticket_id, "client": client, **extra}
        ),
        "title": f"{ticket_id}: queue {event_type}",
    }
    return SessionMessage(
        message=JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method="notifications/message",
                params={"level": "info", "logger": "cw-queue-events", "data": data},
            )
        )
    )


# ---------------------------------------------------------------------------
# TestExtractPayload
# ---------------------------------------------------------------------------


class TestExtractPayload:
    def test_valid_cw_queue_event(self) -> None:
        msg = _make_queue_session_message("T-1", "acme", "queue.ticket_enqueued")
        result = _extract_payload(msg)
        assert result is not None
        assert result["event"] == "queue.ticket_enqueued"
        assert result["ticket_id"] == "T-1"
        assert result["client"] == "acme"

    def test_non_notification_root(self) -> None:
        response = JSONRPCResponse(jsonrpc="2.0", id=1, result={})
        msg = SessionMessage(message=JSONRPCMessage(response))
        assert _extract_payload(msg) is None

    def test_notification_type_mismatch(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={
                "level": "info",
                "logger": "cw-queue-events",
                "data": {
                    "notification_type": "not-a-queue-event",
                    "message": json.dumps({"event": "queue.ticket_enqueued"}),
                },
            },
        )
        msg = SessionMessage(message=JSONRPCMessage(notif))
        assert _extract_payload(msg) is None

    def test_missing_data_key(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={"level": "info"},
        )
        msg = SessionMessage(message=JSONRPCMessage(notif))
        assert _extract_payload(msg) is None

    def test_malformed_json_in_message(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={
                "level": "info",
                "logger": "cw-queue-events",
                "data": {
                    "notification_type": _NOTIFICATION_TYPE,
                    "message": "not-json",
                },
            },
        )
        msg = SessionMessage(message=JSONRPCMessage(notif))
        assert _extract_payload(msg) is None

    def test_missing_message_key(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={
                "level": "info",
                "data": {
                    "notification_type": _NOTIFICATION_TYPE,
                },
            },
        )
        msg = SessionMessage(message=JSONRPCMessage(notif))
        assert _extract_payload(msg) is None


# ---------------------------------------------------------------------------
# TestBuildMeta
# ---------------------------------------------------------------------------


class TestBuildMeta:
    def test_basic_fields(self) -> None:
        data = {"event": "queue.ticket_enqueued", "ticket_id": "T-1", "client": "acme"}
        meta = _build_meta(data)
        assert meta["event_type"] == "queue.ticket_enqueued"
        assert meta["ticket_id"] == "T-1"
        assert meta["client"] == "acme"

    def test_empty_values_omitted(self) -> None:
        data = {"event": "queue.session_idled", "ticket_id": "", "client": ""}
        meta = _build_meta(data)
        assert "ticket_id" not in meta
        assert "client" not in meta
        assert meta["event_type"] == "queue.session_idled"

    def test_uses_event_key_not_event_type_key(self) -> None:
        """event_type in meta comes from data["event"], not data["event_type"]."""
        data = {
            "event": "queue.ticket_claimed",
            "event_type": "should-be-ignored",
            "ticket_id": "T-2",
            "client": "acme",
        }
        meta = _build_meta(data)
        assert meta["event_type"] == "queue.ticket_claimed"

    def test_missing_optional_keys_omitted(self) -> None:
        data = {"event": "queue.session_idled"}
        meta = _build_meta(data)
        assert meta["event_type"] == "queue.session_idled"
        assert "ticket_id" not in meta
        assert "client" not in meta


# ---------------------------------------------------------------------------
# TestBuildOutboundNotification
# ---------------------------------------------------------------------------


class TestBuildOutboundNotification:
    def _data(self) -> dict[str, Any]:
        return {
            "event": "queue.ticket_claimed",
            "ticket_id": "T-5",
            "client": "acme",
            "session_id": "sess-abc",
        }

    def test_returns_session_message(self) -> None:
        result = _build_outbound_notification(self._data())
        assert isinstance(result, SessionMessage)

    def test_root_is_json_rpc_notification(self) -> None:
        result = _build_outbound_notification(self._data())
        assert isinstance(result.message.root, JSONRPCNotification)

    def test_method_is_notifications_claude_channel(self) -> None:
        result = _build_outbound_notification(self._data())
        assert result.message.root.method == "notifications/claude/channel"

    def test_data_preserved(self) -> None:
        data = self._data()
        result = _build_outbound_notification(data)
        params = result.message.root.params or {}
        assert json.loads(params["content"]) == data


# ---------------------------------------------------------------------------
# TestRelayUpstream
# ---------------------------------------------------------------------------


class TestRelayUpstream:
    def test_valid_message_forwarded(self) -> None:
        import anyio

        msg = _make_queue_session_message("T-10", "acme", "queue.ticket_enqueued")

        async def _run() -> SessionMessage | None:
            send_in, recv_in = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            send_out, recv_out = anyio.create_memory_object_stream[Any](
                max_buffer_size=5
            )
            await send_in.send(msg)
            await send_in.aclose()

            await _relay_upstream(recv_in, send_out)
            await send_out.aclose()

            try:
                return cast("SessionMessage", await recv_out.receive())
            except anyio.EndOfStream:
                return None

        result = anyio.run(_run)
        assert result is not None
        assert isinstance(result, SessionMessage)
        root = result.message.root
        assert isinstance(root, JSONRPCNotification)
        assert root.method == "notifications/claude/channel"
        params = root.params or {}
        assert json.loads(params["content"])["ticket_id"] == "T-10"

    def test_exception_skipped(self) -> None:
        import anyio

        exc = Exception("sse error")

        async def _run() -> int:
            send_in, recv_in = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            send_out, recv_out = anyio.create_memory_object_stream[Any](
                max_buffer_size=5
            )
            await send_in.send(exc)
            await send_in.aclose()

            await _relay_upstream(recv_in, send_out)
            await send_out.aclose()

            count = 0
            async for _ in recv_out:
                count += 1
            return count

        count = anyio.run(_run)
        assert count == 0

    def test_non_queue_event_skipped(self) -> None:
        import anyio

        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={"data": {"notification_type": "other"}},
        )
        msg = SessionMessage(message=JSONRPCMessage(notif))

        async def _run() -> int:
            send_in, recv_in = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            send_out, recv_out = anyio.create_memory_object_stream[Any](
                max_buffer_size=5
            )
            await send_in.send(msg)
            await send_in.aclose()

            await _relay_upstream(recv_in, send_out)
            await send_out.aclose()

            count = 0
            async for _ in recv_out:
                count += 1
            return count

        count = anyio.run(_run)
        assert count == 0


# ---------------------------------------------------------------------------
# TestRunProxyEnvVars
# ---------------------------------------------------------------------------


class TestRunProxyEnvVars:
    def _invoke_proxy_capture_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client_id: str | None = None,
    ) -> str:
        """Invoke run_proxy, capturing the SSE URL via sse_client patch."""
        import contextlib
        from contextlib import asynccontextmanager

        import mcp.client.sse as _sse_mod
        from cw.cw_queue_events_channel import run_proxy

        captured: list[str] = []

        class _StopEarly(Exception):  # noqa: N818
            pass

        @asynccontextmanager
        async def _fake_sse_client(url: str, **_kw: Any) -> Any:
            captured.append(url)
            raise _StopEarly
            yield

        monkeypatch.setattr(_sse_mod, "sse_client", _fake_sse_client)

        with contextlib.suppress(_StopEarly, RuntimeError):
            run_proxy(client_id=client_id)

        return captured[0] if captured else ""

    def test_default_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CW_QUEUE_EVENTS_BASE_URL", raising=False)
        monkeypatch.delenv("CW_QUEUE_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme + "://" + parsed.netloc == _DEFAULT_BASE_URL
        assert parsed.path == "/sse/"

    def test_custom_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CW_QUEUE_EVENTS_BASE_URL", "http://example.com:9999")
        monkeypatch.delenv("CW_QUEUE_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        assert parsed.netloc == "example.com:9999"
        assert parsed.path == "/sse/"

    def test_custom_client_id_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CW_QUEUE_EVENTS_BASE_URL", raising=False)
        monkeypatch.setenv("CW_QUEUE_EVENTS_CLIENT_ID", "my-custom-id")
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == ["my-custom-id"]

    def test_client_id_param_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CW_QUEUE_EVENTS_BASE_URL", raising=False)
        monkeypatch.setenv("CW_QUEUE_EVENTS_CLIENT_ID", "env-id")
        url = self._invoke_proxy_capture_url(monkeypatch, client_id="explicit-id")
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == ["explicit-id"]

    def test_default_client_id_is_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CW_QUEUE_EVENTS_BASE_URL", raising=False)
        monkeypatch.delenv("CW_QUEUE_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == [socket.gethostname()]

    def test_sse_url_uses_trailing_slash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SSE URL must use /sse/ to connect directly, not /sse which redirects."""
        monkeypatch.delenv("CW_QUEUE_EVENTS_BASE_URL", raising=False)
        monkeypatch.delenv("CW_QUEUE_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        assert parsed.path == "/sse/", f"Expected /sse/ path, got {parsed.path!r}"


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_queue_channel_proxy_help(self) -> None:
        from cw.cli import main

        result = CliRunner().invoke(main, ["queue-channel", "proxy", "--help"])
        assert result.exit_code == 0
        assert "--client-id" in result.output


# ---------------------------------------------------------------------------
# TestClientIdFilter
# ---------------------------------------------------------------------------


class TestClientIdFilter:
    def test_matching_client_id_relayed(self) -> None:
        import anyio

        msg = _make_queue_session_message("T-20", "acme", "queue.ticket_enqueued")

        async def _run() -> int:
            send_in, recv_in = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            send_out, recv_out = anyio.create_memory_object_stream[Any](
                max_buffer_size=5
            )
            await send_in.send(msg)
            await send_in.aclose()

            await _relay_upstream(recv_in, send_out, client_id="acme")
            await send_out.aclose()

            count = 0
            async for _ in recv_out:
                count += 1
            return count

        count = anyio.run(_run)
        assert count == 1

    def test_non_matching_client_id_suppressed(self) -> None:
        import anyio

        msg = _make_queue_session_message("T-21", "acme", "queue.ticket_enqueued")

        async def _run() -> int:
            send_in, recv_in = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            send_out, recv_out = anyio.create_memory_object_stream[Any](
                max_buffer_size=5
            )
            await send_in.send(msg)
            await send_in.aclose()

            await _relay_upstream(recv_in, send_out, client_id="other-client")
            await send_out.aclose()

            count = 0
            async for _ in recv_out:
                count += 1
            return count

        count = anyio.run(_run)
        assert count == 0

    def test_no_client_id_relays_all(self) -> None:
        import anyio

        msg1 = _make_queue_session_message("T-22", "acme", "queue.ticket_enqueued")
        msg2 = _make_queue_session_message("T-23", "other", "queue.ticket_claimed")

        async def _run() -> int:
            send_in, recv_in = anyio.create_memory_object_stream[Any](max_buffer_size=5)
            send_out, recv_out = anyio.create_memory_object_stream[Any](
                max_buffer_size=5
            )
            await send_in.send(msg1)
            await send_in.send(msg2)
            await send_in.aclose()

            await _relay_upstream(recv_in, send_out, client_id=None)
            await send_out.aclose()

            count = 0
            async for _ in recv_out:
                count += 1
            return count

        count = anyio.run(_run)
        assert count == 2
