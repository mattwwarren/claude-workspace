"""Tests for ``cw.queue_peek.rows`` -- report rows for ``cw queue peek``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cw import queue_peek
from cw.models import QueueItemStatus, Stage, TicketTask
from cw.queue_peek import locate
from tests._opencode_helpers import write_opencode_log
from tests._queue_peek_helpers import (
    NOW,
    make_ticket_task,
    make_transcript,
    redirect_peek_paths,
    sentinel_payload,
    sentinel_text,
    write_sessions,
)


@pytest.fixture
def patched_peek(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``locate.CLAUDE_PROJECTS`` and ``locate.CW_STATE`` to tmp_path."""
    redirect_peek_paths(monkeypatch, tmp_path)


class TestMinutesSince:
    def test_returns_none_for_none_input(self) -> None:
        assert queue_peek.minutes_since(None, NOW) is None

    def test_returns_none_for_invalid_timestamp(self) -> None:
        assert queue_peek.minutes_since("not-a-date", NOW) is None

    def test_computes_correct_minutes(self) -> None:
        ts = "2026-06-20T11:50:00+00:00"  # 10 minutes before NOW
        result = queue_peek.minutes_since(ts, NOW)
        assert result == pytest.approx(10.0)

    def test_returns_zero_for_current_time(self) -> None:
        ts = NOW.isoformat()
        result = queue_peek.minutes_since(ts, NOW)
        assert result == pytest.approx(0.0)

    def test_naive_timestamp_coerced_to_utc(self) -> None:
        """A tz-less ISO string (e.g. from CW_STATE started_at) is coerced to
        UTC rather than raising, mirroring _parse_started_at's coercion."""
        ts = "2026-06-20T11:50:00"  # no offset — 10 minutes before NOW
        result = queue_peek.minutes_since(ts, NOW)
        assert result == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# format_row
# ---------------------------------------------------------------------------


class TestFormatRow:
    def test_returns_all_required_keys(self) -> None:
        task = make_ticket_task(
            ticket_id="T-1", session_id="sess1234", client="acme", attempts=2
        )
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        with patch("cw.queue_peek.rows.gh_pr_state", return_value="OPEN"):
            row = queue_peek.format_row(task, info, NOW)
        required_keys = {
            "ticket",
            "session",
            "client",
            "attempts",
            "unproductive_attempts",
            "age_min",
            "idle_min",
            "stage",
            "status",
            "pr",
            "pr_state",
            "recommend",
            "reason",
            "signal_source",
            "jsonl_idle_min",
            "stage_high_water",
            "pipeline_stage",
        }
        assert required_keys == set(row.keys())

    def test_attempts_and_unproductive_attempts_are_independently_visible(
        self,
    ) -> None:
        """attempts (raw claim count) and unproductive_attempts (the STOP
        ladder's signal) are both surfaced on the row, and can diverge."""
        task = make_ticket_task(attempts=9, unproductive_attempts=2)
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["attempts"] == 9
        assert row["unproductive_attempts"] == 2

    def test_session_truncated_to_12_chars(self) -> None:
        task = make_ticket_task(session_id="abcdef1234567890")
        row = queue_peek.format_row(task, {}, NOW)
        assert row["session"] == "abcdef123456"

    def test_none_session_id_becomes_dash(self) -> None:
        task = make_ticket_task(session_id=None)
        row = queue_peek.format_row(task, {}, NOW)
        assert row["session"] == "-"

    def test_no_pr_number_skips_gh_call(self) -> None:
        task = make_ticket_task()
        info: dict[str, Any] = {"last_pr_number": None}
        with patch("cw.queue_peek.rows.gh_pr_state") as mock_gh:
            queue_peek.format_row(task, info, NOW)
        mock_gh.assert_not_called()

    def test_pr_number_triggers_gh_call(self) -> None:
        task = make_ticket_task()
        info: dict[str, Any] = {"last_pr_number": 99}
        with patch("cw.queue_peek.rows.gh_pr_state", return_value="OPEN") as mock_gh:
            row = queue_peek.format_row(task, info, NOW)
        mock_gh.assert_called_once_with(99)
        assert row["pr_state"] == "OPEN"

    def test_stage_high_water_present_in_row(self) -> None:
        task = make_ticket_task(stage_high_water=Stage.REVIEW)
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["stage_high_water"] == Stage.REVIEW

    def test_pipeline_stage_present_in_row(self) -> None:
        # TicketTask.stage is non-Optional (typed Stage, default Stage.PLAN),
        # so pipeline_stage is never null in practice here — this exercises
        # the queue-side value distinct from the sentinel-derived "stage" key.
        task = make_ticket_task(stage=Stage.FINALIZE)
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["pipeline_stage"] == Stage.FINALIZE
        assert row["stage"] is None

    def test_stage_and_stage_high_water_unchanged_by_pipeline_stage(self) -> None:
        """Regression: adding pipeline_stage must not alter the existing
        "stage" (sentinel-derived) or "stage_high_water" values or semantics.
        """
        task = make_ticket_task(stage_high_water=Stage.REVIEW, stage=Stage.FINALIZE)
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["stage_high_water"] == Stage.REVIEW
        assert row["stage_high_water"] == task.stage_high_water
        assert row["stage"] is None

    def test_age_min_uses_claim_started_at_when_present(self) -> None:
        """claim_started_at (Session.started_at) is the trustworthy age
        anchor — the far-off first_user_ts is ignored when it's present."""
        task = make_ticket_task()
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:55:00+00:00",  # 5 min before NOW
            "first_user_ts": "2026-06-20T10:00:00+00:00",  # 2 hours before NOW
            "last_asst_ts": None,
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["age_min"] == pytest.approx(5.0)

    def test_age_min_falls_back_to_first_user_ts_without_claim_started_at(
        self,
    ) -> None:
        """No claim_started_at in info → existing first_user_ts-derived
        behavior is unchanged (locks in backward compatibility)."""
        task = make_ticket_task()
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",  # 10 min before NOW
            "last_asst_ts": None,
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["age_min"] == pytest.approx(10.0)

    def test_idle_min_nulled_when_inconsistent_with_claim_anchored_age(self) -> None:
        """Reproduces the ticket's false-STOP shape: claim_started_at is
        seconds old but last_asst_ts is ~99 minutes stale (leftover
        prior-session transcript on a reused worktree) — idle_min is nulled
        and the row must not recommend STOP/STOP-OR-PEEK."""
        task = make_ticket_task(attempts=1)
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:59:55+00:00",  # 5 sec before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T10:21:00+00:00",  # ~99 min before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["idle_min"] is None
        assert row["recommend"] not in {"STOP", "STOP-OR-PEEK"}

    def test_idle_min_kept_when_consistent_with_claim_anchored_age(self) -> None:
        """Healthy case: idle_min <= age_min → the transcript-derived value
        is unchanged."""
        task = make_ticket_task()
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:50:00+00:00",  # 10 min before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T11:58:00+00:00",  # 2 min before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["idle_min"] == pytest.approx(2.0)

    def test_idle_clamp_skipped_without_claim_started_at(self) -> None:
        """No claim data at all → an idle value that looks inconsistent
        against first_user_ts is NOT nulled (degraded/legacy path preserved,
        no false-positive invalidation)."""
        task = make_ticket_task()
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:59:00+00:00",  # 1 min before NOW
            "last_asst_ts": "2026-06-20T10:21:00+00:00",  # ~99 min before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["idle_min"] is not None


