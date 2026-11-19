"""Tests for src/cw/codex_review_signals.py (#3055, RFC 0018 S3)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import pytest

from cw import codex_review_signals
from cw.auto_dev_result import OperatorConstraintsSource
from cw.codex_review_signals import (
    UNDELIVERED_OPERATOR_CONTEXT,
    VerdictCommentKind,
    constraints_source,
    emit_confirm_escalated,
    emit_verdict_comment_skipped,
    operator_context_undelivered_payload,
    tracker_label,
    undelivered_operator_context,
    verdict_comment_skipped_payload,
)
from cw.events import read_events
from cw.exceptions import CwError
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
from tests._doctor_helpers import make_client
from tests._shared.cli_preflight import _write_tracker_config

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
    def test_plan_kind_is_accepted(self, tmp_path: Path) -> None:
        """#3060: the codex PLAN post reuses the event with kind=plan."""
        payload = verdict_comment_skipped_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="plan",
            artifact_path=tmp_path / ".cw" / "plan.md",
            reviewed_sha=None,
            session_id="s-1",
            reason="plan_refused",
        )
        assert payload["kind"] == "plan"
        assert payload["reviewed_sha"] is None
        assert payload["reason"] == "plan_refused"

    def test_payload_fields(self, tmp_path: Path) -> None:
        payload = verdict_comment_skipped_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="unparseable",
            artifact_path=tmp_path / "a.md",
            reviewed_sha="",
            session_id="s-1",
            reason="outbox_enqueue_failed",
        )

        assert payload == {
            "ticket_id": "GEN-9",
            "client": "acme",
            "tracker": "linear",
            "backend": "codex",
            "kind": "unparseable",
            "reason": "outbox_enqueue_failed",
            "artifact_path": str(tmp_path / "a.md"),
            "reviewed_sha": None,
            "session_id": "s-1",
            "entry_id": None,
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
            reason="outbox_enqueue_failed",
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
                reason="outbox_enqueue_failed",
            )

        (record,) = caplog.records
        assert "GEN-9" in record.getMessage()
        assert "review.verdict_comment_skipped" in record.getMessage()

    def test_emit_swallows_cwerror(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("cw.codex_review_signals.record_event", side_effect=CwError("x")),
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
                reason="outbox_enqueue_failed",
            )

        (record,) = caplog.records
        assert "GEN-9" in record.getMessage()
        assert "review.verdict_comment_skipped" in record.getMessage()


class TestVerdictCommentSkippedReason:
    """The producer names why nothing was enqueued."""

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
            "entry_id",
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


class TestSkippedEntryId:
    def test_skipped_payload_carries_entry_id_last(self) -> None:
        payload = verdict_comment_skipped_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="verdict",
            artifact_path=None,
            reviewed_sha="abc",
            session_id="s-1",
            reason="already_posted",
            entry_id="e-1",
        )

        assert list(payload)[-2:] == ["session_id", "entry_id"]
        assert payload["entry_id"] == "e-1"

    def test_entry_id_defaults_none(self) -> None:
        payload = verdict_comment_skipped_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="verdict",
            artifact_path=None,
            reviewed_sha=None,
            session_id="s-1",
            reason="outbox_enqueue_failed",
        )

        assert payload["entry_id"] is None

    def test_emit_forwards_the_entry_id(self) -> None:
        emit_verdict_comment_skipped(
            ticket_id="GEN-9",
            client="acme",
            tracker="linear",
            kind="verdict",
            artifact_path=None,
            reviewed_sha="abc",
            session_id="s-1",
            reason="already_posted",
            entry_id="e-7",
        )

        (event,) = _events(OrchestratorEventType.REVIEW_VERDICT_COMMENT_SKIPPED)
        assert event.payload["entry_id"] == "e-7"


class TestTrackerLabel:
    def test_unset_tracker_is_github_issues(self, tmp_path: Path) -> None:
        client = make_client(name="acme", workspace_path=tmp_path)
        assert tracker_label(client) == "github-issues"

    def test_github_tracker(self, tmp_path: Path) -> None:
        _write_tracker_config(tmp_path, "github-issues")
        client = make_client(name="acme", workspace_path=tmp_path)
        assert tracker_label(client) == "github-issues"

    def test_linear_tracker(self, tmp_path: Path) -> None:
        _write_tracker_config(tmp_path, "linear")
        client = make_client(name="acme", workspace_path=tmp_path)
        assert tracker_label(client) == "linear"


class TestRetiredEmittersAreGone:
    @pytest.mark.parametrize(
        "name",
        [
            "VerdictCommentIdentity",
            "NON_GITHUB_TRACKER_REASON",
            "emit_verdict_comment_posted",
            "emit_verdict_comment_unverified",
            "verdict_comment_posted_payload",
            "verdict_comment_unverified_payload",
            "posted_content_sha256",
            "surface_tracker_io_outcome",
            "_KIND_LOG_LABELS",
        ],
    )
    def test_retired_emitters_are_gone(self, name: str) -> None:
        assert not hasattr(codex_review_signals, name)


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


_CONFIRM_PAYLOAD: dict[str, object] = {
    "ticket_id": "GEN-9",
    "client": "acme",
    "reason": "confirm_must_fix",
    "round": 2,
    "reviewed_sha": "abc1234",
    "identities": ["MUST_FIX|src/a.py|bug"],
}


class TestEmitConfirmEscalated:
    """#3216: the codex peer of claude-native's ``announce_confirm_park`` event."""

    def test_records_the_given_payload_with_ticket_correlation(self) -> None:
        emit_confirm_escalated(_CONFIRM_PAYLOAD, ticket_id="GEN-9")

        (event,) = _events(OrchestratorEventType.REVIEW_CONFIRM_ESCALATED)
        assert event.correlation_id == "GEN-9"
        assert event.payload == _CONFIRM_PAYLOAD

    def test_swallows_oserror_with_one_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("cw.codex_review_signals.record_event", side_effect=OSError("x")),
            caplog.at_level(logging.WARNING, logger="cw.codex_review_signals"),
        ):
            emit_confirm_escalated(_CONFIRM_PAYLOAD, ticket_id="GEN-9")

        (record,) = caplog.records
        message = record.getMessage()
        assert message.startswith("codex_review_signal_failed")
        assert "GEN-9" in message
        assert "review.confirm_escalated" in message


class TestPrLinkKind:
    """#3062: the codex FINALIZE PR-link comment reuses the three events."""

    def test_payload_carries_the_pr_link_kind(self) -> None:
        payload = verdict_comment_skipped_payload(
            ticket_id="GEN-9",
            client="acme",
            tracker="github-issues",
            kind="pr_link",
            artifact_path=None,
            reviewed_sha=None,
            session_id="s-1",
            reason="outbox_enqueue_failed",
        )
        assert payload["kind"] == "pr_link"
        assert payload["reviewed_sha"] is None


_EVENTS_DOC = Path(__file__).resolve().parent.parent / "docs" / "events.md"
_KIND_ENUMERATION = re.compile(r'"kind": "([a-z_| ]+)"')


class TestEventsDocEnumeratesEveryKind:
    """The three verdict-comment events list every ``VerdictCommentKind``."""

    def test_each_kind_enumeration_names_every_member(self) -> None:
        text = _EVENTS_DOC.read_text(encoding="utf-8")
        enumerations = [
            {part.strip() for part in match.split("|")}
            for match in _KIND_ENUMERATION.findall(text)
            if "|" in match and "verdict" in match
        ]
        assert len(enumerations) == 3
        members = set(get_args(VerdictCommentKind))
        for enumeration in enumerations:
            assert members <= enumeration, members - enumeration
