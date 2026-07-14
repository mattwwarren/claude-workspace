"""Tests for cw_pr_events_server: payload validation, notification shape, registry."""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

starlette = pytest.importorskip(
    "starlette", reason="requires mcp extras: pip install 'cw[mcp]'"
)

from starlette.testclient import TestClient

import cw.cw_pr_events_server as _server_mod
from cw.cw_pr_events_server import (
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
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import DevQueueStore, OrchestratorEventType, PrState, TicketTask
from cw.pr_events_auth import (
    CW_PR_EVENTS_HMAC_SECRET_ENV,
    SIGNATURE_HEADER,
    SIGNATURE_PREFIX,
)
from cw.pr_hydrate import observe_pushed_event


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


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
        "event_type",
        ["ci_failed", "review_received", "mergeable", "merged", "review_requested"],
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
        # These tests exercise payload validation / broadcast behavior, not
        # auth -- allow_unsigned=True keeps them focused on that (#1127).
        return TestClient(make_app(allow_unsigned=True))

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


class TestReviewRequestedWebhook:
    """review_requested webhook registration (GitHub #1154, RFC 0011 S2, R4)."""

    _OPERATOR = "mattwwarren"

    def _make_client(self) -> TestClient:
        return TestClient(make_app(allow_unsigned=True))

    def _post(self, client: TestClient, payload: dict[str, Any]) -> Any:
        return client.post(
            "/pr-event",
            json={
                "repo": "acme/widgets",
                "pr_number": 42,
                "event_type": "review_requested",
                "payload": payload,
            },
        )

    def test_individual_review_request_registers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: self._OPERATOR
        )
        resp = self._post(self._make_client(), {"reviewer": {"login": self._OPERATOR}})
        assert resp.status_code == 200
        assert resp.json() == {"registered": True, "reason": "registered"}
        watched = load_dev_queue().watched_prs
        assert len(watched) == 1
        assert watched[0].status == "active"
        assert watched[0].source == "webhook"

    def test_team_review_request_ignored_with_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: self._OPERATOR
        )
        resp = self._post(self._make_client(), {"reviewer": {"slug": "eng-team"}})
        assert resp.status_code == 200
        assert resp.json() == {"registered": False, "reason": "team_targeted"}
        assert load_dev_queue().watched_prs == []

    def test_identity_unresolved_fails_closed_on_webhook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.operator_identity.cached_gh_login", lambda: None)
        resp = self._post(self._make_client(), {"reviewer": {"login": self._OPERATOR}})
        assert resp.status_code == 200
        assert resp.json() == {"registered": False, "reason": "identity_unresolved"}
        assert load_dev_queue().watched_prs == []

    def test_review_requested_idempotent_on_replay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: self._OPERATOR
        )
        client = self._make_client()
        first = self._post(client, {"reviewer": {"login": self._OPERATOR}})
        assert first.json() == {"registered": True, "reason": "registered"}
        second = self._post(client, {"reviewer": {"login": self._OPERATOR}})
        assert second.json() == {"registered": False, "reason": "already_registered"}

    def test_review_requested_still_broadcasts_mcp_notification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: self._OPERATOR
        )
        q = subscribe()
        try:
            self._post(self._make_client(), {"reviewer": {"login": self._OPERATOR}})
            notif = q.get_nowait()
            assert notif["notification_type"] == _NOTIFICATION_TYPE
            data = json.loads(notif["message"])
            assert data["event_type"] == "review_requested"
        finally:
            unsubscribe(q)

    def test_review_requested_missing_reviewer_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.operator_identity.cached_gh_login", lambda: self._OPERATOR
        )
        resp = self._post(self._make_client(), {})
        assert resp.status_code == 200
        assert resp.json() == {"registered": False, "reason": "no_reviewer"}
        assert load_dev_queue().watched_prs == []


class TestServe:
    def test_serve_calls_uvicorn_run(self) -> None:
        mock_run = MagicMock()
        with patch("uvicorn.run", mock_run):
            serve(host="127.0.0.1", port=9999)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs.get("host") == "127.0.0.1"
        assert call_kwargs.get("port") == 9999

    def test_serve_passes_allow_unsigned_to_make_app(self) -> None:
        mock_make_app = MagicMock()
        with (
            patch("uvicorn.run", MagicMock()),
            patch("cw.cw_pr_events_server.make_app", mock_make_app),
        ):
            serve(host="127.0.0.1", port=9999, allow_unsigned=True)
        mock_make_app.assert_called_once_with(allow_unsigned=True)

    def test_serve_calls_warn_with_allow_unsigned(self) -> None:
        mock_warn = MagicMock()
        with (
            patch("uvicorn.run", MagicMock()),
            patch("cw.cw_pr_events_server.warn_if_unsigned_mode", mock_warn),
        ):
            serve(host="127.0.0.1", port=9999, allow_unsigned=True)
        mock_warn.assert_called_once_with(allow_unsigned=True)


