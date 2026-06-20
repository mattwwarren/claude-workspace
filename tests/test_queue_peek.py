"""Tests for cw.queue_peek — transcript selection, ladder, and sentinel parsing."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cw import queue_peek
from cw.models import QueueItemStatus, TicketTask

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_peek(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect CLAUDE_PROJECTS and CW_STATE to tmp_path for isolation."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    state_file = tmp_path / "sessions.json"
    monkeypatch.setattr(queue_peek, "CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr(queue_peek, "CW_STATE", state_file)


def _make_jsonl(
    proj_dir: Path, session_uuid: str, first_ts: str, first_text: str
) -> Path:
    """Create a minimal transcript jsonl at proj_dir/{session_uuid}.jsonl."""
    proj_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj_dir / f"{session_uuid}.jsonl"
    with jsonl.open("w") as f:
        f.write(json.dumps({"type": "init", "sessionId": session_uuid}) + "\n")
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": session_uuid,
                    "timestamp": first_ts,
                    "message": {"content": first_text},
                }
            )
            + "\n"
        )
    return jsonl


def _write_sessions(state_file: Path, sessions: list[dict[str, Any]]) -> None:
    state_file.write_text(json.dumps({"sessions": sessions}))


def _make_ticket_task(
    ticket_id: str = "T-1",
    session_id: str | None = "abc12345",
    client: str = "test",
    attempts: int = 1,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# find_transcript_for_ticket
# ---------------------------------------------------------------------------


class TestFindTranscriptForTicket:
    def test_no_project_dir_returns_none(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        queue_peek.CLAUDE_PROJECTS = tmp_path / "nonexistent"
        assert queue_peek.find_transcript_for_ticket("143") is None

    def test_no_matching_project_dir_returns_none(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        unrelated = queue_peek.CLAUDE_PROJECTS / "-home-cw-auto-dev-999"
        unrelated.mkdir()
        assert queue_peek.find_transcript_for_ticket("143") is None

    def test_single_transcript_returned(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-auto-dev-143"
        expected = _make_jsonl(
            proj,
            "aaaa1111-0000-0000-0000-000000000000",
            "2026-05-27T01:00:00Z",
            "/auto-dev 143 --headless",
        )
        result = queue_peek.find_transcript_for_ticket("143")
        assert result == expected

    def test_two_runs_picks_latest(self, patched_peek: None, tmp_path: Path) -> None:
        """Regression test for GH #358: two dispatch runs → picks the fresh one."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        _make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = _make_jsonl(
            proj,
            "fresh000-0000-0000-0000-000000000000",
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        result = queue_peek.find_transcript_for_ticket("143")
        assert result == fresh

    def test_session_id_match_selects_exact_jsonl(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """When session_id resolves to a claude_session_id, that exact jsonl is used."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        fresh_uuid = "23d81c88-0000-0000-0000-000000000000"
        _make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = _make_jsonl(
            proj,
            fresh_uuid,
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        _write_sessions(
            queue_peek.CW_STATE,
            [{"id": "23d81c88", "claude_session_id": fresh_uuid, "status": "active"}],
        )
        result = queue_peek.find_transcript_for_ticket("143", session_id="23d81c88")
        assert result == fresh

    def test_session_id_not_in_sessions_json_falls_back_to_latest(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """If session_id has no match in sessions.json, falls back to latest."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        _make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = _make_jsonl(
            proj,
            "fresh000-0000-0000-0000-000000000000",
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        _write_sessions(queue_peek.CW_STATE, [])
        result = queue_peek.find_transcript_for_ticket("143", session_id="unknown1")
        assert result == fresh

    def test_session_id_resolves_but_jsonl_missing_falls_back_to_latest(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """If session resolves but jsonl doesn't exist on disk, falls back to latest."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        _make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = _make_jsonl(
            proj,
            "fresh000-0000-0000-0000-000000000000",
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        # session maps to a UUID that has no corresponding jsonl file
        _write_sessions(
            queue_peek.CW_STATE,
            [
                {
                    "id": "23d81c88",
                    "claude_session_id": "23d81c88-0000-0000-0000-000000000000",
                    "status": "active",
                }
            ],
        )
        result = queue_peek.find_transcript_for_ticket("143", session_id="23d81c88")
        assert result == fresh

    def test_main_vs_subagent_within_single_run(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """Within one run, main session (score=0) wins over subagent (score=1)."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        main_jsonl = _make_jsonl(
            proj,
            "main0000-0000-0000-0000-000000000000",
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        # Subagent: different first user text → score=1 but later timestamp
        _make_jsonl(
            proj,
            "sub00000-0000-0000-0000-000000000000",
            "2026-05-29T02:36:00Z",
            "Implement the fix in file.py per the plan",
        )
        result = queue_peek.find_transcript_for_ticket("143")
        assert result == main_jsonl

    def test_no_session_id_arg_uses_heuristic(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """Calling without session_id still works (backward compat)."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-200"
        expected = _make_jsonl(
            proj,
            "bbbb2222-0000-0000-0000-000000000000",
            "2026-05-29T03:00:00Z",
            "/auto-dev 200 --headless",
        )
        result = queue_peek.find_transcript_for_ticket("200")
        assert result == expected


# ---------------------------------------------------------------------------
# load_claude_session_id
# ---------------------------------------------------------------------------


class TestLoadClaudeSessionId:
    def test_returns_none_when_no_state_file(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        assert queue_peek.load_claude_session_id("abc12345") is None

    def test_returns_none_when_session_id_is_none(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        assert queue_peek.load_claude_session_id(None) is None

    def test_returns_claude_session_id_when_found(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        uuid = "23d81c88-1234-5678-abcd-000000000000"
        _write_sessions(
            queue_peek.CW_STATE,
            [{"id": "23d81c88", "claude_session_id": uuid, "status": "active"}],
        )
        assert queue_peek.load_claude_session_id("23d81c88") == uuid

    def test_returns_none_when_id_not_in_sessions(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        _write_sessions(
            queue_peek.CW_STATE,
            [
                {
                    "id": "other123",
                    "claude_session_id": "other123-0000-0000-0000-000000000000",
                }
            ],
        )
        assert queue_peek.load_claude_session_id("notfound") is None

    def test_handles_corrupt_sessions_json_gracefully(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        queue_peek.CW_STATE.write_text("{not valid json}")
        assert queue_peek.load_claude_session_id("abc12345") is None


# ---------------------------------------------------------------------------
# minutes_since
# ---------------------------------------------------------------------------

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


class TestMinutesSince:
    def test_returns_none_for_none_input(self) -> None:
        assert queue_peek.minutes_since(None, _NOW) is None

    def test_returns_none_for_invalid_timestamp(self) -> None:
        assert queue_peek.minutes_since("not-a-date", _NOW) is None

    def test_computes_correct_minutes(self) -> None:
        ts = "2026-06-20T11:50:00+00:00"  # 10 minutes before _NOW
        result = queue_peek.minutes_since(ts, _NOW)
        assert result == pytest.approx(10.0)

    def test_returns_zero_for_current_time(self) -> None:
        ts = _NOW.isoformat()
        result = queue_peek.minutes_since(ts, _NOW)
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# recommend / peek-stop ladder
# ---------------------------------------------------------------------------


class TestRecommend:
    def test_no_transcript_returns_peek(self) -> None:
        rec, reason = queue_peek.recommend(None, None, None, None, 1)
        assert rec == "PEEK"
        assert "no transcript" in reason

    def test_merged_pr_idle_returns_stop(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_POST_PR_MIN + 1.0,
            pr_state="MERGED",
            sentinel_status=None,
            attempts=1,
        )
        assert rec == "STOP"
        assert "merged" in reason.lower()

    def test_merged_pr_not_idle_returns_wait(self) -> None:
        """PR merged but worker still active → falls through to stall check."""
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state="MERGED",
            sentinel_status=None,
            attempts=1,
        )
        # idle_min = 0.0, not > IDLE_POST_PR_MIN, so doesn't STOP — falls to stall check
        assert rec == "WAIT"

    def test_shipped_open_pr_healthy_returns_wait(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=2.0,
            pr_state="OPEN",
            sentinel_status="shipped",
            attempts=1,
        )
        assert rec == "WAIT"
        assert "CI" in reason

    def test_shipped_open_pr_stalled_returns_peek(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=20.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state="OPEN",
            sentinel_status="shipped",
            attempts=1,
        )
        assert rec == "PEEK"
        assert "CI" in reason

    def test_age_above_stop_threshold_returns_stop(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=queue_peek.STOP_AGE_MIN + 1.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=1,
        )
        assert rec == "STOP"
        assert "timeout" in reason

    def test_high_attempts_returns_stop(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=queue_peek.STOP_ATTEMPTS_MIN,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_long_idle_no_pr_returns_stop_or_peek(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            attempts=1,
        )
        assert rec == "STOP-OR-PEEK"

    def test_moderate_idle_no_pr_returns_peek(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_PEEK_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            attempts=1,
        )
        assert rec == "PEEK"

    def test_mature_age_returns_peek(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=queue_peek.PEEK_AGE_MIN + 1.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=1,
        )
        assert rec == "PEEK"

    def test_moderate_age_returns_wait(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=queue_peek.WAIT_AGE_MIN + 1.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=1,
        )
        assert rec == "WAIT"

    def test_early_age_returns_wait(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=5.0,
            idle_min=None,
            pr_state=None,
            sentinel_status=None,
            attempts=1,
        )
        assert rec == "WAIT"
        assert "early" in reason


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------


def _make_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _sentinel_payload(
    ticket_id: str = "T-999",
    status: str = "review_pending_approval",
    stage: str = "stage3_review",
    pr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid AutoDevResult payload for sentinel tests."""
    _scope = {
        "tier": "small",
        "files": 2,
        "lines_estimate": 50,
        "lines_actual": 52,
        "forbidden_touched": False,
    }
    _health = {
        "lowest_agent_confidence": "HIGH",
        "any_incomplete_risk": False,
        "shortcuts": [],
        "recommendation": "PROCEED",
        "downgrade_applied": False,
        "fix_loop_escalated": False,
    }
    return {
        "schema_version": 4,
        "ticket_id": ticket_id,
        "status": status,
        "stage_reached": stage,
        "scope": _scope,
        "plan_source": "github_issue_existing",
        "branch": f"dev/{ticket_id.lower()}",
        "commits": ["abc"] if pr else [],
        "pr": pr,
        "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},
        "health": _health,
        "friction_highlights": [],
        "blocker": None,
        "next_actions": [],
    }


def _sentinel_text(payload: dict[str, Any]) -> str:
    return f"<<<AUTO_DEV_RESULT\n{json.dumps(payload)}\nAUTO_DEV_RESULT>>>"


class TestParseTranscript:
    def test_empty_file_returns_all_nones(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] is None
        assert result["last_asst_ts"] is None
        assert result["last_sentinel_status"] is None
        assert result["last_sentinel_stage"] is None
        assert result["last_pr_number"] is None

    def test_extracts_user_and_assistant_timestamps(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": []},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] == "2026-06-01T10:00:00Z"
        assert result["last_asst_ts"] == "2026-06-01T10:05:00Z"
        assert result["last_sentinel_status"] is None

    def test_first_user_ts_set_only_once(self, tmp_path: Path) -> None:
        """Multiple user messages: first_user_ts captures the earliest."""
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "first"},
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:10:00Z",
                    "message": {"content": "second"},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] == "2026-06-01T10:00:00Z"

    def test_handles_bad_json_lines_gracefully(self, tmp_path: Path) -> None:
        p = tmp_path / "t.jsonl"
        user_line = json.dumps(
            {"type": "user", "timestamp": "2026-06-01T10:00:00Z", "message": {}}
        )
        asst_line = json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-06-01T10:01:00Z",
                "message": {"content": []},
            }
        )
        p.write_text(f"{user_line}\nnot valid json\n{asst_line}\n")
        result = queue_peek.parse_transcript(p)
        assert result["first_user_ts"] == "2026-06-01T10:00:00Z"
        assert result["last_asst_ts"] == "2026-06-01T10:01:00Z"

    def test_nonexistent_file_returns_all_nones(self, tmp_path: Path) -> None:
        """FileNotFoundError (OSError subclass) is caught and returns empty-ish dict."""
        result = queue_peek.parse_transcript(tmp_path / "nope.jsonl")
        assert result["first_user_ts"] is None
        assert result["last_sentinel_status"] is None

    def test_extracts_sentinel_via_auto_dev_result(self, tmp_path: Path) -> None:
        """Valid sentinel in assistant message updates last_sentinel fields."""
        text = _sentinel_text(_sentinel_payload())
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "start"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                },
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] == "review_pending_approval"
        assert result["last_sentinel_stage"] == "stage3_review"
        assert result["last_pr_number"] is None

    def test_sentinel_with_pr_number(self, tmp_path: Path) -> None:
        """Sentinel with pr.number populates last_pr_number."""
        pr = {
            "number": 77,
            "url": "https://example.com/pr/77",
            "auto_merge": False,
            "base": "main",
        }
        text = _sentinel_text(
            _sentinel_payload(status="shipped", stage="stage5_post_create", pr=pr)
        )
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_pr_number"] == 77
        assert result["last_sentinel_status"] == "shipped"

    def test_skips_documented_example_sentinel(self, tmp_path: Path) -> None:
        """Example sentinel (PROJ-1234, pr#42) is skipped; status stays None."""
        pr = {
            "number": 42,
            "url": "https://example.com/pr/42",
            "auto_merge": False,
            "base": "main",
        }
        example_payload = _sentinel_payload(
            ticket_id="PROJ-1234",
            status="shipped",
            stage="stage5_post_create",
            pr=pr,
        )
        example_payload["branch"] = "dev/proj-1234-fix"
        example_payload["plan_source"] = "none"
        text = _sentinel_text(example_payload)
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:05:00Z",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None


