"""Unit tests for cw.reconcile.liveness_page (#2153).

Covers the pure page-evidence helpers the liveness sweep and doctor class-8
share: the last-record summarizer, the dedup evidence key, the two operator
commands, the breadcrumb suffix builder, and the additive payload fields.
Sweep-level behavior (once-per-evidence-key paging) lives in
``tests/test_reconcile_liveness.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cw.models import QueueItemStatus, Session, TicketTask
from cw.reconcile.liveness_page import (
    LastRecordSummary,
    close_command,
    compute_evidence_key,
    evidence_payload_fields,
    format_evidence_suffix,
    requeue_command,
    summarize_last_transcript_record,
)
from tests._reconcile_helpers import (
    _API_ERROR_TEXT,
    _api_error_then_cost_state_records,
    _ul_record,
    _write_transcript_records,
)
from tests.conftest import _make_daemon_session

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_ERROR_TS = datetime(2026, 1, 1, 23, 10, 0, tzinfo=UTC)
_COST_TS = datetime(2026, 1, 1, 23, 11, 0, tzinfo=UTC)
_LEAD = "; evidence suggests this session is dead (confirm before closing)"
_CLOSE = "cw spawn close --confirmed-dead sess-1"
_REQUEUE = "cw dev-queue requeue T-1 -c client-a --from-cancelled"
_REMEDY = f"; if you have confirmed the session is dead, run: {_CLOSE} then: {_REQUEUE}"


@pytest.fixture
def home() -> Path:
    """The per-test HOME (``_isolate_home``) transcript lookup searches."""
    return Path.home()


def _session(tmp_path: Path) -> Session:
    return _make_daemon_session(
        surface_ref="fake-short-id",
        worktree_path=tmp_path / "wt",
        started_at=_STARTED_AT,
    )


def _task(session_id: str | None, status: QueueItemStatus) -> TicketTask:
    return TicketTask(
        ticket_id="T-1", client="client-a", status=status, session_id=session_id
    )


def _write(home: Path, sess: Session, records: list[dict[str, object]]) -> Path:
    assert sess.worktree_path is not None
    return _write_transcript_records(home, sess.worktree_path, records)


# ---------------------------------------------------------------------------
# summarize_last_transcript_record
# ---------------------------------------------------------------------------


class TestSummarizeLastTranscriptRecord:
    def test_api_error_then_cost_state(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        _write(home, sess, _api_error_then_cost_state_records(_ERROR_TS, _COST_TS))

        summary = summarize_last_transcript_record(sess)

        assert summary == LastRecordSummary(
            record_type="cost-state",
            timestamp=_COST_TS,
            is_error=True,
            snippet=_API_ERROR_TEXT,
        )

    def test_last_record_normal_assistant(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        _write(home, sess, [_ul_record("all good", _ERROR_TS.isoformat())])

        summary = summarize_last_transcript_record(sess)

        assert summary is not None
        assert summary.record_type == "assistant"
        assert summary.is_error is False
        assert summary.snippet is None

    def test_user_record_after_error_is_not_error(
        self, tmp_path: Path, home: Path
    ) -> None:
        sess = _session(tmp_path)
        user = {
            "type": "user",
            "timestamp": _COST_TS.isoformat(),
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        }
        _write(home, sess, [_ul_record(_API_ERROR_TEXT, _ERROR_TS.isoformat()), user])

        summary = summarize_last_transcript_record(sess)

        assert summary is not None
        assert summary.record_type == "user"
        assert summary.is_error is False
        assert summary.snippet is None

    def test_cost_state_after_normal_assistant_is_not_error(
        self, tmp_path: Path, home: Path
    ) -> None:
        sess = _session(tmp_path)
        _write(
            home,
            sess,
            [
                _ul_record("done", _ERROR_TS.isoformat()),
                {"type": "cost-state", "timestamp": _COST_TS.isoformat()},
            ],
        )

        summary = summarize_last_transcript_record(sess)

        assert summary is not None
        assert summary.record_type == "cost-state"
        assert summary.is_error is False

    def test_record_without_timestamp(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        _write(home, sess, [_ul_record("no stamp")])

        summary = summarize_last_transcript_record(sess)

        assert summary is not None
        assert summary.timestamp is None

    def test_missing_transcript_returns_none(self, tmp_path: Path) -> None:
        assert summarize_last_transcript_record(_session(tmp_path)) is None

    def test_locate_oserror_returns_none(self, tmp_path: Path) -> None:
        with patch(
            "cw.reconcile.liveness_page._locate_session_transcript",
            side_effect=OSError("boom"),
        ):
            assert summarize_last_transcript_record(_session(tmp_path)) is None

    def test_unopenable_transcript_returns_none(
        self, tmp_path: Path, home: Path
    ) -> None:
        """A directory matching the transcript glob yields no records."""
        sess = _session(tmp_path)
        path = _write(home, sess, [_ul_record("x")])
        path.unlink()
        path.mkdir()

        assert summarize_last_transcript_record(sess) is None

    def test_malformed_line_is_skipped(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        path = _write(home, sess, [])
        lines = [
            json.dumps(_ul_record(_API_ERROR_TEXT, _ERROR_TS.isoformat())),
            "{not json",
            json.dumps({"type": "cost-state", "timestamp": _COST_TS.isoformat()}),
        ]
        path.write_text("\n".join(lines) + "\n")

        summary = summarize_last_transcript_record(sess)

        assert summary is not None
        assert summary.record_type == "cost-state"
        assert summary.is_error is True

    def test_error_snippet_is_redacted(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        secret = "c" * 40
        text = f"API Error: 401 Authorization: {secret}"
        _write(home, sess, [_ul_record(text, _ERROR_TS.isoformat())])

        summary = summarize_last_transcript_record(sess)

        assert summary is not None
        assert summary.snippet is not None
        assert secret not in summary.snippet
        assert "<redacted>" in summary.snippet

    def test_error_snippet_is_truncated(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        text = "API Error: " + "x" * 200
        _write(home, sess, [_ul_record(text, _ERROR_TS.isoformat())])

        summary = summarize_last_transcript_record(sess)

        assert summary is not None
        assert summary.snippet is not None
        assert summary.snippet.endswith("…")
        assert len(summary.snippet) == 81


# ---------------------------------------------------------------------------
# compute_evidence_key
# ---------------------------------------------------------------------------


class TestComputeEvidenceKey:
    def test_literal_format(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        _write(home, sess, _api_error_then_cost_state_records(_ERROR_TS, _COST_TS))

        key = compute_evidence_key(
            sess,
            paused_status="session_unresponsive",
            task=_task(sess.id, QueueItemStatus.RUNNING),
            session_age=False,
        )

        assert key == f"session_unresponsive|{_ERROR_TS.isoformat()}|running"

    def test_stable_across_metadata_only_append(
        self, tmp_path: Path, home: Path
    ) -> None:
        sess = _session(tmp_path)
        records = _api_error_then_cost_state_records(_ERROR_TS, _COST_TS)
        _write(home, sess, records)
        before = compute_evidence_key(
            sess, paused_status="s", task=None, session_age=False
        )

        later = (_COST_TS + timedelta(minutes=30)).isoformat()
        _write(home, sess, [*records, {"type": "cost-state", "timestamp": later}])
        after = compute_evidence_key(
            sess, paused_status="s", task=None, session_age=False
        )

        assert before == after

    def test_changes_with_paused_status(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        _write(home, sess, _api_error_then_cost_state_records(_ERROR_TS, _COST_TS))

        first = compute_evidence_key(
            sess, paused_status="a", task=None, session_age=False
        )
        second = compute_evidence_key(
            sess, paused_status="b", task=None, session_age=False
        )

        assert first != second

    def test_changes_with_new_content_record(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        records = _api_error_then_cost_state_records(_ERROR_TS, _COST_TS)
        _write(home, sess, records)
        before = compute_evidence_key(
            sess, paused_status="s", task=None, session_age=False
        )

        later = (_COST_TS + timedelta(minutes=5)).isoformat()
        _write(home, sess, [*records, _ul_record("retrying", later)])
        after = compute_evidence_key(
            sess, paused_status="s", task=None, session_age=False
        )

        assert before != after
        assert after.split("|")[1] == later

    def test_changes_with_task_status(self, tmp_path: Path, home: Path) -> None:
        sess = _session(tmp_path)
        _write(home, sess, _api_error_then_cost_state_records(_ERROR_TS, _COST_TS))

        running = compute_evidence_key(
            sess,
            paused_status="s",
            task=_task(sess.id, QueueItemStatus.RUNNING),
            session_age=False,
        )
        blocked = compute_evidence_key(
            sess,
            paused_status="s",
            task=_task(sess.id, QueueItemStatus.BLOCKED_ON_USER),
            session_age=False,
        )

        assert running.endswith("|running")
        assert blocked.endswith("|blocked_on_user")

    @pytest.mark.parametrize("owner", [None, "other-session"])
    def test_task_leg_none_without_owned_task(
        self, tmp_path: Path, home: Path, owner: str | None
    ) -> None:
        sess = _session(tmp_path)
        _write(home, sess, _api_error_then_cost_state_records(_ERROR_TS, _COST_TS))
        task = None if owner is None else _task(owner, QueueItemStatus.RUNNING)

        key = compute_evidence_key(
            sess, paused_status="s", task=task, session_age=False
        )

        assert key.endswith("|none")

    def test_session_age_skips_transcript_timestamp(self, tmp_path: Path) -> None:
        widened = MagicMock()
        with patch("cw.reconcile.liveness_page._widened_transcript_timestamp", widened):
            key = compute_evidence_key(
                _session(tmp_path),
                paused_status="session_unresponsive",
                task=None,
                session_age=True,
            )

        assert key == "session_unresponsive|none|none"
        widened.assert_not_called()

    def test_widened_oserror_gives_none_leg(self, tmp_path: Path) -> None:
        with patch(
            "cw.reconcile.liveness_page._widened_transcript_timestamp",
            side_effect=OSError("boom"),
        ):
            key = compute_evidence_key(
                _session(tmp_path), paused_status="s", task=None, session_age=False
            )

        assert key == "s|none|none"

    def test_no_transcript_gives_none_leg(self, tmp_path: Path) -> None:
        key = compute_evidence_key(
            _session(tmp_path), paused_status="s", task=None, session_age=False
        )

        assert key == "s|none|none"


# ---------------------------------------------------------------------------
# Operator commands
# ---------------------------------------------------------------------------


class TestCommands:
    def test_close_command_exact(self) -> None:
        assert close_command("abc123") == "cw spawn close --confirmed-dead abc123"

    def test_requeue_command_substitutes_ticket_and_client(self) -> None:
        assert requeue_command("GEN-9", "acme") == (
            "cw dev-queue requeue GEN-9 -c acme --from-cancelled"
        )

    def test_requeue_command_none_without_ticket(self) -> None:
        assert requeue_command(None, "client-a") is None


# ---------------------------------------------------------------------------
# format_evidence_suffix
# ---------------------------------------------------------------------------


_ERROR_SUMMARY = LastRecordSummary(
    record_type="cost-state",
    timestamp=_COST_TS,
    is_error=True,
    snippet=_API_ERROR_TEXT,
)


class TestFormatEvidenceSuffix:
    def test_summary_form_exact(self) -> None:
        suffix = format_evidence_suffix(
            _ERROR_SUMMARY,
            session_id="sess-1",
            ticket_id="T-1",
            client="client-a",
            stale_minutes=50,
        )

        assert suffix == (
            f"{_LEAD}; last record cost-state at 2026-01-01T23:11:00+00:00 "
            f"(API error: {_API_ERROR_TEXT}); flat 0.8h{_REMEDY}"
        )

    def test_no_summary_form_exact(self) -> None:
        suffix = format_evidence_suffix(
            None,
            session_id="sess-1",
            ticket_id="T-1",
            client="client-a",
            stale_minutes=840,
        )

        assert suffix == f"{_LEAD}; flat 14.0h{_REMEDY}"

    def test_session_age_form_exact(self) -> None:
        suffix = format_evidence_suffix(
            None,
            session_id="sess-1",
            ticket_id="T-1",
            client="client-a",
            stale_minutes=50,
            session_age=True,
        )

        assert suffix == (
            f"{_LEAD}; unobserved for 0.8h (session age, no transcript){_REMEDY}"
        )

    def test_unknown_type_and_timestamp_without_error(self) -> None:
        summary = LastRecordSummary(
            record_type=None, timestamp=None, is_error=False, snippet=None
        )

        suffix = format_evidence_suffix(
            summary,
            session_id="sess-1",
            ticket_id="T-1",
            client="client-a",
            stale_minutes=50,
        )

        assert suffix == f"{_LEAD}; last record unknown at unknown; flat 0.8h{_REMEDY}"
        assert "API error" not in suffix

    def test_no_then_clause_without_ticket(self) -> None:
        suffix = format_evidence_suffix(
            None,
            session_id="sess-1",
            ticket_id=None,
            client="client-a",
            stale_minutes=50,
        )

        assert suffix == (
            f"{_LEAD}; flat 0.8h; if you have confirmed the session is dead, "
            f"run: {_CLOSE}"
        )
        assert "then:" not in suffix


# ---------------------------------------------------------------------------
# evidence_payload_fields
# ---------------------------------------------------------------------------


class TestEvidencePayloadFields:
    def test_keys_and_values(self) -> None:
        fields = evidence_payload_fields(
            _ERROR_SUMMARY,
            session_id="sess-1",
            ticket_id="T-1",
            client="client-a",
            evidence_key="k",
        )

        assert fields == {
            "last_record_type": "cost-state",
            "last_record_ts": _COST_TS.isoformat(),
            "last_record_is_error": True,
            "close_command": _CLOSE,
            "requeue_command": _REQUEUE,
            "evidence_key": "k",
        }

    def test_summary_none_nulls_last_record_keys(self) -> None:
        fields = evidence_payload_fields(
            None,
            session_id="sess-1",
            ticket_id="T-1",
            client="client-a",
            evidence_key="k",
        )

        assert fields["last_record_type"] is None
        assert fields["last_record_ts"] is None
        assert fields["last_record_is_error"] is None

    def test_record_without_timestamp_nulls_ts(self) -> None:
        summary = LastRecordSummary(
            record_type="assistant", timestamp=None, is_error=False, snippet=None
        )

        fields = evidence_payload_fields(
            summary,
            session_id="sess-1",
            ticket_id="T-1",
            client="client-a",
            evidence_key="k",
        )

        assert fields["last_record_ts"] is None
        assert fields["last_record_is_error"] is False

    def test_requeue_key_present_and_none_without_ticket(self) -> None:
        fields = evidence_payload_fields(
            None,
            session_id="sess-1",
            ticket_id=None,
            client="client-a",
            evidence_key="k",
        )

        assert "requeue_command" in fields
        assert fields["requeue_command"] is None
        assert fields["close_command"] == _CLOSE