class TestCLIPrChannel:
    def test_pr_channel_serve_command_invokes_serve(self) -> None:
        from cw.cli import main

        mock_serve = MagicMock()
        runner = CliRunner()
        with patch("cw.cw_pr_events_server.serve", mock_serve):
            result = runner.invoke(main, ["pr-channel", "serve", "--port", "9123"])
        assert result.exit_code == 0
        mock_serve.assert_called_once_with(
            host="127.0.0.1", port=9123, allow_unsigned=False
        )

    def test_pr_channel_serve_command_passes_allow_unsigned_flag(self) -> None:
        from cw.cli import main

        mock_serve = MagicMock()
        runner = CliRunner()
        with patch("cw.cw_pr_events_server.serve", mock_serve):
            result = runner.invoke(
                main, ["pr-channel", "serve", "--port", "9123", "--allow-unsigned"]
            )
        assert result.exit_code == 0
        mock_serve.assert_called_once_with(
            host="127.0.0.1", port=9123, allow_unsigned=True
        )


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

    def test_skips_malformed_line_without_raising(self) -> None:
        """A torn/partial JSONL line is skipped, not raised (#433)."""
        from cw.config import state_dir
        from cw.cw_pr_events_server import _read_events_from_offset

        path = state_dir() / "channel-events.jsonl"
        path.write_text(
            json.dumps({"notification_type": "cw-pr-event", "offset": 0})
            + "\n"
            + "\n"  # blank line (also skipped)
            + "{ partial torn line\n"  # malformed: no closing brace
        )
        result = _read_events_from_offset(0)
        assert len(result) == 1  # blank + malformed lines skipped, valid one kept

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


# ---------------------------------------------------------------------------
# Defect #433: subscribe_with_cursor TOCTOU / fsync fixes
# ---------------------------------------------------------------------------


class TestSubscribeWithCursorTOCTOU:
    """Subscribe+replay window must not deliver duplicate events (TOCTOU fix)."""

    def test_no_double_delivery_when_broadcast_races_subscribe(self) -> None:
        """Event broadcast during subscribe+replay gap must NOT be double-delivered."""
        from cw.cw_pr_events_server import _append_event

        # Set up: event 0 already appended, cursor at 0 (will replay from 0)
        _append_event(
            {"notification_type": "cw-pr-event", "message": "pre", "title": "pre"}
        )
        # cursor at 0 means subscriber expects to replay from 0

        # The TOCTOU window: between subscribe() and _read_events_from_offset(),
        # a broadcast appends event offset=1 and fans out to the new subscriber.
        # The replay then also reads offset=1 → double delivery.
        #
        # The fix: replay is bounded to the offset snapshotted BEFORE subscribe(),
        # so event offset=1 (appended after snapshot) is excluded from replay
        # (it is delivered only via the live fan-out).

        results: list[list[dict]] = []
        errors: list[Exception] = []

        def _subscriber() -> None:
            try:
                q = subscribe_with_cursor("toctou-sub")
                # Drain all items including any replayed ones
                items = []
                import time

                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    try:
                        items.append(q.get_nowait())
                    except Exception:  # noqa: BLE001
                        import time as _t

                        _t.sleep(0.01)
                results.append(items)
                unsubscribe(q)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        sub_thread = threading.Thread(target=_subscriber)
        sub_thread.start()

        # Broadcast immediately (may hit the gap)
        broadcast(
            {
                "notification_type": "cw-pr-event",
                "message": "concurrent",
                "title": "c",
            }
        )

        sub_thread.join(timeout=5)
        assert not errors, f"Subscriber thread errored: {errors}"
        assert results, "Subscriber produced no results"

        items = results[0]
        offsets = [item.get("offset") for item in items]
        # No offset should appear twice — even if event 0 AND 1 are received,
        # each must appear exactly once.
        assert len(offsets) == len(set(offsets)), (
            f"Duplicate delivery detected: offsets={offsets}"
        )

    def test_append_event_fsyncs_after_flush(self) -> None:
        """_append_event must call os.fsync after flush (durability fix)."""
        import os
        from unittest.mock import patch

        fsync_calls: list[int] = []
        real_fsync = os.fsync

        def _mock_fsync(fd: int) -> None:
            fsync_calls.append(fd)
            real_fsync(fd)

        with patch("os.fsync", side_effect=_mock_fsync):
            broadcast(
                {
                    "notification_type": "cw-pr-event",
                    "message": "durable",
                    "title": "d",
                }
            )

        assert fsync_calls, "os.fsync was never called during _append_event"


_PR_URL = "https://github.com/acme/widgets/pull/42"