class TestSubagentIdleBlend:
    """format_row's parent/child idle blend (#2028)."""

    def test_healthy_parent_idle_child_active_rescues_idle_and_reason(self) -> None:
        """Shape A — #1976: parent idle_min ~15.8min (looks stuck), but a
        subagent is active seconds ago. The blended idle_min reflects the
        child's freshness and the ladder must not reach STOP/STOP-OR-PEEK;
        reason must name the rescuing child."""
        task = make_ticket_task(attempts=1)
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:30:00+00:00",  # 30 min before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T11:44:12+00:00",  # ~15.8 min before NOW
            "subagent_last_asst_ts": "2026-06-20T11:59:55+00:00",  # 5 sec before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["idle_min"] == pytest.approx(0.1, abs=0.05)
        assert row["recommend"] not in {"STOP", "STOP-OR-PEEK"}
        assert "child subagent active" in row["reason"]

    def test_wedged_parent_idle_child_also_idle_unchanged(self) -> None:
        """Shape B — #9: both parent and child are stale (~46.2min). The
        blend must not rescue idle_min or reason — this is a genuine stall."""
        task = make_ticket_task(attempts=1)
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:10:00+00:00",  # 50 min before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T11:13:48+00:00",  # ~46.2 min before NOW
            "subagent_last_asst_ts": "2026-06-20T11:00:00+00:00",  # ~60 min before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["idle_min"] == pytest.approx(46.2, abs=0.1)
        assert row["recommend"] == "STOP-OR-PEEK"
        assert "child subagent active" not in row["reason"]

    def test_no_subagents_byte_identical_to_pre_fix_row(self) -> None:
        """subagent_last_asst_ts absent → idle_min and reason unchanged."""
        task = make_ticket_task(attempts=1)
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:50:00+00:00",  # 10 min before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T11:58:00+00:00",  # 2 min before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row_without_key = queue_peek.format_row(task, info, NOW)
        row_with_none = queue_peek.format_row(
            task, {**info, "subagent_last_asst_ts": None}, NOW
        )
        assert row_without_key["idle_min"] == pytest.approx(2.0)
        assert "child subagent active" not in row_without_key["reason"]
        assert row_without_key == row_with_none

    def test_child_rescues_when_parent_idle_already_nulled_by_stale_guard(
        self,
    ) -> None:
        """Parent idle is nulled by the existing reused-worktree stale-guard
        (idle > age); a fresh subagent still rescues both idle_min and
        reason — a nulled parent idle counts as 'no signal', not 'healthy'."""
        task = make_ticket_task(attempts=1)
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:59:55+00:00",  # 5 sec before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T10:21:00+00:00",  # ~99 min before NOW
            "subagent_last_asst_ts": "2026-06-20T11:59:58+00:00",  # 2 sec before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["idle_min"] is not None
        assert row["idle_min"] < 1
        assert "child subagent active" in row["reason"]

    def test_active_child_suppresses_unproductive_attempts_stop(self) -> None:
        """#2028 comment 4, Instance 2 (session da8f1b20): age_m 5.1,
        idle_m 1.3, unproductive attempt 3 — STOP fired via the
        attempt-count arm even though a subagent had written 33s earlier.
        format_row must thread child_active into recommend() so this arm no
        longer fires while a child is demonstrably active."""
        task = make_ticket_task(attempts=3, unproductive_attempts=3)
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:54:54+00:00",  # ~5.1 min before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T11:58:42+00:00",  # ~1.3 min before NOW
            "subagent_last_asst_ts": "2026-06-20T11:59:27+00:00",  # 33 sec before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["recommend"] != "STOP"
        assert "systemic" not in row["reason"]
        assert "child subagent active" in row["reason"]

    def test_idle_child_does_not_suppress_unproductive_attempts_stop(self) -> None:
        """Regression guard: with no active child, the attempt-count STOP
        arm fires exactly as before — the #2028 gate must not blanket-
        suppress genuine systemic-failure STOPs."""
        task = make_ticket_task(
            attempts=3, unproductive_attempts=queue_peek.STOP_UNPRODUCTIVE_ATTEMPTS_MIN
        )
        info: dict[str, Any] = {
            "claim_started_at": "2026-06-20T11:50:00+00:00",  # 10 min before NOW
            "first_user_ts": None,
            "last_asst_ts": "2026-06-20T11:50:36+00:00",  # ~9.4 min before NOW
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["recommend"] == "STOP"
        assert "systemic" in row["reason"]


# ---------------------------------------------------------------------------
# build_peek_rows
# ---------------------------------------------------------------------------


class TestBuildPeekRows:
    def test_returns_empty_list_when_no_running_tasks(self) -> None:
        with patch("cw.queue_peek.rows.load_attention_tasks", return_value=[]):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert rows == []

    def test_builds_one_row_per_task(self) -> None:
        tasks = [make_ticket_task("T-1"), make_ticket_task("T-2")]
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=tasks),
            patch("cw.queue_peek.rows.find_transcript_for_ticket", return_value=None),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert len(rows) == 2

    def test_passes_worktree_path_to_find_transcript(self, tmp_path: Path) -> None:
        """build_peek_rows forwards task.worktree_path to find_transcript_for_ticket."""
        wt = tmp_path / "dev-999"
        task = make_ticket_task("999", worktree_path=wt)
        captured_wt: list[object] = []

        def _capture(
            ticket_id: str,
            session_id: object = None,
            worktree_path: object = None,
        ) -> None:
            captured_wt.append(worktree_path)

        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.rows.find_transcript_for_ticket", side_effect=_capture
            ),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            queue_peek.build_peek_rows(None, NOW)
        assert len(captured_wt) == 1
        assert captured_wt[0] == wt

    def test_client_filter_passed_to_load(self) -> None:
        with (
            patch(
                "cw.queue_peek.rows.load_attention_tasks", return_value=[]
            ) as mock_load,
        ):
            queue_peek.build_peek_rows("my-client", NOW)
        mock_load.assert_called_once_with("my-client")

    def test_transcript_found_sets_signal_source_transcript(
        self, tmp_path: Path
    ) -> None:
        """When transcript is found, row has signal_source=transcript."""
        task = make_ticket_task("T-1")
        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-06-20T11:50:00Z",
                    "message": {"content": "start"},
                }
            )
            + "\n"
        )
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.rows.find_transcript_for_ticket",
                return_value=transcript_path,
            ),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert len(rows) == 1
        assert rows[0]["signal_source"] == "transcript"
        assert rows[0]["jsonl_idle_min"] is None

    def test_threads_claim_started_at_from_session_refs(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """build_peek_rows threads Session.started_at (via CW_STATE) into
        info['claim_started_at'] so format_row anchors age on claim time."""
        task = make_ticket_task("T-1", session_id="sess1234")
        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-06-20T09:00:00Z",  # ~3h before NOW — stale
                    "message": {"content": "start"},
                }
            )
            + "\n"
        )
        write_sessions(
            locate.CW_STATE,
            [
                {
                    "id": "sess1234",
                    "claude_session_id": None,
                    "started_at": "2026-06-20T11:55:00+00:00",  # 5 min before NOW
                }
            ],
        )
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.rows.find_transcript_for_ticket",
                return_value=transcript_path,
            ),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert rows[0]["age_min"] == pytest.approx(5.0)

    def test_reused_worktree_stale_transcript_does_not_false_stop(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """Full regression for evidence (2): a project dir containing only an
        old jsonl (first/last timestamps ~100 min stale) plus a CW_STATE
        session claimed seconds ago, attempts=1 (otherwise healthy) — the row
        must not recommend STOP/STOP-OR-PEEK."""
        task = make_ticket_task("T-2", session_id="sess5678", attempts=1)
        transcript_path = tmp_path / "session.jsonl"
        make_transcript(
            transcript_path,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-20T10:21:00Z",  # ~99 min before NOW
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-20T10:21:36Z",  # ~98.9 min before NOW
                    "message": {"content": []},
                },
            ],
        )
        write_sessions(
            locate.CW_STATE,
            [
                {
                    "id": "sess5678",
                    "claude_session_id": None,
                    "started_at": "2026-06-20T11:59:55+00:00",  # 5 sec before NOW
                }
            ],
        )
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.rows.find_transcript_for_ticket",
                return_value=transcript_path,
            ),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert rows[0]["recommend"] not in {"STOP", "STOP-OR-PEEK"}

    def test_high_raw_attempts_low_unproductive_attempts_not_stopped(
        self, tmp_path: Path
    ) -> None:
        """The actual regression surface: format_row must wire t.attempts
        through to recommend()'s unproductive_attempts parameter using
        t.unproductive_attempts, not the raw claim counter — a healthy,
        all-productive #1727-shaped ticket (many raw attempts, zero
        unproductive ones) must not be recommended STOP end-to-end."""
        task = make_ticket_task("T-1", attempts=12, unproductive_attempts=0)
        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-06-20T11:50:00Z",  # 10 min before NOW — healthy
                    "message": {"content": "start"},
                }
            )
            + "\n"
        )
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.rows.find_transcript_for_ticket",
                return_value=transcript_path,
            ),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert rows[0]["recommend"] != "STOP"


