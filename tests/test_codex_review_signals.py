"""Tests for src/cw/codex_review_signals.py (#3055, RFC 0018 S3)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cw.auto_dev_result import OperatorConstraintsSource
from cw.codex_review_signals import (
    UNDELIVERED_OPERATOR_CONTEXT,
    constraints_source,
    emit_verdict_comment_skipped,
    emit_verdict_comment_unverified,
    operator_context_undelivered_payload,
    undelivered_operator_context,
    verdict_comment_skipped_payload,
    verdict_comment_unverified_payload,
)
from cw.events import read_events
from cw.models import (
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.models.operator_constraint import PLAN_ACTOR, OperatorConstraint
from cw.review_finding_dispositions import FindingDisposition
from cw.tracker import TrackerClass, TrackerKind

_GITHUB = TrackerKind(TrackerClass.GITHUB, "github-issues")
_LINEAR = TrackerKind(TrackerClass.OTHER, "linear")
_UNSET = TrackerKind(TrackerClass.UNSET, None)
_NOW = datetime(2026, 11, 10, tzinfo=UTC)


def _constraint(*, plan: bool = False) -> OperatorConstraint:
    if plan:
        return OperatorConstraint(
            text="keep it small",
            recorded_at=_NOW,
            actor=PLAN_ACTOR,
            round=1,
            kind="non_goal",
        )
    return OperatorConstraint(text="no new deps", recorded_at=_NOW, actor="op", round=1)


def _disposition() -> FindingDisposition:
    return FindingDisposition(
        outcome="REJECTED",
        rationale="false positive",
        recorded_at="2026-11-10T00:00:00Z",
        actor="op",
        reviewed_sha="abc1234",
        summary="s",
    )


def _task(*, constraints: int = 0, plan: int = 0, dispositions: int = 0) -> TicketTask:
    return TicketTask(
        ticket_id="GEN-9",
        client="acme",
        status=QueueItemStatus.RUNNING,
        stage=Stage.REVIEW,
        operator_constraints=[_constraint() for _ in range(constraints)]
        + [_constraint(plan=True) for _ in range(plan)],
        finding_dispositions={f"k{n}": _disposition() for n in range(dispositions)},
    )


def _events(event_type: OrchestratorEventType) -> list[OrchestratorEvent]:
    return read_events(consumer="test-3055-signals", event_types=[event_type])


class TestConstraintsSource:
    @pytest.mark.parametrize(
        ("has_comment", "row_entries", "tracker", "expected"),
        [
            (True, 2, _GITHUB, OperatorConstraintsSource.COMMENT_AND_ROW),
            (True, 0, _GITHUB, OperatorConstraintsSource.COMMENT),
            (False, 1, _GITHUB, OperatorConstraintsSource.ROW),
            (False, 0, _GITHUB, OperatorConstraintsSource.NONE),
            (False, 0, _LINEAR, OperatorConstraintsSource.TRACKER_UNREADABLE),
            (False, 0, _UNSET, OperatorConstraintsSource.TRACKER_UNREADABLE),
            (False, 3, _LINEAR, OperatorConstraintsSource.ROW),
            # Cannot occur (comments are unreadable there) but pins totality.
            (True, 0, _LINEAR, OperatorConstraintsSource.COMMENT),
            (True, 1, _UNSET, OperatorConstraintsSource.COMMENT_AND_ROW),
        ],
    )
    def test_table(
        self,
        has_comment: bool,
        row_entries: int,
        tracker: TrackerKind,
        expected: OperatorConstraintsSource,
    ) -> None:
        assert (
            constraints_source(
                has_comment=has_comment, row_entries=row_entries, tracker=tracker
            )
            is expected
        )


class TestVerdictCommentSkipped:
    def test_payload_fields(self, tmp_path: Path) -> None:
        payload = verdict_comment_skipped_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="unparseable",
            artifact_path=tmp_path / "a.md",
            reviewed_sha="",
            session_id="s-1",
        )

        assert payload == {
            "ticket_id": "GEN-9",
            "client": "acme",
            "tracker": "linear",
            "backend": "codex",
            "kind": "unparseable",
            "reason": "non_github_tracker",
            "artifact_path": str(tmp_path / "a.md"),
            "reviewed_sha": None,
            "session_id": "s-1",
        }
        json.dumps(payload)

    def test_emit_records_with_ticket_correlation(self) -> None:
        emit_verdict_comment_skipped(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="verdict",
            artifact_path=None,
            reviewed_sha="abc",
            session_id="s-1",
        )

        (event,) = _events(OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED)
        assert event.correlation_id == "GEN-9"
        assert event.payload["reviewed_sha"] == "abc"
        assert event.payload["artifact_path"] is None

    def test_emit_swallows_oserror(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("cw.codex_review_signals.record_event", side_effect=OSError("x")),
            caplog.at_level(logging.WARNING, logger="cw.codex_review_signals"),
        ):
            emit_verdict_comment_skipped(
                ticket_id="GEN-9",
                client="acme",
                tracker="linear",
                kind="verdict",
                artifact_path=None,
                reviewed_sha=None,
                session_id="s-1",
            )

        (record,) = caplog.records
        assert "GEN-9" in record.getMessage()
        assert "review.verdict_comment_skipped" in record.getMessage()


class TestVerdictCommentSkippedReason:
    """#3057: the tracker-IO turn names why nothing was posted."""

    def test_reason_override_keeps_key_order(self) -> None:
        payload = verdict_comment_skipped_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="verdict",
            artifact_path=None,
            reviewed_sha="abc",
            session_id="s-1",
            reason="tracker_mcp_not_enabled",
        )

        assert list(payload) == [
            "ticket_id",
            "client",
            "tracker",
            "backend",
            "kind",
            "reason",
            "artifact_path",
            "reviewed_sha",
            "session_id",
        ]
        assert payload["reason"] == "tracker_mcp_not_enabled"

    def test_emit_forwards_the_reason(self) -> None:
        emit_verdict_comment_skipped(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="verdict",
            artifact_path=None,
            reviewed_sha="abc",
            session_id="s-1",
            reason="mcp_server_unnamed",
        )

        (event,) = _events(OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED)
        assert event.payload["reason"] == "mcp_server_unnamed"


