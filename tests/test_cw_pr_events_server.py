"""Tests for cw_pr_events_server: payload validation, notification shape, registry."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

starlette = pytest.importorskip(
    "starlette", reason="requires mcp extras: pip install 'cw[mcp]'"
)

from starlette.testclient import TestClient  # noqa: E402

import cw.cw_pr_events_server as _server_mod  # noqa: E402
from cw.cw_pr_events_server import (  # noqa: E402
    _NOTIFICATION_TYPE,
    PREventRequest,
    _build_notification,
    broadcast,
    make_app,
    serve,
    subscribe,
    subscribe_with_cursor,
    unsubscribe,
)


@pytest.fixture(autouse=True)
def _reset_subscribers() -> Generator[None]:
    """Clear global subscriber list between tests to prevent state bleed."""
    with _server_mod._lock:
        _server_mod._subscribers.clear()
    yield
    with _server_mod._lock:
        _server_mod._subscribers.clear()


@pytest.fixture(autouse=True)
def _reset_channel_state() -> Generator[None]:
    """Reset durable-replay in-memory state between tests."""
    with _server_mod._file_lock:
        _server_mod._cursors.clear()
        _server_mod._event_offset[0] = 0
    yield
    with _server_mod._file_lock:
        _server_mod._cursors.clear()
        _server_mod._event_offset[0] = 0


class TestPREventPayloadValidation:
    def test_valid_payload_accepted(self) -> None:
        event = PREventRequest(
            repo="owner/repo", pr_number=42, event_type="ci_failed", payload={}
        )
        assert event.repo == "owner/repo"
        assert event.pr_number == 42

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventRequest.model_validate({"pr_number": 42, "event_type": "ci_failed"})

    def test_invalid_event_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PREventRequest(repo="x", pr_number=1, event_type="unknown_type")

    def test_pr_number_must_be_int(self) -> None:
        with pytest.raises(ValidationError):
            PREventRequest.model_validate(
                {"repo": "x", "pr_number": "foo", "event_type": "ci_failed"}
            )


class TestMCPNotificationShape:
    def _make_event(self, event_type: str = "ci_failed") -> PREventRequest:
        return PREventRequest(
            repo="owner/repo",
            pr_number=42,
            event_type=event_type,
            payload={"key": "val"},
        )

    def test_notification_has_correct_type(self) -> None:
        notif = _build_notification(self._make_event())
        assert notif["notification_type"] == _NOTIFICATION_TYPE

    def test_notification_message_is_json(self) -> None:
        notif = _build_notification(self._make_event())
        data = json.loads(notif["message"])
        assert "repo" in data
        assert "pr_number" in data
        assert "event_type" in data

    def test_notification_title_is_short_string(self) -> None:
        notif = _build_notification(self._make_event())
        assert "42" in notif["title"]
        assert isinstance(notif["title"], str)

    @pytest.mark.parametrize(
        "event_type", ["ci_failed", "review_received", "mergeable", "merged"]
    )
    def test_all_known_event_types_produce_title(self, event_type: str) -> None:
        event = self._make_event(event_type)
        notif = _build_notification(event)
        assert notif["title"]


class TestSubscriberRegistry:
    def test_subscribe_adds_to_registry(self) -> None:
        q = subscribe()
        try:
            assert isinstance(q, queue.SimpleQueue)
        finally:
            unsubscribe(q)

    def test_unsubscribe_removes_from_registry(self) -> None:
        q = subscribe()
        unsubscribe(q)
        broadcast({"test": True})
        assert q.empty()

    def test_broadcast_sends_to_all_queues(self) -> None:
        q1 = subscribe()
        q2 = subscribe()
        try:
            broadcast({"x": 1})
            assert q1.get_nowait() == {"x": 1}
            assert q2.get_nowait() == {"x": 1}
        finally:
            unsubscribe(q1)
            unsubscribe(q2)


class TestHandlePostPrEvent:
    def _make_client(self) -> TestClient:
        return TestClient(make_app())

    def test_valid_event_returns_ok(self) -> None:
        client = self._make_client()
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 42, "event_type": "ci_failed"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_invalid_event_type_returns_400(self) -> None:
        client = self._make_client()
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 42, "event_type": "bad_type"},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_missing_repo_returns_400(self) -> None:
        client = self._make_client()
        resp = client.post(
            "/pr-event",
            json={"pr_number": 42, "event_type": "merged"},
        )
        assert resp.status_code == 400

    def test_valid_event_broadcasts_notification(self) -> None:
        q = subscribe()
        try:
            client = self._make_client()
            client.post(
                "/pr-event",
                json={"repo": "org/proj", "pr_number": 7, "event_type": "merged"},
            )
            notif = q.get_nowait()
            assert notif["notification_type"] == _NOTIFICATION_TYPE
            data = json.loads(notif["message"])
            assert data["pr_number"] == 7
        finally:
            unsubscribe(q)

    def test_make_app_returns_starlette_app(self) -> None:
        from starlette.applications import Starlette

        app = make_app()
        assert isinstance(app, Starlette)


class TestServe:
    def test_serve_calls_uvicorn_run(self) -> None:
        mock_run = MagicMock()
        with patch("uvicorn.run", mock_run):
            serve(host="127.0.0.1", port=9999)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs.get("host") == "127.0.0.1"
        assert call_kwargs.get("port") == 9999


class TestCLIPrChannel:
    def test_pr_channel_serve_command_invokes_serve(self) -> None:
        from cw.cli import main

        mock_serve = MagicMock()
        runner = CliRunner()
        with patch("cw.cw_pr_events_server.serve", mock_serve):
            result = runner.invoke(main, ["pr-channel", "serve", "--port", "9123"])
        assert result.exit_code == 0
        mock_serve.assert_called_once_with(host="127.0.0.1", port=9123)


class TestAppendEvent:
    def test_appends_to_jsonl_file(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _append_event

        _append_event(
            {"notification_type": "cw-pr-event", "message": "m", "title": "t"}
        )
        path = state_dir() / "channel-events.jsonl"
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 1

    def test_offset_increments_monotonically(self) -> None:
        from cw.cw_pr_events_server import _append_event

        _append_event(
            {"notification_type": "cw-pr-event", "message": "m", "title": "t"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "m2", "title": "t2"}
        )
        assert _server_mod._event_offset[0] == 2

    def test_record_contains_offset_field(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _append_event

        _append_event(
            {"notification_type": "cw-pr-event", "message": "m", "title": "t"}
        )
        path = state_dir() / "channel-events.jsonl"
        record = json.loads(path.read_text().splitlines()[0])
        assert record["offset"] == 0

    def test_thread_safe_under_concurrent_appends(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _append_event

        def worker() -> None:
            _append_event(
                {"notification_type": "cw-pr-event", "message": "x", "title": "x"}
            )

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        path = state_dir() / "channel-events.jsonl"
        lines = path.read_text().splitlines()
        assert len(lines) == 10
        records = [json.loads(line) for line in lines]
        offsets = {r["offset"] for r in records}
        assert len(offsets) == 10  # all unique


class TestReadEventsFromOffset:
    def test_returns_empty_for_missing_file(self) -> None:
        from cw.cw_pr_events_server import _read_events_from_offset

        result = _read_events_from_offset(0)
        assert result == []

    def test_returns_all_events_from_zero(self) -> None:
        from cw.cw_pr_events_server import _append_event, _read_events_from_offset

        _append_event(
            {"notification_type": "cw-pr-event", "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "b", "title": "b"}
        )
        result = _read_events_from_offset(0)
        assert len(result) == 2

    def test_respects_offset_filter(self) -> None:
        from cw.cw_pr_events_server import _append_event, _read_events_from_offset

        _append_event(
            {"notification_type": "cw-pr-event", "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "b", "title": "b"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "c", "title": "c"}
        )
        result = _read_events_from_offset(1)
        assert len(result) == 2
        assert result[0]["offset"] == 1
        assert result[1]["offset"] == 2

    def test_skips_malformed_lines(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _append_event, _read_events_from_offset

        _append_event(
            {"notification_type": "cw-pr-event", "message": "a", "title": "a"}
        )
        path = state_dir() / "channel-events.jsonl"
        with path.open("a") as f:
            f.write("not-json\n")
        result = _read_events_from_offset(0)
        assert len(result) == 1


class TestSubscribeWithCursor:
    def test_returns_queue(self) -> None:
        q = subscribe_with_cursor("sub1")
        assert isinstance(q, queue.SimpleQueue)
        unsubscribe(q)

    def test_replays_missed_events(self) -> None:
        from cw.cw_pr_events_server import _append_event

        _append_event(
            {"notification_type": "cw-pr-event", "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "b", "title": "b"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "c", "title": "c"}
        )

        _server_mod._cursors["sub2"] = 1
        q = subscribe_with_cursor("sub2")
        try:
            items = []
            while not q.empty():
                items.append(q.get_nowait())
            assert len(items) == 2
            assert items[0]["offset"] == 1
            assert items[1]["offset"] == 2
        finally:
            unsubscribe(q)

    def test_no_replay_when_caught_up(self) -> None:
        from cw.cw_pr_events_server import _append_event

        _append_event(
            {"notification_type": "cw-pr-event", "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "b", "title": "b"}
        )

        _server_mod._cursors["sub3"] = 2
        q = subscribe_with_cursor("sub3")
        try:
            assert q.empty()
        finally:
            unsubscribe(q)

    def test_registers_for_future_events(self) -> None:
        q = subscribe_with_cursor("sub4")
        try:
            broadcast(
                {"notification_type": "cw-pr-event", "message": "future", "title": "f"}
            )
            item = q.get_nowait()
            assert item["notification_type"] == "cw-pr-event"
        finally:
            unsubscribe(q)


class TestAckOffset:
    def test_persists_cursor_to_file(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import ack_offset

        ack_offset("sub-a", 3)
        path = state_dir() / "channel-cursors.json"
        data = json.loads(path.read_text())
        assert data == {"sub-a": 3}

    def test_updates_in_memory_cursors(self) -> None:
        from cw.cw_pr_events_server import ack_offset

        ack_offset("sub-b", 7)
        assert _server_mod._cursors["sub-b"] == 7

    def test_overwrites_previous_cursor(self) -> None:
        from cw.cw_pr_events_server import ack_offset

        ack_offset("sub-c", 1)
        ack_offset("sub-c", 5)
        assert _server_mod._cursors["sub-c"] == 5


class TestHandlePostAck:
    def _make_client(self) -> TestClient:
        return TestClient(make_app())

    def test_valid_request_returns_ok(self) -> None:
        client = self._make_client()
        resp = client.post("/ack", json={"client_id": "c1", "offset": 0})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_invalid_body_returns_400(self) -> None:
        client = self._make_client()
        resp = client.post("/ack", json={"client_id": "c1"})
        assert resp.status_code == 400

    def test_updates_cursor(self) -> None:
        client = self._make_client()
        client.post("/ack", json={"client_id": "c2", "offset": 42})
        assert _server_mod._cursors["c2"] == 42

    def test_route_registered(self) -> None:
        app = make_app()
        route_paths = [r.path for r in app.routes]
        assert "/ack" in route_paths


class TestBroadcastDurable:
    def test_appends_to_file(self) -> None:
        from cw.config import state_dir

        broadcast({"notification_type": "cw-pr-event", "message": "m", "title": "t"})
        path = state_dir() / "channel-events.jsonl"
        assert path.exists()
        lines = path.read_text().splitlines()
        assert len(lines) == 1

    def test_persists_before_subscriber_receives(self) -> None:
        from cw.config import state_dir

        # Verify file is written as part of broadcast
        q = subscribe()
        try:
            broadcast(
                {"notification_type": "cw-pr-event", "message": "m", "title": "t"}
            )
            # File must exist after broadcast completes
            path = state_dir() / "channel-events.jsonl"
            assert path.exists()
            # Subscriber also received it
            item = q.get_nowait()
            assert item["notification_type"] == "cw-pr-event"
        finally:
            unsubscribe(q)


class TestDurableReplay:
    def test_five_events_survive_subscriber_restart(self) -> None:
        """Primary acceptance criterion from ticket."""

        # 1. Broadcast 5 events
        for i in range(5):
            broadcast(
                {
                    "notification_type": "cw-pr-event",
                    "message": f"msg{i}",
                    "title": f"t{i}",
                }
            )

        # 2. Subscribe with cursor at 0 (no prior cursor) — simulates reconnect
        q = subscribe_with_cursor("dispatcher")
        try:
            # 3. Drain queue
            items = []
            while not q.empty():
                items.append(q.get_nowait())

            # 4. Assert exactly 5 notifications with offsets 0-4
            assert len(items) == 5
            assert {item["offset"] for item in items} == {0, 1, 2, 3, 4}
        finally:
            unsubscribe(q)


class TestLoadCursors:
    def test_returns_empty_for_missing_file(self) -> None:
        from cw.cw_pr_events_server import _load_cursors

        result = _load_cursors()
        assert result == {}

    def test_returns_persisted_cursors(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _load_cursors

        path = state_dir() / "channel-cursors.json"
        path.write_text(json.dumps({"sub-x": 5, "sub-y": 12}))
        result = _load_cursors()
        assert result == {"sub-x": 5, "sub-y": 12}

    def test_returns_empty_on_corrupt_file(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _load_cursors

        path = state_dir() / "channel-cursors.json"
        path.write_text("not-json")
        result = _load_cursors()
        assert result == {}


class TestLoadOffsetFromFile:
    def test_returns_zero_for_missing_file(self) -> None:
        from cw.cw_pr_events_server import _load_offset_from_file

        result = _load_offset_from_file()
        assert result == 0

    def test_returns_max_offset_plus_one(self) -> None:
        from cw.cw_pr_events_server import _append_event, _load_offset_from_file

        _append_event(
            {"notification_type": "cw-pr-event", "message": "a", "title": "a"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "b", "title": "b"}
        )
        _append_event(
            {"notification_type": "cw-pr-event", "message": "c", "title": "c"}
        )
        result = _load_offset_from_file()
        assert result == 3

    def test_skips_malformed_lines(self) -> None:
        from cw.config import state_dir
        from cw.cw_pr_events_server import _append_event, _load_offset_from_file

        _append_event(
            {"notification_type": "cw-pr-event", "message": "a", "title": "a"}
        )
        path = state_dir() / "channel-events.jsonl"
        with path.open("a") as f:
            f.write("bad-json\n")
        result = _load_offset_from_file()
        assert result == 1


class TestLazyStarlette:
    """Verify starlette is not required at module-load time (lazy-import contract)."""

    def test_module_import_does_not_require_starlette(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib
        import sys

        # Block starlette sub-modules to simulate the [mcp] extra being absent
        for key in (
            "starlette",
            "starlette.applications",
            "starlette.responses",
            "starlette.routing",
            "starlette.requests",
        ):
            monkeypatch.setitem(sys.modules, key, None)

        server_mod_name = "cw.cw_pr_events_server"
        original_mod = sys.modules.pop(server_mod_name, None)
        try:
            mod = importlib.import_module(server_mod_name)
            assert mod is not None
        finally:
            sys.modules.pop(server_mod_name, None)
            if original_mod is not None:
                sys.modules[server_mod_name] = original_mod

    def test_make_app_raises_clear_importerror_without_starlette(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "starlette.applications", None)

        with pytest.raises(ImportError, match=r"channel server requires \[mcp\] extra"):
            _server_mod.make_app()

    def test_serve_raises_clear_importerror_without_starlette(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "starlette.applications", None)

        with pytest.raises(ImportError, match=r"channel server requires \[mcp\] extra"):
            _server_mod.serve()


class TestSSERouting:
    """Regression: /sse must not 307-redirect (issue #305).

    TestClient hangs on live SSE connections so tests are structural/unit-level.
    """

    def test_app_redirect_slashes_disabled(self) -> None:
        """make_app() disables Starlette's default redirect_slashes to stop /sse→307."""
        app = make_app()
        assert not app.router.redirect_slashes

    def test_sse_slash_middleware_rewrites_bare_path(self) -> None:
        """_SSESlashMiddleware rewrites /sse (no trailing slash) → /sse/."""
        import asyncio

        from cw.cw_pr_events_server import _SSESlashMiddleware

        captured: list[str] = []

        async def _capture(scope: object, receive: object, send: object) -> None:
            assert isinstance(scope, dict)
            captured.append(scope["path"])

        mw = _SSESlashMiddleware(_capture)

        async def _run() -> None:
            await mw(
                {"type": "http", "path": "/sse", "query_string": b"client_id=t"},
                None,
                None,
            )

        asyncio.run(_run())
        assert captured == ["/sse/"]

    def test_sse_slash_middleware_leaves_slash_path_unchanged(self) -> None:
        """_SSESlashMiddleware leaves /sse/ (already has trailing slash) untouched."""
        import asyncio

        from cw.cw_pr_events_server import _SSESlashMiddleware

        captured: list[str] = []

        async def _capture(scope: object, receive: object, send: object) -> None:
            assert isinstance(scope, dict)
            captured.append(scope["path"])

        mw = _SSESlashMiddleware(_capture)

        async def _run() -> None:
            await mw(
                {"type": "http", "path": "/sse/", "query_string": b"client_id=t"},
                None,
                None,
            )

        asyncio.run(_run())
        assert captured == ["/sse/"]

    def test_sse_slash_middleware_ignores_other_paths(self) -> None:
        """_SSESlashMiddleware does not rewrite non-/sse paths."""
        import asyncio

        from cw.cw_pr_events_server import _SSESlashMiddleware

        captured: list[str] = []

        async def _capture(scope: object, receive: object, send: object) -> None:
            assert isinstance(scope, dict)
            captured.append(scope["path"])

        mw = _SSESlashMiddleware(_capture)

        async def _run() -> None:
            for path in ["/ack", "/pr-event", "/messages"]:
                scope = {"type": "http", "path": path, "query_string": b""}
                await mw(scope, None, None)

        asyncio.run(_run())
        assert captured == ["/ack", "/pr-event", "/messages"]