class TestAwaitingOperatorRows:
    """#2212: a BLOCKED_ON_USER row is 'awaiting operator', not 'wedged'."""

    def _blocked_task(self, **overrides: object) -> TicketTask:
        kwargs: dict[str, object] = {
            "ticket_id": "2212",
            "client": "c",
            "status": QueueItemStatus.BLOCKED_ON_USER,
            "session_id": "abc12345",
            "attempts": 1,
        }
        kwargs.update(overrides)
        return TicketTask.model_validate(kwargs)

    def test_recommendation_is_awaiting_operator(self) -> None:
        row = queue_peek.format_row(self._blocked_task(), {}, NOW)
        assert row["recommend"] == queue_peek.RECOMMEND_AWAITING_OPERATOR
        assert row["recommend"] == "AWAITING_OPERATOR"

    def test_age_and_idle_ladder_is_skipped(self) -> None:
        """A parked row is *supposed* to be idle — no false-precision scores."""
        row = queue_peek.format_row(self._blocked_task(), {}, NOW)
        assert row["age_min"] is None
        assert row["idle_min"] is None

    def test_reason_prefers_disposition(self) -> None:
        """The primary park path (_route_stopped_without_sentinel) stamps
        ``disposition``, not ``blocked_reason``."""
        task = self._blocked_task(
            disposition="stopped_without_sentinel",
            blocked_reason="secondary",
            advisory_note="tertiary",
        )
        assert (
            "stopped_without_sentinel" in queue_peek.format_row(task, {}, NOW)["reason"]
        )

    def test_reason_falls_back_to_blocked_reason(self) -> None:
        task = self._blocked_task(
            blocked_reason="needs a product decision", advisory_note="tertiary"
        )
        assert (
            "needs a product decision" in queue_peek.format_row(task, {}, NOW)["reason"]
        )

    def test_reason_falls_back_to_advisory_note(self) -> None:
        task = self._blocked_task(advisory_note="session id mismatch")
        assert "session id mismatch" in queue_peek.format_row(task, {}, NOW)["reason"]

    def test_reason_falls_back_to_a_generic_string(self) -> None:
        reason = queue_peek.format_row(self._blocked_task(), {}, NOW)["reason"]
        assert reason
        assert "parked" in reason.lower()

    def test_blind_signal_source_does_not_shadow_the_parked_branch(self) -> None:
        row = queue_peek.format_row(
            self._blocked_task(), {"signal_source": "blind"}, NOW
        )
        assert row["recommend"] == queue_peek.RECOMMEND_AWAITING_OPERATOR

    def test_running_rows_are_unaffected(self) -> None:
        """Regression guard: the STOP-OR-PEEK bucket keeps its RUNNING rows."""
        task = make_ticket_task("T-1", unproductive_attempts=0)
        info = {
            "first_user_ts": "2026-06-20T11:30:00Z",
            "last_asst_ts": "2026-06-20T11:40:00Z",  # 20 min idle at NOW
            "last_sentinel_status": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        assert queue_peek.format_row(task, info, NOW)["recommend"] == "STOP-OR-PEEK"

    def test_build_peek_rows_reports_a_parked_row(self) -> None:
        with (
            patch(
                "cw.queue_peek.rows.load_attention_tasks",
                return_value=[self._blocked_task()],
            ),
            patch("cw.queue_peek.rows.find_transcript_for_ticket", return_value=None),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert len(rows) == 1
        assert rows[0]["recommend"] == queue_peek.RECOMMEND_AWAITING_OPERATOR

    def test_build_peek_rows_skips_transcript_lookup_for_a_parked_row(self) -> None:
        with (
            patch(
                "cw.queue_peek.rows.load_attention_tasks",
                return_value=[self._blocked_task()],
            ),
            patch("cw.queue_peek.rows.find_transcript_for_ticket") as mock_find,
        ):
            queue_peek.build_peek_rows(None, NOW)
        assert mock_find.call_count == 0


class TestBuildPeekRowsSubagents:
    """End-to-end regression for the #2028 subagent idle blend.

    The parent transcript path returned by ``find_transcript_for_ticket`` is
    deliberately named ``session.jsonl`` — NOT ``<csid>.jsonl`` — so this
    test only passes if the subagent lookup is scoped by the CW_STATE
    ``claude_session_id`` (via ``_load_session_refs``), not by
    ``transcript.stem``. A stem-keyed lookup would resolve to the wrong
    (nonexistent) subagents dir and silently find nothing.
    """

    _CSID = "aaaa1111-0000-0000-0000-000000000000"

    def test_fresh_subagent_rescues_idle_min_via_csid_not_transcript_stem(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        task = make_ticket_task("T-1", session_id="sess1234", attempts=1)
        transcript_path = tmp_path / "session.jsonl"
        make_transcript(
            transcript_path,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-20T11:30:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-20T11:44:12Z",  # ~15.8 min before NOW
                    "message": {"content": []},
                },
            ],
        )
        child_path = transcript_path.parent / self._CSID / "subagents" / "agent-x.jsonl"
        child_path.parent.mkdir(parents=True)
        make_transcript(
            child_path,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-20T11:59:55Z",  # 5 sec before NOW
                    "message": {"content": []},
                },
            ],
        )
        write_sessions(
            locate.CW_STATE,
            [
                {
                    "id": "sess1234",
                    "claude_session_id": self._CSID,
                    "started_at": "2026-06-20T11:30:00+00:00",  # 30 min before NOW
                }
            ],
        )
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.rows.find_transcript_for_ticket",
                return_value=transcript_path,
            ),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert rows[0]["idle_min"] is not None
        assert rows[0]["idle_min"] < 1
        assert "child subagent active" in rows[0]["reason"]

    def test_no_claude_session_id_leaves_idle_min_unchanged(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """A CW_STATE row with no claude_session_id has no <csid>/subagents/
        path to derive — the blend is skipped, not an error."""
        task = make_ticket_task("T-1", session_id="sess1234", attempts=1)
        transcript_path = tmp_path / "session.jsonl"
        make_transcript(
            transcript_path,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-20T11:50:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-20T11:58:00Z",  # 2 min before NOW
                    "message": {"content": []},
                },
            ],
        )
        write_sessions(
            locate.CW_STATE,
            [
                {
                    "id": "sess1234",
                    "claude_session_id": None,
                    "started_at": "2026-06-20T11:50:00+00:00",
                }
            ],
        )
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.rows.find_transcript_for_ticket",
                return_value=transcript_path,
            ),
            patch("cw.queue_peek.rows.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert rows[0]["idle_min"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# load_attention_tasks
# ---------------------------------------------------------------------------


class TestLoadAttentionTasks:
    def test_filters_out_pending_tasks(self) -> None:
        pending = TicketTask(
            ticket_id="T-1",
            client="c",
            status=QueueItemStatus.PENDING,
            session_id=None,
            attempts=0,
        )
        running = TicketTask(
            ticket_id="T-2",
            client="c",
            status=QueueItemStatus.RUNNING,
            session_id="abc",
            attempts=1,
        )
        with patch("cw.queue_peek.rows.list_tickets", return_value=[pending, running]):
            result = queue_peek.load_attention_tasks(None)
        assert len(result) == 1
        assert result[0].ticket_id == "T-2"

    def test_enumerates_blocked_on_user_alongside_running(self) -> None:
        """#2212: a parked row was invisible to ``cw queue peek`` before this."""
        running = TicketTask(
            ticket_id="T-2", client="c", status=QueueItemStatus.RUNNING, attempts=1
        )
        blocked = TicketTask(
            ticket_id="T-3",
            client="c",
            status=QueueItemStatus.BLOCKED_ON_USER,
            attempts=1,
        )
        with patch("cw.queue_peek.rows.list_tickets", return_value=[running, blocked]):
            result = queue_peek.load_attention_tasks(None)
        assert {t.ticket_id for t in result} == {"T-2", "T-3"}

    def test_signoff_parked_rows_are_not_included(self) -> None:
        """AWAITING_OPERATOR_SIGNOFF is a distinct RFC 0007 concept (#990) —
        a ticket parked pending ship-signoff, not an unanswered question."""
        signoff = TicketTask(
            ticket_id="T-4",
            client="c",
            status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
            attempts=1,
        )
        with patch("cw.queue_peek.rows.list_tickets", return_value=[signoff]):
            assert queue_peek.load_attention_tasks(None) == []


# ---------------------------------------------------------------------------
# gh_pr_state
# ---------------------------------------------------------------------------


class TestGhPrState:
    def test_returns_unknown_when_fetch_returns_none(self) -> None:
        with patch("cw.queue_peek.rows._fetch_pr_state", return_value=None):
            assert queue_peek.gh_pr_state(42) == "UNKNOWN"

    def test_returns_unknown_when_gh_not_on_path(self) -> None:
        with patch(
            "cw.queue_peek.rows._fetch_pr_state", side_effect=FileNotFoundError("gh")
        ):
            assert queue_peek.gh_pr_state(42) == "UNKNOWN"

    def test_returns_state_string_when_available(self) -> None:
        with patch("cw.queue_peek.rows._fetch_pr_state", return_value="MERGED"):
            assert queue_peek.gh_pr_state(42) == "MERGED"


# ---------------------------------------------------------------------------
# TestBlindRow — blind-labeling when no transcript is resolvable
# ---------------------------------------------------------------------------


def _make_blind_info(jsonl_idle_min: float | None = 12.0) -> dict[str, Any]:
    return {"signal_source": "blind", "jsonl_idle_min": jsonl_idle_min}


class TestBlindRow:
    def test_blind_signal_source(self) -> None:
        task = make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(), NOW)
        assert row["signal_source"] == "blind"

    def test_blind_recommend_is_peek_blind(self) -> None:
        task = make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(), NOW)
        assert row["recommend"] == queue_peek.RECOMMEND_BLIND
        assert row["recommend"] == "PEEK-BLIND"

    def test_blind_reason_includes_liveness(self) -> None:
        task = make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=12.0), NOW)
        assert "no resolvable transcript" in row["reason"]
        assert "12m ago" in row["reason"]

    def test_blind_reason_none_found_when_no_jsonl(self) -> None:
        task = make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=None), NOW)
        assert "no resolvable transcript" in row["reason"]
        assert "none found" in row["reason"]

    def test_blind_jsonl_idle_min_propagated(self) -> None:
        task = make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=7.5), NOW)
        assert row["jsonl_idle_min"] == 7.5

    def test_blind_jsonl_idle_min_none_when_no_jsonl(self) -> None:
        task = make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=None), NOW)
        assert row["jsonl_idle_min"] is None

    def test_blind_row_has_null_transcript_fields(self) -> None:
        task = make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(), NOW)
        assert row["age_min"] is None
        assert row["idle_min"] is None
        assert row["stage"] is None
        assert row["status"] is None
        assert row["pr"] is None

    def test_blind_row_includes_stage_high_water(self) -> None:
        task = make_ticket_task(stage_high_water=Stage.REVIEW)
        row = queue_peek.format_row(task, _make_blind_info(), NOW)
        assert "stage_high_water" in row
        assert row["stage_high_water"] == Stage.REVIEW

    def test_blind_row_includes_pipeline_stage(self) -> None:
        task = make_ticket_task(stage=Stage.FINALIZE)
        row = queue_peek.format_row(task, _make_blind_info(), NOW)
        assert "pipeline_stage" in row
        assert row["pipeline_stage"] == Stage.FINALIZE
        assert row["stage"] is None

    def test_non_blind_signal_source_is_transcript(self) -> None:
        task = make_ticket_task()
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
            "jsonl_idle_min": None,
        }
        row = queue_peek.format_row(task, info, NOW)
        assert row["signal_source"] == "transcript"
        assert row["jsonl_idle_min"] is None

    def test_format_row_defaults_to_transcript_when_signal_source_absent(
        self,
    ) -> None:
        """Backward compat: info without signal_source defaults to transcript path."""
        task = make_ticket_task()
        info: dict[str, Any] = {}
        row = queue_peek.format_row(task, info, NOW)
        assert row["signal_source"] == "transcript"

    def test_build_peek_rows_blind_when_no_transcript(self, patched_peek: None) -> None:
        """When find_transcript_for_ticket returns None, row is blind."""
        task = make_ticket_task("999")
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch("cw.queue_peek.rows.find_transcript_for_ticket", return_value=None),
            patch("cw.queue_peek.rows._compute_jsonl_idle_min", return_value=5.0),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert len(rows) == 1
        assert rows[0]["signal_source"] == "blind"
        assert rows[0]["recommend"] == "PEEK-BLIND"
        assert rows[0]["jsonl_idle_min"] == 5.0


