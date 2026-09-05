"""Tests for _events_channel_base: shared MCP channel-proxy primitives.

Covers only what is genuinely new/shared and not already exercised end-to-end
by tests/test_cw_{queue,pr,operator}_events_channel.py: the generic
ChannelProxyConfig carrier, extract_payload/build_outbound_notification
parameterized on an arbitrary notification_type, relay_upstream's two
injectable hooks (filter_by_client, always_relay), and run_proxy's R1
(no `instructions=` kwarg) contract plus generic env-var/URL construction.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import urllib.parse
from typing import Any

import pytest

starlette = pytest.importorskip(
    "starlette", reason="requires mcp extras: pip install 'cw[mcp]'"
)

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCNotification, JSONRPCResponse

from cw._events_channel_base import (
    ChannelProxyConfig,
    build_outbound_notification,
    build_server_notification,
    extract_payload,
    relay_upstream,
    run_proxy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel_session_message(
    notification_type: str,
    message_dict: dict[str, Any],
    *,
    logger: str = "cw-events-channel-base",
    title: str = "test-notification",
) -> SessionMessage:
    """Build a server-shaped SessionMessage for the generic base functions.

    Deliberately not a shared conftest fixture -- see the plan's Test-helper
    inventory: this signature (bare notification_type + pre-built dict) has
    no per-module field contract, unlike the three existing per-module
    private builders which hardcode their own module's field names.
    """
    data = {
        "notification_type": notification_type,
        "message": json.dumps(message_dict),
        "title": title,
    }
    return SessionMessage(
        message=JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={"level": "info", "logger": logger, "data": data},
        )
    )


def _noop_build_meta(_data: dict[str, Any]) -> dict[str, str]:
    return {}


def _make_test_config(**overrides: Any) -> ChannelProxyConfig:
    base: dict[str, Any] = {
        "server_name": "events-base-test-server",
        "default_base_url": "http://127.0.0.1:9099",
        "base_url_env": "_EVENTS_BASE_TEST_BASE_URL",
        "client_id_env": "_EVENTS_BASE_TEST_CLIENT_ID",
        "sse_path": "/sse/events-base-test/",
        "notification_type": "events-base-test-event",
        "build_meta": _noop_build_meta,
    }
    base.update(overrides)
    return ChannelProxyConfig(**base)


# ---------------------------------------------------------------------------
# TestChannelProxyConfig
# ---------------------------------------------------------------------------


class TestChannelProxyConfig:
    def test_defaults(self) -> None:
        config = _make_test_config()
        assert config.filter_by_client is True
        assert config.always_relay is None
        assert config.instructions == ""

    def test_frozen(self) -> None:
        config = _make_test_config()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.server_name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestExtractPayload
# ---------------------------------------------------------------------------


class TestExtractPayload:
    NOTIFICATION_TYPE = "arbitrary-test-event"

    def test_valid_match(self) -> None:
        msg = _make_channel_session_message(self.NOTIFICATION_TYPE, {"foo": "bar"})
        result = extract_payload(msg, self.NOTIFICATION_TYPE)
        assert result == {"foo": "bar"}

    def test_non_notification_root(self) -> None:
        response = JSONRPCResponse(jsonrpc="2.0", id=1, result={})
        msg = SessionMessage(message=response)
        assert extract_payload(msg, self.NOTIFICATION_TYPE) is None

    def test_notification_type_mismatch(self) -> None:
        msg = _make_channel_session_message("some-other-type", {"foo": "bar"})
        assert extract_payload(msg, self.NOTIFICATION_TYPE) is None

    def test_missing_data_key(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={"level": "info"},
        )
        msg = SessionMessage(message=notif)
        assert extract_payload(msg, self.NOTIFICATION_TYPE) is None

    def test_malformed_json_in_message(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={
                "level": "info",
                "logger": "cw-events-channel-base",
                "data": {
                    "notification_type": self.NOTIFICATION_TYPE,
                    "message": "not-json",
                },
            },
        )
        msg = SessionMessage(message=notif)
        assert extract_payload(msg, self.NOTIFICATION_TYPE) is None

    def test_missing_message_key(self) -> None:
        notif = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={
                "level": "info",
                "data": {"notification_type": self.NOTIFICATION_TYPE},
            },
        )
        msg = SessionMessage(message=notif)
        assert extract_payload(msg, self.NOTIFICATION_TYPE) is None


# ---------------------------------------------------------------------------
# TestBuildOutboundNotification
# ---------------------------------------------------------------------------


class TestBuildOutboundNotification:
    def _data(self) -> dict[str, Any]:
        return {"foo": "bar", "n": 1}

    def _build_meta(self, _data: dict[str, Any]) -> dict[str, str]:
        return {"meta_key": "meta_value"}

    def test_returns_session_message(self) -> None:
        result = build_outbound_notification(self._data(), self._build_meta)
        assert isinstance(result, SessionMessage)

    def test_root_is_json_rpc_notification(self) -> None:
        result = build_outbound_notification(self._data(), self._build_meta)
        assert isinstance(result.message, JSONRPCNotification)

    def test_method_is_notifications_claude_channel(self) -> None:
        result = build_outbound_notification(self._data(), self._build_meta)
        assert result.message.method == "notifications/claude/channel"

    def test_content_and_meta_from_build_meta_callback(self) -> None:
        data = self._data()
        result = build_outbound_notification(data, self._build_meta)
        params = result.message.params or {}
        assert json.loads(params["content"]) == data
        assert params["meta"] == {"meta_key": "meta_value"}


# ---------------------------------------------------------------------------
# TestBuildServerNotification
# ---------------------------------------------------------------------------


class TestBuildServerNotification:
    """Covers the construction the three server ``_drain`` closures share.

    The drains themselves are ``pragma: no cover``; this is the executed
    assertion that the mcp ``SessionMessage``/``JSONRPCNotification`` shape
    still fits (the 1.x -> 2.x change raised ``TypeError`` here in production).
    """

    def _notification(self) -> dict[str, Any]:
        return {"notification_type": "cw-pr-event", "message": "{}", "title": "t"}

    def test_returns_session_message_wrapping_notification(self) -> None:
        result = build_server_notification("cw-pr-events", self._notification())
        assert isinstance(result, SessionMessage)
        assert isinstance(result.message, JSONRPCNotification)

    def test_method_and_params(self) -> None:
        data = self._notification()
        result = build_server_notification("cw-queue-events", data)
        assert result.message.method == "notifications/message"
        params = result.message.params or {}
        assert params["level"] == "info"
        assert params["logger"] == "cw-queue-events"
        assert params["data"] == data

    def test_round_trips_through_extract_payload(self) -> None:
        payload = {"client": "c", "ticket_id": "1"}
        data = {"notification_type": "cw-op", "message": json.dumps(payload)}
        result = build_server_notification("cw-operator", data)
        assert extract_payload(result, "cw-op") == payload


# ---------------------------------------------------------------------------
# TestRelayUpstream
# ---------------------------------------------------------------------------


class TestRelayUpstream:
    NOTIFICATION_TYPE = "relay-test-event"

    async def _drain(
        self,
        config: ChannelProxyConfig,
        messages: list[Any],
        client_id: str | None,
    ) -> int:
        import anyio

        send_in, recv_in = anyio.create_memory_object_stream[Any](max_buffer_size=10)
        send_out, recv_out = anyio.create_memory_object_stream[Any](max_buffer_size=10)
        for m in messages:
            await send_in.send(m)
        await send_in.aclose()

        await relay_upstream(recv_in, send_out, client_id, config=config)
        await send_out.aclose()

        count = 0
        async for _ in recv_out:
            count += 1
        return count

    def test_filter_by_client_matching_relayed(self) -> None:
        import anyio

        msg = _make_channel_session_message(self.NOTIFICATION_TYPE, {"client": "acme"})
        config = _make_test_config(
            notification_type=self.NOTIFICATION_TYPE, filter_by_client=True
        )

        async def _run() -> int:
            return await self._drain(config, [msg], "acme")

        assert anyio.run(_run) == 1

    def test_filter_by_client_non_matching_suppressed(self) -> None:
        import anyio

        msg = _make_channel_session_message(self.NOTIFICATION_TYPE, {"client": "acme"})
        config = _make_test_config(
            notification_type=self.NOTIFICATION_TYPE, filter_by_client=True
        )

        async def _run() -> int:
            return await self._drain(config, [msg], "other-client")

        assert anyio.run(_run) == 0

    def test_filter_by_client_no_client_id_relays_all(self) -> None:
        import anyio

        msg = _make_channel_session_message(self.NOTIFICATION_TYPE, {"client": "acme"})
        config = _make_test_config(
            notification_type=self.NOTIFICATION_TYPE, filter_by_client=True
        )

        async def _run() -> int:
            return await self._drain(config, [msg], None)

        assert anyio.run(_run) == 1

    def test_filter_by_client_false_relays_regardless_of_client_id(self) -> None:
        """Regression guard for pr's "always relay" behavior once driven

        through the shared code path (filter_by_client=False) instead of
        pr's own bespoke _relay_upstream.
        """
        import anyio

        msg = _make_channel_session_message(self.NOTIFICATION_TYPE, {"client": "acme"})
        config = _make_test_config(
            notification_type=self.NOTIFICATION_TYPE, filter_by_client=False
        )

        async def _run() -> int:
            return await self._drain(config, [msg], "does-not-match")

        assert anyio.run(_run) == 1

    def test_always_relay_bypasses_filter_before_client_check(self) -> None:
        """Proves the bypass-before-filter ordering is preserved in the

        shared implementation -- mirrors operator's
        test_pr_registered_always_relayed_even_with_client_id.
        """
        import anyio

        msg = _make_channel_session_message(
            self.NOTIFICATION_TYPE, {"client": "acme", "special": True}
        )
        config = _make_test_config(
            notification_type=self.NOTIFICATION_TYPE,
            filter_by_client=True,
            always_relay=lambda payload: payload.get("special") is True,
        )

        async def _run() -> int:
            return await self._drain(config, [msg], "does-not-match")

        assert anyio.run(_run) == 1

    def test_exception_skipped(self) -> None:
        import anyio

        exc = Exception("sse error")
        config = _make_test_config(notification_type=self.NOTIFICATION_TYPE)

        async def _run() -> int:
            return await self._drain(config, [exc], None)

        assert anyio.run(_run) == 0

    def test_non_matching_notification_type_skipped(self) -> None:
        import anyio

        msg = _make_channel_session_message("some-other-type", {"client": "acme"})
        config = _make_test_config(notification_type=self.NOTIFICATION_TYPE)

        async def _run() -> int:
            return await self._drain(config, [msg], None)

        assert anyio.run(_run) == 0


# ---------------------------------------------------------------------------
# TestRunProxy
# ---------------------------------------------------------------------------


class TestRunProxy:
    def test_server_constructed_without_instructions_kwarg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct regression test for R1: Server(config.server_name) stays a

        single positional arg -- ChannelProxyConfig.instructions is stored
        but never read by run_proxy. Fails loudly if a future change wires
        config.instructions through.
        """
        import mcp.server as _server_mod

        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        class _StopEarly(Exception):  # noqa: N818
            pass

        class _FakeServer:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                calls.append((args, kwargs))
                raise _StopEarly

        monkeypatch.setattr(_server_mod, "Server", _FakeServer)
        config = _make_test_config(instructions="some unused instructions")

        with pytest.raises(_StopEarly):
            run_proxy(config=config)

        assert calls == [((config.server_name,), {})]


