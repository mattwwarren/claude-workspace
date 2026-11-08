"""Tests for the five ``review.debt_*`` events (#3041, R16)."""

from __future__ import annotations

import logging

import pytest

from cw.events import read_events
from cw.models import OrchestratorEventType
from cw.review_debt_filing import emit_verify_failed
from cw.review_debt_filing._events import (
    emit_deduped,
    emit_emitted,
    emit_filed,
    emit_filing_failed,
)
from tests._review_debt_helpers import DEBT_TICKET, _debt_ctx, _events_of

_COMMON = {
    "ticket_id": DEBT_TICKET,
    "client": "acme",
    "branch": "dev/3041",
    "key": "k1",
    "tracker": "github-issues",
}


def test_each_event_carries_the_common_keys_and_its_extras() -> None:
    ctx = _debt_ctx()
    emit_filed(ctx, "k1", "100", "https://x/100")
    emit_deduped(ctx, "k1", "100", "marker")
    emit_filing_failed(ctx, "k1", "auth", "bad")
    emit_emitted(ctx, "k1", "/out/k1.json", "mcp")
    assert _events_of(OrchestratorEventType.REVIEW_DEBT_FILED) == [
        {**_COMMON, "tracker_id": "100", "url": "https://x/100", "mode": "filed"}
    ]
    assert _events_of(OrchestratorEventType.REVIEW_DEBT_DEDUPED) == [
        {**_COMMON, "tracker_id": "100", "via": "marker"}
    ]
    assert _events_of(OrchestratorEventType.REVIEW_DEBT_FILING_FAILED) == [
        {**_COMMON, "tracker_id": None, "failure": "auth", "detail": "bad"}
    ]
    assert _events_of(OrchestratorEventType.REVIEW_DEBT_EMITTED) == [
        {**_COMMON, "tracker_id": None, "path": "/out/k1.json", "cause": "mcp"}
    ]


def test_events_correlate_to_the_queue_ticket() -> None:
    emit_filed(_debt_ctx(), "k1", "100", "u")
    assert [e.correlation_id for e in read_events()] == [DEBT_TICKET]


def test_verify_failed_is_one_event_per_run() -> None:
    emit_verify_failed(
        _debt_ctx(tracker="linear"), [("k1", "malformed_id"), (None, "not_json")]
    )
    assert _events_of(OrchestratorEventType.REVIEW_DEBT_VERIFY_FAILED) == [
        {
            "ticket_id": DEBT_TICKET,
            "client": "acme",
            "branch": "dev/3041",
            "tracker": "linear",
            "count": 2,
            "failures": [
                {"key": "k1", "reason": "malformed_id"},
                {"key": None, "reason": "not_json"},
            ],
        }
    ]


def test_a_failed_event_write_logs_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("cw.review_debt_filing._events.record_event", _boom)
    with caplog.at_level(logging.WARNING):
        emit_filed(_debt_ctx(), "k1", "100", "u")
    assert "disk full" in caplog.text