def test_build_peek_rows_uses_opencode_parser(
    patched_peek: None, tmp_path: Path
) -> None:
    """build_peek_rows calls parse_opencode_transcript for opencode sessions."""
    from cw.opencode_runner import make_blocked

    worktree = tmp_path / "wt-peek-opencode"
    worktree.mkdir()
    blocked = make_blocked(ticket_id="T-1", worktree=worktree, reason="test")
    sentinel_json = blocked.model_dump_json()
    sentinel_text = f"<<<AUTO_DEV_RESULT\n{sentinel_json}\nAUTO_DEV_RESULT>>>"
    write_opencode_log(
        worktree,
        [{"type": "text", "part": {"text": sentinel_text}}],
    )

    task = make_ticket_task("T-1", worktree_path=worktree)
    with patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]):
        rows = queue_peek.build_peek_rows(None, NOW)

    assert len(rows) == 1
    assert rows[0]["signal_source"] == "transcript"
    assert rows[0]["status"] == "blocked"


# ---------------------------------------------------------------------------
# #2417: a matched Session with both Claude IDs null is blind, never a
# borrowed transcript from an earlier stage in a reused worktree
# ---------------------------------------------------------------------------

_BLIND_NEVER_STARTED = "blind \u2014 never started"
_BLIND_WITH_HANDLE = "blind \u2014 no Claude transcript (local process handle present)"
_IDLESS_SESSION_ID = "dead0417"