# ---------------------------------------------------------------------------
# TestRunProxyEnvVars
# ---------------------------------------------------------------------------


class TestRunProxyEnvVars:
    def _invoke_proxy_capture_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: ChannelProxyConfig,
        client_id: str | None = None,
    ) -> str:
        """Invoke run_proxy, capturing the SSE URL via sse_client patch."""
        import contextlib
        from contextlib import asynccontextmanager

        import mcp.client.sse as _sse_mod

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
            run_proxy(client_id=client_id, config=config)

        return captured[0] if captured else ""

    def test_default_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_test_config()
        monkeypatch.delenv(config.base_url_env, raising=False)
        monkeypatch.delenv(config.client_id_env, raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch, config)
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme + "://" + parsed.netloc == config.default_base_url
        assert parsed.path == config.sse_path

    def test_custom_base_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_test_config()
        monkeypatch.setenv(config.base_url_env, "http://example.com:9999")
        monkeypatch.delenv(config.client_id_env, raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch, config)
        parsed = urllib.parse.urlparse(url)
        assert parsed.netloc == "example.com:9999"
        assert parsed.path == config.sse_path

    def test_custom_client_id_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _make_test_config()
        monkeypatch.delenv(config.base_url_env, raising=False)
        monkeypatch.setenv(config.client_id_env, "my-custom-id")
        url = self._invoke_proxy_capture_url(monkeypatch, config)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == ["my-custom-id"]

    def test_client_id_param_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_test_config()
        monkeypatch.delenv(config.base_url_env, raising=False)
        monkeypatch.setenv(config.client_id_env, "env-id")
        url = self._invoke_proxy_capture_url(
            monkeypatch, config, client_id="explicit-id"
        )
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == ["explicit-id"]

    def test_default_client_id_is_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _make_test_config()
        monkeypatch.delenv(config.base_url_env, raising=False)
        monkeypatch.delenv(config.client_id_env, raising=False)
        url = self._invoke_proxy_capture_url(monkeypatch, config)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params.get("client_id") == [socket.gethostname()]
