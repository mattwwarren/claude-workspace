"""Tests for cw_pr_events_channel: payload extraction, notification building, relay."""

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

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCNotification, JSONRPCResponse

from cw.cw_pr_events_channel import (
    _DEFAULT_BASE_URL,
    _NOTIFICATION_TYPE,
    _build_meta,
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
    return SessionMessage(message=notif)


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
        msg = SessionMessage(message=response)
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
        msg = SessionMessage(message=notif)
        assert _extract_payload(msg) is None

    def test_missing_data_key(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={"level": "info"},
        )
        msg = SessionMessage(message=notif)
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
        msg = SessionMessage(message=notif)
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
        msg = SessionMessage(message=notif)
        assert _extract_payload(msg) is None


# ---------------------------------------------------------------------------
# _build_meta
# ---------------------------------------------------------------------------


class TestBuildMeta:
    def test_basic_fields(self) -> None:
        data = {"repo": "owner/repo", "pr_number": 42, "event_type": "ci_failed"}
        meta = _build_meta(data)
        assert meta["repo"] == "owner/repo"
        assert meta["pr_number"] == "42"
        assert meta["event_type"] == "ci_failed"

    def test_empty_values_omitted(self) -> None:
        # Empty string values must be filtered out; missing keys produce empty strings.
        data = {
            "repo": "",
            "event_type": "merged",
        }  # pr_number absent → str("") omitted
        meta = _build_meta(data)
        assert "repo" not in meta
        assert "pr_number" not in meta
        assert meta["event_type"] == "merged"

    def test_pr_number_as_string(self) -> None:
        data = {"repo": "r/r", "pr_number": 99, "event_type": "opened"}
        meta = _build_meta(data)
        assert isinstance(meta["pr_number"], str)
        assert meta["pr_number"] == "99"

    def test_role_from_payload(self) -> None:
        data = {
            "repo": "r/r",
            "pr_number": 1,
            "event_type": "review",
            "payload": {"role": "author"},
        }
        meta = _build_meta(data)
        assert meta["role"] == "author"

    def test_client_from_payload(self) -> None:
        data = {
            "repo": "r/r",
            "pr_number": 1,
            "event_type": "review",
            "payload": {"client": "acme"},
        }
        meta = _build_meta(data)
        assert meta["client"] == "acme"

    def test_missing_payload(self) -> None:
        data = {"repo": "r/r", "pr_number": 5, "event_type": "merged"}
        meta = _build_meta(data)
        assert "role" not in meta
        assert "client" not in meta
        assert meta["repo"] == "r/r"


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
        assert isinstance(result.message, JSONRPCNotification)

    def test_method_is_notifications_claude_channel(self) -> None:
        result = _build_outbound_notification(self._data())
        assert result.message.method == "notifications/claude/channel"

    def test_data_preserved(self) -> None:
        data = self._data()
        result = _build_outbound_notification(data)
        params = result.message.params or {}
        assert json.loads(params["content"]) == data


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
                return cast("SessionMessage", await recv_out.receive())
            except anyio.EndOfStream:
                return None

        result = anyio.run(_run)
        assert result is not None
        assert isinstance(result, SessionMessage)
        root = result.message
        assert isinstance(root, JSONRPCNotification)
        assert root.method == "notifications/claude/channel"
        params = root.params or {}
        assert json.loads(params["content"])["repo"] == "owner/repo"

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
        msg = SessionMessage(message=notif)

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
        monkeypatch.delenv("CW_PR_EVENTS_BASE_URL", raising=False)
        monkeypatch.delenv("CW_PR_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme + "://" + parsed.netloc == _DEFAULT_BASE_URL
        assert parsed.path == "/sse/"

    def test_custom_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CW_PR_EVENTS_BASE_URL", "http://example.com:9999")
        monkeypatch.delenv("CW_PR_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        assert parsed.netloc == "example.com:9999"
        assert parsed.path == "/sse/"

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

    def test_sse_url_uses_trailing_slash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SSE URL must use /sse/ to connect directly, not /sse which redirects."""
        monkeypatch.delenv("CW_PR_EVENTS_BASE_URL", raising=False)
        monkeypatch.delenv("CW_PR_EVENTS_CLIENT_ID", raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch)
        parsed = urllib.parse.urlparse(url)
        assert parsed.path == "/sse/", f"Expected /sse/ path, got {parsed.path!r}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_pr_channel_proxy_help(self) -> None:
        from cw.cli import main

        result = CliRunner().invoke(main, ["pr-channel", "proxy", "--help"])
        assert result.exit_code == 0
        assert "--client-id" in result.output