def _prior_stage_transcript(worktree: Path) -> Path:
    """Write the previous implementation stage's transcript for *worktree*.

    It carries a ``stage_complete`` sentinel: if queue peek wrongly selects it
    for the current review session, the row would show that stage/status.
    """
    from cw._util import claude_project_dir

    proj = claude_project_dir(worktree)
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / "11110000-0000-0000-0000-000000000000.jsonl"
    text = sentinel_text(
        sentinel_payload(ticket_id="T-1", status="stage_complete", stage="stage2_impl")
    )
    make_transcript(
        path,
        [
            {
                "type": "user",
                "timestamp": "2026-06-20T10:00:00Z",
                "message": {"content": "/auto-dev T-1 --headless"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-20T10:30:00Z",
                "message": {"content": [{"type": "text", "text": text}]},
            },
        ],
    )
    return path


def _write_idless_session(
    worktree: Path, *, local_liveness: dict[str, Any] | None = None
) -> None:
    row: dict[str, Any] = {
        "id": _IDLESS_SESSION_ID,
        "claude_session_id": None,
        "surface_ref": None,
        "started_at": "2026-06-20T11:00:00+00:00",
        "worktree_path": str(worktree),
    }
    if local_liveness is not None:
        row["local_liveness"] = local_liveness
    write_sessions(locate.CW_STATE, [row])


def _peek_idless_row(worktree: Path) -> dict[str, Any]:
    task = make_ticket_task("T-1", session_id=_IDLESS_SESSION_ID, stage=Stage.REVIEW)
    with patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]):
        rows = queue_peek.build_peek_rows(None, NOW)
    assert len(rows) == 1
    return rows[0]


