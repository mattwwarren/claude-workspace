"""Tests for cw_pr_events_channel: payload extraction, notification building, relay."""

from __future__ import annotations

import json
import socket
import urllib.parse
from typing import Any

import pytest
from click.testing import CliRunner

starlette = pytest.importorskip(
    "starlette", reason="requires mcp extras: pip install 'cw[mcp]'"
)

from mcp.shared.message import SessionMessage  # noqa: E402
from mcp.types import JSONRPCMessage, JSONRPCNotification, JSONRPCResponse  # noqa: E402

from cw.cw_pr_events_channel import (  # noqa: E402
    _DEFAULT_BASE_URL,
    _NOTIFICATION_TYPE,
    _build_outbound_notification,
    _extract_payload,
    _relay_upstream,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_message(
    repo: str,
    pr_number: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> SessionMessage:
    """Build a SessionMessage matching the server's _build_notification + SSE format."""
    inner = json.dumps(
        {
            "repo": repo,
            "pr_number": pr_number,
            "event_type": event_type,
            "payload": payload or {},
        }
    )
    notif = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/message",
        params={
            "level": "info",
            "logger": "cw-pr-events",
            "data": {
                "notification_type": _NOTIFICATION_TYPE,
                "message": inner,
                "title": f"PR #{pr_number}: {event_type}",
            },
        },
    )
    return SessionMessage(message=JSONRPCMessage(notif))


# ---------------------------------------------------------------------------
# _extract_payload
# ---------------------------------------------------------------------------


class TestExtractPayload:
    def test_valid_cw_pr_event(self) -> None:
        msg = _make_session_message("owner/repo", 42, "ci_failed", {"key": "val"})
        result = _extract_payload(msg)
        assert result is not None
        assert result["repo"] == "owner/repo"
        assert result["pr_number"] == 42
        assert result["event_type"] == "ci_failed"
        assert result["payload"] == {"key": "val"}

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
                "logger": "cw-pr-events",
                "data": {
                    "notification_type": "not-a-pr-event",
                    "message": json.dumps({"repo": "x", "pr_number": 1}),
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
                "logger": "cw-pr-events",
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
# _build_outbound_notification
# ---------------------------------------------------------------------------


class TestBuildOutboundNotification:
    def _data(self) -> dict[str, Any]:
        return {"repo": "owner/repo", "pr_number": 5, "event_type": "merged"}

    def test_returns_session_message(self) -> None:
        result = _build_outbound_notification(self._data())
        assert isinstance(result, SessionMessage)

    def test_root_is_json_rpc_notification(self) -> None:
        result = _build_outbound_notification(self._data())
        assert isinstance(result.message.root, JSONRPCNotification)

    def test_method_is_notifications_message(self) -> None:
        result = _build_outbound_notification(self._data())
        assert result.message.root.method == "notifications/message"

    def test_data_preserved(self) -> None:
        data = self._data()
        result = _build_outbound_notification(data)
        params = result.message.root.params or {}
        assert params["data"] == data


# ---------------------------------------------------------------------------
# _relay_upstream
# ---------------------------------------------------------------------------


class TestRelayUpstream:
    def test_valid_message_forwarded(self) -> None:
        import anyio

        msg = _make_session_message("owner/repo", 10, "review_received")

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
                return await recv_out.receive()
            except anyio.EndOfStream:
                return None

        result = anyio.run(_run)
        assert result is not None
        assert isinstance(result, SessionMessage)
        root = result.message.root
        assert isinstance(root, JSONRPCNotification)
        params = root.params or {}
        assert params["data"]["repo"] == "owner/repo"

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

    def test_non_pr_event_skipped(self) -> None:
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
# run_proxy: env var / URL construction
#
# Strategy: patch anyio.run to call a real async runner that intercepts
# sse_client at the mcp.client.sse module level before the deferred import.
# ---------------------------------------------------------------------------


def _build_expected_sse_url(base: str, client_id: str) -> str:
    return f"{base}/sse?client_id={urllib.parse.quote(client_id)}"


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

        from cw.cw_pr_events_channel import run_proxy

        captured: list[str] = []

        class _StopEarly(Exception):  # noqa: N818
            pass

        @asynccontextmanager  # type: ignore[arg-type]
        async def _fake_sse_client(url: str, **_kw: Any) -> Any:  # type: ignore[misc]
            captured.append(url)
            raise _StopEarly
            yield  # type: ignore[misc]

        monkeypatch.setattr(_sse_mod, "sse_client", _fake_sse_client)

        with contextlib.suppress(_StopEarly, RuntimeError):
            run_proxy(client_id=client_id)

        return captured[0] if captured else ""

    def test_default_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CW_PR_EVENTS_BASE_URL", raising=False)
        monkeypatch.delenv("CW_PR_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        assert url.startswith(f"{_DEFAULT_BASE_URL}/sse")

    def test_custom_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CW_PR_EVENTS_BASE_URL", "http://example.com:9999")
        monkeypatch.delenv("CW_PR_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        assert url.startswith("http://example.com:9999/sse")

    def test_custom_client_id_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CW_PR_EVENTS_BASE_URL", raising=False)
        monkeypatch.setenv("CW_PR_EVENTS_CLIENT_ID", "my-custom-id")
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == ["my-custom-id"]

    def test_client_id_param_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CW_PR_EVENTS_BASE_URL", raising=False)
        monkeypatch.setenv("CW_PR_EVENTS_CLIENT_ID", "env-id")
        url = self._invoke_proxy_capture_url(monkeypatch, client_id="explicit-id")
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == ["explicit-id"]

    def test_default_client_id_is_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CW_PR_EVENTS_BASE_URL", raising=False)
        monkeypatch.delenv("CW_PR_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == [socket.gethostname()]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_pr_channel_proxy_help(self) -> None:
        from cw.cli import main

        result = CliRunner().invoke(main, ["pr-channel", "proxy", "--help"])
        assert result.exit_code == 0
        assert "--client-id" in result.output