# ---------------------------------------------------------------------------
# format_row
# ---------------------------------------------------------------------------


class TestFormatRow:
    def test_returns_all_required_keys(self) -> None:
        task = _make_ticket_task(
            ticket_id="T-1", session_id="sess1234", client="acme", attempts=2
        )
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
        }
        with patch("cw.queue_peek.gh_pr_state", return_value="OPEN"):
            row = queue_peek.format_row(task, info, _NOW)
        required_keys = {
            "ticket",
            "session",
            "client",
            "attempts",
            "age_min",
            "idle_min",
            "stage",
            "status",
            "pr",
            "pr_state",
            "recommend",
            "reason",
        }
        assert required_keys == set(row.keys())

    def test_session_truncated_to_12_chars(self) -> None:
        task = _make_ticket_task(session_id="abcdef1234567890")
        row = queue_peek.format_row(task, {}, _NOW)
        assert row["session"] == "abcdef123456"

    def test_none_session_id_becomes_dash(self) -> None:
        task = _make_ticket_task(session_id=None)
        row = queue_peek.format_row(task, {}, _NOW)
        assert row["session"] == "-"

    def test_no_pr_number_skips_gh_call(self) -> None:
        task = _make_ticket_task()
        info: dict[str, Any] = {"last_pr_number": None}
        with patch("cw.queue_peek.gh_pr_state") as mock_gh:
            queue_peek.format_row(task, info, _NOW)
        mock_gh.assert_not_called()

    def test_pr_number_triggers_gh_call(self) -> None:
        task = _make_ticket_task()
        info: dict[str, Any] = {"last_pr_number": 99}
        with patch("cw.queue_peek.gh_pr_state", return_value="OPEN") as mock_gh:
            row = queue_peek.format_row(task, info, _NOW)
        mock_gh.assert_called_once_with(99)
        assert row["pr_state"] == "OPEN"


# ---------------------------------------------------------------------------
# build_peek_rows
# ---------------------------------------------------------------------------


class TestBuildPeekRows:
    def test_returns_empty_list_when_no_running_tasks(self) -> None:
        with patch("cw.queue_peek.load_running_tasks", return_value=[]):
            rows = queue_peek.build_peek_rows(None, _NOW)
        assert rows == []

    def test_builds_one_row_per_task(self) -> None:
        tasks = [_make_ticket_task("T-1"), _make_ticket_task("T-2")]
        with (
            patch("cw.queue_peek.load_running_tasks", return_value=tasks),
            patch("cw.queue_peek.find_transcript_for_ticket", return_value=None),
            patch("cw.queue_peek.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, _NOW)
        assert len(rows) == 2

    def test_client_filter_passed_to_load(self) -> None:
        with (
            patch("cw.queue_peek.load_running_tasks", return_value=[]) as mock_load,
        ):
            queue_peek.build_peek_rows("my-client", _NOW)
        mock_load.assert_called_once_with("my-client")