class TestIdlessSessionBlind:
    def test_prior_stage_transcript_is_not_attributed_without_handle(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        worktree = tmp_path / ".cw" / "wt" / "abc" / "dev-1"
        worktree.mkdir(parents=True)
        prior = _prior_stage_transcript(worktree)
        # Precondition: the old sentinel is genuinely parseable, so a wrong
        # selection would be visible in the row.
        assert (
            queue_peek.parse_transcript(prior)["last_sentinel_status"]
            == "stage_complete"
        )
        _write_idless_session(worktree)

        row = _peek_idless_row(worktree)

        assert row["recommend"] == "PEEK-BLIND"
        assert row["signal_source"] == "blind"
        assert row["stage"] is None
        assert row["status"] is None
        assert row["age_min"] is None
        assert row["idle_min"] is None
        assert row["jsonl_idle_min"] is None
        assert row["reason"] == _BLIND_NEVER_STARTED
        assert "newest jsonl" not in row["reason"]
        assert not row["recommend"].startswith("STOP")

    def test_local_handle_stays_blind_without_claiming_never_started(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        worktree = tmp_path / ".cw" / "wt" / "abc" / "dev-1"
        worktree.mkdir(parents=True)
        _prior_stage_transcript(worktree)
        _write_idless_session(
            worktree,
            local_liveness={"pid": 4242, "start_time_ns": 7, "backend": "codex"},
        )

        row = _peek_idless_row(worktree)

        assert row["recommend"] == "PEEK-BLIND"
        assert row["stage"] is None
        assert row["status"] is None
        assert row["jsonl_idle_min"] is None
        assert row["reason"] == _BLIND_WITH_HANDLE
        assert "never started" not in row["reason"]

    def test_known_idless_session_skips_jsonl_idle_scan(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        worktree = tmp_path / ".cw" / "wt" / "abc" / "dev-1"
        worktree.mkdir(parents=True)
        _write_idless_session(worktree)
        with patch("cw.queue_peek.rows._compute_jsonl_idle_min") as scan:
            row = _peek_idless_row(worktree)
        scan.assert_not_called()
        assert row["jsonl_idle_min"] is None

    def test_no_session_row_keeps_legacy_blind_idle_scan(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """No matching Session row: ordinary blind row with the mtime scan."""
        task = make_ticket_task("999", session_id="nomatch1")
        with (
            patch("cw.queue_peek.rows.load_attention_tasks", return_value=[task]),
            patch("cw.queue_peek.rows.find_transcript_for_ticket", return_value=None),
            patch("cw.queue_peek.rows._compute_jsonl_idle_min", return_value=5.0),
        ):
            rows = queue_peek.build_peek_rows(None, NOW)
        assert rows[0]["jsonl_idle_min"] == 5.0
        assert rows[0]["reason"] == "no resolvable transcript; newest jsonl 5m ago"

    def test_exact_csid_match_still_resolves(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        from cw._util import claude_project_dir

        worktree = tmp_path / ".cw" / "wt" / "abc" / "dev-1"
        worktree.mkdir(parents=True)
        prior = _prior_stage_transcript(worktree)
        csid = prior.stem
        write_sessions(
            locate.CW_STATE,
            [
                {
                    "id": _IDLESS_SESSION_ID,
                    "claude_session_id": csid,
                    "surface_ref": None,
                    "started_at": "2000-01-01T00:00:00+00:00",
                    "worktree_path": str(worktree),
                }
            ],
        )
        assert prior.parent == claude_project_dir(worktree)

        row = _peek_idless_row(worktree)

        assert row["signal_source"] == "transcript"
        assert row["status"] == "stage_complete"

    def test_surface_ref_only_match_still_resolves(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        from cw._util import claude_project_dir

        worktree = tmp_path / ".cw" / "wt" / "abc" / "dev-1"
        worktree.mkdir(parents=True)
        proj = claude_project_dir(worktree)
        proj.mkdir(parents=True, exist_ok=True)
        jsonl = proj / "beef0417-0000-0000-0000-000000000000.jsonl"
        jsonl.write_text("")
        write_sessions(
            locate.CW_STATE,
            [
                {
                    "id": _IDLESS_SESSION_ID,
                    "claude_session_id": None,
                    "surface_ref": "beef0417",
                    "started_at": "2000-01-01T00:00:00+00:00",
                    "worktree_path": str(worktree),
                }
            ],
        )
        assert (
            queue_peek.find_transcript_for_ticket(
                "T-1", session_id=_IDLESS_SESSION_ID, worktree_path=None
            )
            == jsonl
        )

    def test_opencode_log_still_resolves_for_idless_session(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        from cw.opencode_runner import make_blocked

        worktree = tmp_path / "wt-opencode-idless"
        worktree.mkdir()
        blocked = make_blocked(ticket_id="T-1", worktree=worktree, reason="test")
        text = f"<<<AUTO_DEV_RESULT\n{blocked.model_dump_json()}\nAUTO_DEV_RESULT>>>"
        write_opencode_log(worktree, [{"type": "text", "part": {"text": text}}])
        _write_idless_session(worktree)

        row = _peek_idless_row(worktree)

        assert row["signal_source"] == "transcript"
        assert row["status"] == "blocked"

    def test_format_row_blind_reason_overrides_default_wording(self) -> None:
        info: dict[str, Any] = {
            "signal_source": "blind",
            "jsonl_idle_min": None,
            "blind_reason": _BLIND_NEVER_STARTED,
        }
        row = queue_peek.format_row(make_ticket_task(), info, NOW)
        assert row["reason"] == _BLIND_NEVER_STARTED
        assert row["recommend"] == "PEEK-BLIND"