_POSTED = [{"index": 1, "comment_id": "c-1", "url": "https://linear.app/x#c-1"}]


class TestVerdictCommentUnverified:
    def test_payload_fields_in_order(self, tmp_path: Path) -> None:
        payload = verdict_comment_unverified_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="verdict",
            reason="readback_mismatch",
            artifact_path=tmp_path / "a.md",
            reviewed_sha="abc",
            session_id="s-1",
            public_state="confirmed",
            posted=_POSTED,
            marker_survived=False,
        )

        assert list(payload.items()) == [
            ("ticket_id", "GEN-9"),
            ("client", "acme"),
            ("tracker", "linear"),
            ("backend", "codex"),
            ("kind", "verdict"),
            ("reason", "readback_mismatch"),
            ("artifact_path", str(tmp_path / "a.md")),
            ("reviewed_sha", "abc"),
            ("session_id", "s-1"),
            ("public_state", "confirmed"),
            ("posted", _POSTED),
            ("marker_survived", False),
        ]
        assert payload["posted"] is not _POSTED
        json.dumps(payload)

    def test_emit_records_with_ticket_correlation(self, tmp_path: Path) -> None:
        emit_verdict_comment_unverified(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="unparseable",
            reason="turn_timeout",
            artifact_path=None,
            reviewed_sha=None,
            session_id="s-1",
            public_state="possible",
            posted=[],
            marker_survived=None,
        )

        (event,) = _events(OrchestratorEventType.REVIEW_VERDICT_COMMENT_UNVERIFIED)
        assert event.correlation_id == "GEN-9"
        assert event.payload["public_state"] == "possible"
        assert event.payload["marker_survived"] is None
        assert tmp_path.exists()

    def test_emit_swallows_oserror(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("cw.codex_review_signals.record_event", side_effect=OSError("x")),
            caplog.at_level(logging.WARNING, logger="cw.codex_review_signals"),
        ):
            emit_verdict_comment_unverified(
                ticket_id="GEN-9",
                client="acme",
                tracker="linear",
                kind="verdict",
                reason="ids_count_mismatch",
                artifact_path=None,
                reviewed_sha=None,
                session_id="s-1",
                public_state="confirmed",
                posted=_POSTED,
                marker_survived=True,
            )

        (record,) = caplog.records
        assert "GEN-9" in record.getMessage()
        assert "review.verdict_comment_unverified" in record.getMessage()


class TestOperatorContextUndelivered:
    def test_payload_counts_all_row_entries(self) -> None:
        task = _task(constraints=1, plan=2, dispositions=3)

        payload = operator_context_undelivered_payload(
            task=task, client="acme", tracker=_LINEAR, session_id="s-1"
        )

        assert list(payload.items()) == [
            ("ticket_id", "GEN-9"),
            ("client", "acme"),
            ("tracker", "linear"),
            ("backend", "codex"),
            ("undelivered", ["operator_comments", "voided_findings", "settle_markers"]),
            ("operator_constraints_source", "row"),
            ("row_constraints", 3),
            ("row_dispositions", 3),
            ("session_id", "s-1"),
        ]
        json.dumps(payload)

    def test_plan_derived_only_row_is_row(self) -> None:
        payload = operator_context_undelivered_payload(
            task=_task(plan=1), client="acme", tracker=_LINEAR, session_id="s-1"
        )

        assert payload["operator_constraints_source"] == "row"

    def test_unset_tracker_is_labelled_and_unreadable(self) -> None:
        payload = operator_context_undelivered_payload(
            task=_task(), client="acme", tracker=_UNSET, session_id="s-1"
        )

        assert payload["tracker"] == "unset"
        assert payload["operator_constraints_source"] == "tracker_unreadable"
        assert payload["row_constraints"] == 0

    def test_undelivered_list_is_a_fresh_copy(self) -> None:
        payload = operator_context_undelivered_payload(
            task=_task(), client="acme", tracker=_LINEAR, session_id="s-1"
        )

        assert payload["undelivered"] == list(UNDELIVERED_OPERATOR_CONTEXT)
        assert payload["undelivered"] is not UNDELIVERED_OPERATOR_CONTEXT

    @pytest.mark.parametrize("tracker", [_LINEAR, _UNSET])
    def test_emits_on_non_github(self, tracker: TrackerKind) -> None:
        undelivered_operator_context(
            task=_task(), client="acme", tracker=tracker, session_id="s-1"
        )

        (event,) = _events(OrchestratorEventType.REVIEW_OPERATOR_CONTEXT_UNDELIVERED)
        assert event.correlation_id == "GEN-9"

    def test_github_is_a_no_op(self) -> None:
        undelivered_operator_context(
            task=_task(), client="acme", tracker=_GITHUB, session_id="s-1"
        )

        assert _events(OrchestratorEventType.REVIEW_OPERATOR_CONTEXT_UNDELIVERED) == []

    def test_swallows_oserror(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("cw.codex_review_signals.record_event", side_effect=OSError("x")),
            caplog.at_level(logging.WARNING, logger="cw.codex_review_signals"),
        ):
            undelivered_operator_context(
                task=_task(), client="acme", tracker=_LINEAR, session_id="s-1"
            )

        (record,) = caplog.records
        assert "review.operator_context_undelivered" in record.getMessage()