class TestPrEventHMAC:
    """HMAC authentication on POST /pr-event (#930)."""

    def test_accepts_valid_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "s3cr3t")
        client = TestClient(make_app())
        body = json.dumps(
            {"repo": "owner/repo", "pr_number": 1, "event_type": "merged"}
        ).encode()
        sig = _sign("s3cr3t", body)
        resp = client.post(
            "/pr-event",
            content=body,
            headers={SIGNATURE_HEADER: sig, "content-type": "application/json"},
        )
        assert resp.status_code == 200

    def test_rejects_invalid_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "s3cr3t")
        client = TestClient(make_app())
        body = json.dumps(
            {"repo": "owner/repo", "pr_number": 1, "event_type": "merged"}
        ).encode()
        resp = client.post(
            "/pr-event",
            content=body,
            headers={
                SIGNATURE_HEADER: SIGNATURE_PREFIX + "deadbeef",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_rejects_missing_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "s3cr3t")
        client = TestClient(make_app())
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 1, "event_type": "merged"},
        )
        assert resp.status_code == 401

    def test_rejects_unsigned_when_secret_unset_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default-deny (#1127): no secret configured and no --allow-unsigned
        opt-in -- unsigned requests are rejected with 401, not accepted."""
        monkeypatch.delenv(CW_PR_EVENTS_HMAC_SECRET_ENV, raising=False)
        client = TestClient(make_app())
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 1, "event_type": "merged"},
        )
        assert resp.status_code == 401

    def test_accepts_unsigned_when_secret_unset_and_allow_unsigned_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The explicit --allow-unsigned opt-in restores the old open behavior."""
        monkeypatch.delenv(CW_PR_EVENTS_HMAC_SECRET_ENV, raising=False)
        client = TestClient(make_app(allow_unsigned=True))
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 1, "event_type": "merged"},
        )
        assert resp.status_code == 200

    def test_signature_still_required_when_secret_set_even_with_allow_unsigned_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Load-bearing regression: allow_unsigned=True must never bypass a
        real signature check once a secret IS configured."""
        monkeypatch.setenv(CW_PR_EVENTS_HMAC_SECRET_ENV, "s3cr3t")
        client = TestClient(make_app(allow_unsigned=True))
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 1, "event_type": "merged"},
        )
        assert resp.status_code == 401


class TestWireSuffixMapping:
    """Each of the 4 wire event_type suffixes maps to the matching pr.*
    OrchestratorEventType (#930)."""

    @pytest.mark.parametrize(
        ("suffix", "expected"),
        [
            ("ci_failed", OrchestratorEventType.PR_CI_FAILED),
            ("review_received", OrchestratorEventType.PR_REVIEW_RECEIVED),
            ("mergeable", OrchestratorEventType.PR_MERGEABLE),
            ("merged", OrchestratorEventType.PR_MERGED),
        ],
    )
    def test_suffix_maps_to_event_type(
        self, suffix: str, expected: OrchestratorEventType
    ) -> None:
        assert suffix in _server_mod._VALID_EVENT_TYPES
        assert OrchestratorEventType("pr." + suffix) is expected


class TestPushFeedsSharedObservation:
    """POST /pr-event feeds the SAME persist/diff/emit path as poll hydration
    (#930): a pushed event lands in dev_queue.json (pr_state) and
    inbox.jsonl (pr.* events), just like hydrate_pr_states.
    """

    def test_merged_push_persists_pr_state_and_emits_event(self) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    TicketTask(
                        ticket_id="GEN-9",
                        client="acme",
                        pr_url=_PR_URL,
                        pr_state=PrState(state="OPEN"),
                    )
                ]
            )
        )
        client = TestClient(make_app(allow_unsigned=True))
        resp = client.post(
            "/pr-event",
            json={"repo": "acme/widgets", "pr_number": 42, "event_type": "merged"},
        )
        assert resp.status_code == 200

        task = load_dev_queue().tasks[0]
        assert task.pr_state is not None
        assert task.pr_state.state == "MERGED"

        events = read_events(event_types=[OrchestratorEventType.PR_MERGED])
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-9"
        assert events[0].payload["client"] == "acme"

    def test_unmatched_pr_is_silent_noop(self) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        client = TestClient(make_app(allow_unsigned=True))
        resp = client.post(
            "/pr-event",
            json={"repo": "ghost/repo", "pr_number": 1, "event_type": "merged"},
        )
        assert resp.status_code == 200
        assert read_events(event_types=[OrchestratorEventType.PR_MERGED]) == []


class TestAsyncOffloadSeam:
    """observe_pushed_event's dev_queue_lock is a blocking fcntl.flock — it
    must be offloaded via anyio.to_thread.run_sync, never called directly on
    the event loop thread (#930).
    """

    def test_handler_offloads_via_run_sync(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, ...]] = []

        async def _fake_run_sync(func: Any, *args: Any) -> Any:
            calls.append((func, args))
            return None

        monkeypatch.setattr("anyio.to_thread.run_sync", _fake_run_sync)
        client = TestClient(make_app(allow_unsigned=True))
        resp = client.post(
            "/pr-event",
            json={
                "repo": "owner/repo",
                "pr_number": 1,
                "event_type": "merged",
                "payload": {"foo": "bar"},
            },
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        offloaded_func, offloaded_args = calls[0]
        assert offloaded_args == ()
        assert offloaded_func.func is observe_pushed_event
        assert offloaded_func.keywords == {
            "repo": "owner/repo",
            "pr_number": 1,
            "wire_event_type": "merged",
            "payload": {"foo": "bar"},
        }
