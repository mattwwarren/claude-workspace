"""Tests for cw_pr_events_server: payload validation, notification shape, registry."""

from __future__ import annotations

import json
import queue
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError
from starlette.testclient import TestClient

from cw.cw_pr_events_server import (
    PREventRequest,
    _build_notification,
    broadcast,
    make_app,
    serve,
    subscribe,
    unsubscribe,
)


class TestPREventPayloadValidation:
    def test_valid_payload_accepted(self):
        event = PREventRequest(
            repo="owner/repo", pr_number=42, event_type="ci_failed", payload={}
        )
        assert event.repo == "owner/repo"
        assert event.pr_number == 42

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            PREventRequest(pr_number=42, event_type="ci_failed")

    def test_invalid_event_type_rejected(self):
        with pytest.raises(ValidationError):
            PREventRequest(repo="x", pr_number=1, event_type="unknown_type")

    def test_pr_number_must_be_int(self):
        with pytest.raises(ValidationError):
            PREventRequest(repo="x", pr_number="foo", event_type="ci_failed")


class TestMCPNotificationShape:
    def _make_event(self, event_type: str = "ci_failed") -> PREventRequest:
        return PREventRequest(
            repo="owner/repo",
            pr_number=42,
            event_type=event_type,
            payload={"key": "val"},
        )

    def test_notification_has_correct_type(self):
        notif = _build_notification(self._make_event())
        assert notif["notification_type"] == "cw-pr-event"

    def test_notification_message_is_json(self):
        notif = _build_notification(self._make_event())
        data = json.loads(notif["message"])
        assert "repo" in data
        assert "pr_number" in data
        assert "event_type" in data

    def test_notification_title_is_short_string(self):
        notif = _build_notification(self._make_event())
        assert "42" in notif["title"]
        assert isinstance(notif["title"], str)

    @pytest.mark.parametrize(
        "event_type", ["ci_failed", "review_received", "mergeable", "merged"]
    )
    def test_all_known_event_types_produce_title(self, event_type: str):
        event = self._make_event(event_type)
        notif = _build_notification(event)
        assert notif["title"]


class TestSubscriberRegistry:
    def test_subscribe_adds_to_registry(self):
        q = subscribe()
        try:
            assert isinstance(q, queue.SimpleQueue)
        finally:
            unsubscribe(q)

    def test_unsubscribe_removes_from_registry(self):
        q = subscribe()
        unsubscribe(q)
        broadcast({"test": True})
        assert q.empty()

    def test_broadcast_sends_to_all_queues(self):
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

    def test_valid_event_returns_ok(self):
        client = self._make_client()
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 42, "event_type": "ci_failed"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_invalid_event_type_returns_400(self):
        client = self._make_client()
        resp = client.post(
            "/pr-event",
            json={"repo": "owner/repo", "pr_number": 42, "event_type": "bad_type"},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_missing_repo_returns_400(self):
        client = self._make_client()
        resp = client.post(
            "/pr-event",
            json={"pr_number": 42, "event_type": "merged"},
        )
        assert resp.status_code == 400

    def test_valid_event_broadcasts_notification(self):
        q = subscribe()
        try:
            client = self._make_client()
            client.post(
                "/pr-event",
                json={"repo": "org/proj", "pr_number": 7, "event_type": "merged"},
            )
            notif = q.get_nowait()
            assert notif["notification_type"] == "cw-pr-event"
            data = json.loads(notif["message"])
            assert data["pr_number"] == 7
        finally:
            unsubscribe(q)

    def test_make_app_returns_starlette_app(self):
        from starlette.applications import Starlette

        app = make_app()
        assert isinstance(app, Starlette)


class TestServe:
    def test_serve_calls_uvicorn_run(self):
        mock_run = MagicMock()
        with patch("uvicorn.run", mock_run):
            serve(host="127.0.0.1", port=9999)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs.get("host") == "127.0.0.1"


class TestCLIPrChannel:
    def test_pr_channel_serve_command_invokes_serve(self):
        from cw.cli import main

        mock_serve = MagicMock()
        runner = CliRunner()
        with patch("cw.cw_pr_events_server.serve", mock_serve):
            result = runner.invoke(main, ["pr-channel", "serve", "--port", "9123"])
        assert result.exit_code == 0
        mock_serve.assert_called_once_with(host="127.0.0.1", port=9123)
