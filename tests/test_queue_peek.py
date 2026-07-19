"""Tests for cw.queue_peek — transcript selection, ladder, and sentinel parsing."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cw import queue_peek
from cw.models import QueueItemStatus, Stage, TicketTask
from tests.conftest import _make_ticket_task as _cw_make_ticket_task

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
    worktree_path: Path | None = None,
    stage_high_water: Stage | None = None,
) -> TicketTask:
    return _cw_make_ticket_task(
        ticket_id=ticket_id,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
        attempts=attempts,
        worktree_path=worktree_path,
        stage_high_water=stage_high_water,
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
# _find_transcript_in_project_dir
# ---------------------------------------------------------------------------


class TestFindTranscriptInProjectDir:
    def test_returns_none_for_missing_project_dir(self, tmp_path: Path) -> None:
        result = queue_peek._find_transcript_in_project_dir(
            tmp_path / "nonexistent", None, None, None
        )
        assert result is None

    def test_exact_csid_match(self, tmp_path: Path) -> None:
        csid = "abcd1234-0000-0000-0000-000000000000"
        proj = tmp_path / "proj"
        proj.mkdir()
        jsonl = proj / f"{csid}.jsonl"
        jsonl.write_text("")
        result = queue_peek._find_transcript_in_project_dir(proj, csid, None, None)
        assert result == jsonl

    def test_csid_file_missing_falls_through_to_surface_ref(
        self, tmp_path: Path
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        surface_ref = "9ef252ef"
        jsonl = proj / f"{surface_ref}-full-uuid.jsonl"
        jsonl.write_text("")
        # csid doesn't exist on disk → falls through to surface_ref match
        result = queue_peek._find_transcript_in_project_dir(
            proj, "missing-csid-0000-0000-0000-000000000000", surface_ref, None
        )
        assert result == jsonl

    def test_surface_ref_match_with_no_started_at(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        surface_ref = "aabbccdd"
        jsonl = proj / f"{surface_ref}-abc.jsonl"
        jsonl.write_text("")
        result = queue_peek._find_transcript_in_project_dir(
            proj, None, surface_ref, None
        )
        assert result == jsonl

    def test_surface_ref_mtime_guard_excludes_stale_file(self, tmp_path: Path) -> None:
        """surface_ref glob excludes files with mtime <= started_at."""
        import os

        proj = tmp_path / "proj"
        proj.mkdir()
        surface_ref = "aabbccdd"
        stale = proj / f"{surface_ref}-old.jsonl"
        stale.write_text("")
        # Set mtime to epoch so it's always before any reasonable started_at
        os.utime(stale, (0, 0))
        started_at = "2026-01-01T00:00:00+00:00"
        result = queue_peek._find_transcript_in_project_dir(
            proj, None, surface_ref, started_at
        )
        assert result is None

    def test_surface_ref_mtime_guard_accepts_fresh_file(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        surface_ref = "aabbccdd"
        fresh = proj / f"{surface_ref}-new.jsonl"
        fresh.write_text("")
        # started_at in the distant past → mtime of just-created file is after it
        started_at = "2000-01-01T00:00:00+00:00"
        result = queue_peek._find_transcript_in_project_dir(
            proj, None, surface_ref, started_at
        )
        assert result == fresh

    def test_degraded_fallback_newest_jsonl_when_no_ids(self, tmp_path: Path) -> None:
        """When both csid and surface_ref are None, returns newest *.jsonl."""
        import os

        proj = tmp_path / "proj"
        proj.mkdir()
        old = proj / "old.jsonl"
        old.write_text("")
        os.utime(old, (1000, 1000))
        new = proj / "new.jsonl"
        new.write_text("")
        # new's mtime is the OS current time, which is > 1000
        result = queue_peek._find_transcript_in_project_dir(proj, None, None, None)
        assert result == new

    def test_degraded_fallback_returns_none_when_empty_dir(
        self, tmp_path: Path
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        result = queue_peek._find_transcript_in_project_dir(proj, None, None, None)
        assert result is None

    def test_surface_ref_naive_started_at_is_utc_coerced(self, tmp_path: Path) -> None:
        """Naive (tz-less) started_at_iso is treated as UTC, not dropped."""
        proj = tmp_path / "proj"
        proj.mkdir()
        surface_ref = "aabbccdd"
        jsonl = proj / f"{surface_ref}-new.jsonl"
        jsonl.write_text("")
        # Naive ISO string (no +00:00) — should be coerced to UTC and accepted
        # since the just-created file's mtime is well after year 2000.
        started_at = "2000-01-01T00:00:00"  # no timezone suffix
        result = queue_peek._find_transcript_in_project_dir(
            proj, None, surface_ref, started_at
        )
        assert result == jsonl

    def test_surface_ref_invalid_started_at_treated_as_none(
        self, tmp_path: Path
    ) -> None:
        """Invalid started_at_iso falls through ValueError → started_at stays None."""
        proj = tmp_path / "proj"
        proj.mkdir()
        surface_ref = "aabbccdd"
        jsonl = proj / f"{surface_ref}-any.jsonl"
        jsonl.write_text("")
        result = queue_peek._find_transcript_in_project_dir(
            proj, None, surface_ref, "not-a-date"
        )
        # started_at stays None after ValueError → no mtime guard → first candidate
        assert result == jsonl


# ---------------------------------------------------------------------------
# find_transcript_for_ticket — worktree_path-based lookup
# ---------------------------------------------------------------------------


class TestFindTranscriptForTicketWorktreePath:
    """Verify that providing worktree_path uses claude_project_dir correctly."""

    def test_worktree_path_finds_transcript_via_csid(
        self, patched_peek: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With worktree_path + csid in sessions.json, exact jsonl is returned."""
        from cw._util import claude_project_dir

        worktree = tmp_path / ".cw" / "wt" / "abc123" / "dev-500"
        worktree.mkdir(parents=True)
        proj = claude_project_dir(worktree)
        csid = "dead1234-0000-0000-0000-000000000000"
        jsonl = proj / f"{csid}.jsonl"
        proj.mkdir(parents=True)
        jsonl.write_text("")

        session_id = "deadbeef"
        _write_sessions(
            queue_peek.CW_STATE,
            [{"id": session_id, "claude_session_id": csid, "surface_ref": None}],
        )
        result = queue_peek.find_transcript_for_ticket(
            "500", session_id=session_id, worktree_path=worktree
        )
        assert result == jsonl

    def test_worktree_path_finds_transcript_via_surface_ref(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """With worktree_path + surface_ref (no csid), surface_ref glob is used."""
        from cw._util import claude_project_dir

        worktree = tmp_path / ".cw" / "wt" / "abc123" / "dev-501"
        worktree.mkdir(parents=True)
        proj = claude_project_dir(worktree)
        proj.mkdir(parents=True)
        surface_ref = "cafe1234"
        jsonl = proj / f"{surface_ref}-full-uuid.jsonl"
        jsonl.write_text("")

        session_id = "cafe1234"
        _write_sessions(
            queue_peek.CW_STATE,
            [
                {
                    "id": session_id,
                    "claude_session_id": None,
                    "surface_ref": surface_ref,
                    "started_at": "2000-01-01T00:00:00+00:00",
                }
            ],
        )
        result = queue_peek.find_transcript_for_ticket(
            "501", session_id=session_id, worktree_path=worktree
        )
        assert result == jsonl

    def test_worktree_path_degraded_fallback_no_session_ids(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """With worktree_path but no session ids, returns newest *.jsonl."""
        from cw._util import claude_project_dir

        worktree = tmp_path / ".cw" / "wt" / "abc123" / "dev-502"
        worktree.mkdir(parents=True)
        proj = claude_project_dir(worktree)
        proj.mkdir(parents=True)
        jsonl = proj / "some-transcript.jsonl"
        jsonl.write_text("")

        # No sessions.json written → _load_session_refs returns {}
        result = queue_peek.find_transcript_for_ticket(
            "502", session_id=None, worktree_path=worktree
        )
        assert result == jsonl

    def test_worktree_path_project_dir_missing_falls_back_to_heuristic(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """When project_dir doesn't exist, falls back to heuristic search."""
        worktree = tmp_path / ".cw" / "wt" / "ghost" / "dev-503"
        worktree.mkdir(parents=True)
        # No project dir created → worktree path lookup finds nothing

        # Put a matching heuristic dir in CLAUDE_PROJECTS so fallback works
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-auto-dev-503"
        _make_jsonl(
            proj,
            "bbbb0000-0000-0000-0000-000000000000",
            "2026-06-01T10:00:00Z",
            "/auto-dev 503 --headless",
        )
        result = queue_peek.find_transcript_for_ticket(
            "503", session_id=None, worktree_path=worktree
        )
        # Falls back to heuristic and finds the matching dir
        assert result is not None
        assert result.name == "bbbb0000-0000-0000-0000-000000000000.jsonl"


# ---------------------------------------------------------------------------
# find_transcript_for_ticket — session.worktree_path fallback (DAEMON path)
# ---------------------------------------------------------------------------


class TestFindTranscriptForTicketDaemonWorktreePath:
    """DAEMON-origin sessions: TicketTask.worktree_path is None; worktree_path
    must be loaded from the Session in CW_STATE."""

    def test_daemon_task_resolves_via_session_worktree_path(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """Regression: task worktree_path=None but session carries it."""
        from cw._util import claude_project_dir

        worktree = tmp_path / ".cw" / "wt" / "abc123" / "dev-816"
        worktree.mkdir(parents=True)
        proj = claude_project_dir(worktree)
        proj.mkdir(parents=True)
        csid = "feed0000-0000-0000-0000-000000000000"
        jsonl = proj / f"{csid}.jsonl"
        jsonl.write_text("")

        session_id = "feedbeef"
        _write_sessions(
            queue_peek.CW_STATE,
            [
                {
                    "id": session_id,
                    "claude_session_id": csid,
                    "surface_ref": None,
                    "worktree_path": str(worktree),
                }
            ],
        )
        # Pass worktree_path=None to simulate TicketTask.worktree_path for a
        # dispatch-created task — the function must fall back to CW_STATE.
        result = queue_peek.find_transcript_for_ticket(
            "816", session_id=session_id, worktree_path=None
        )
        assert result == jsonl

    def test_daemon_task_no_worktree_path_in_session_uses_heuristic(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """When both task and session lack worktree_path, falls back to heuristic."""
        session_id = "cafe0000"
        _write_sessions(
            queue_peek.CW_STATE,
            [{"id": session_id, "claude_session_id": None, "worktree_path": None}],
        )
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-dev-504"
        _make_jsonl(
            proj,
            "cccc0000-0000-0000-0000-000000000000",
            "2026-06-01T10:00:00Z",
            "/auto-dev 504 --headless",
        )
        result = queue_peek.find_transcript_for_ticket(
            "504", session_id=session_id, worktree_path=None
        )
        assert result is not None
        assert result.name == "cccc0000-0000-0000-0000-000000000000.jsonl"


# ---------------------------------------------------------------------------
# _matching_project_dirs — endswith fix
# ---------------------------------------------------------------------------


class TestMatchingProjectDirs:
    def test_matches_dev_prefix_dir(self, patched_peek: None) -> None:
        """dev-816 slug now matches ticket 816 (the regression this PR fixes)."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-matthew--cw-wt-7dc983e2-dev-816"
        proj.mkdir(parents=True)
        result = queue_peek._matching_project_dirs("816")
        assert proj in result

    def test_matches_auto_dev_prefix_dir(self, patched_peek: None) -> None:
        """auto-dev-816 dirs still match — regression guard for other clients."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-user-projects-auto-dev-816"
        proj.mkdir(parents=True)
        result = queue_peek._matching_project_dirs("816")
        assert proj in result

    def test_does_not_match_longer_id(self, patched_peek: None) -> None:
        """dev-8160 does not match ticket 816 — no substring false positives."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-user-dev-8160"
        proj.mkdir(parents=True)
        result = queue_peek._matching_project_dirs("816")
        assert proj not in result


# ---------------------------------------------------------------------------
# _load_session_refs
# ---------------------------------------------------------------------------


class TestLoadSessionRefs:
    def test_returns_empty_dict_when_no_session_id(self, patched_peek: None) -> None:
        assert queue_peek._load_session_refs(None) == {}

    def test_returns_empty_dict_when_no_state_file(self, patched_peek: None) -> None:
        assert queue_peek._load_session_refs("abc12345") == {}

    def test_returns_all_refs_when_found(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        wt = "/home/user/.cw/wt/abc/dev-42"
        _write_sessions(
            queue_peek.CW_STATE,
            [
                {
                    "id": "abc12345",
                    "claude_session_id": "abc12345-0000-0000-0000-000000000000",
                    "surface_ref": "abc12345",
                    "started_at": "2026-06-21T00:00:00+00:00",
                    "worktree_path": wt,
                }
            ],
        )
        refs = queue_peek._load_session_refs("abc12345")
        assert refs["claude_session_id"] == "abc12345-0000-0000-0000-000000000000"
        assert refs["surface_ref"] == "abc12345"
        assert refs["started_at"] == "2026-06-21T00:00:00+00:00"
        assert refs["worktree_path"] == wt

    def test_returns_empty_dict_when_not_found(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        _write_sessions(queue_peek.CW_STATE, [{"id": "other111"}])
        assert queue_peek._load_session_refs("abc12345") == {}

    def test_handles_corrupt_json_gracefully(self, patched_peek: None) -> None:
        queue_peek.CW_STATE.write_text("{bad json")
        assert queue_peek._load_session_refs("abc12345") == {}


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


class TestReachedDeepStage:
    """_reached_deep_stage — pipeline-order gate for stage_high_water (#1361)."""

    def test_review_is_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.REVIEW) is True

    def test_finalize_is_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.FINALIZE) is True

    def test_impl_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.IMPL) is False

    def test_plan_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.PLAN) is False

    def test_harden_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(Stage.HARDEN) is False

    def test_none_is_not_deep(self) -> None:
        assert queue_peek._reached_deep_stage(None) is False

    def test_stage_order_is_pipeline_order_not_lexicographic(self) -> None:
        """_STAGE_ORDER must be pipeline order (HARDEN..FINALIZE), NOT the
        lexicographic order plain StrEnum '<' comparison would give — this
        documents the exact trap a naive '<'/'>' comparison would create."""
        order = queue_peek._STAGE_ORDER
        assert (
            order.index(Stage.HARDEN)
            < order.index(Stage.PLAN)
            < order.index(Stage.IMPL)
            < order.index(Stage.REVIEW)
            < order.index(Stage.FINALIZE)
        )
        # Plain StrEnum '<' is lexicographic on the string values and gives
        # the opposite (wrong) answer for this pair.
        assert (Stage.FINALIZE < Stage.HARDEN) is True


class TestScoreSessionStageHighWater:
    """_score_session's attempt-count STOP branch gated on stage_high_water
    (#1361): a ticket that demonstrably reached REVIEW or later falls
    through to the stall-check ladder instead of hard-stopping."""

    def test_high_attempts_with_deep_high_water_falls_through_not_stop(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=queue_peek.STOP_ATTEMPTS_MIN,
            stage_high_water=Stage.REVIEW,
        )
        assert rec != "STOP"
        assert "systemic" not in reason

    def test_high_attempts_with_shallow_high_water_still_stops(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=queue_peek.STOP_ATTEMPTS_MIN,
            stage_high_water=Stage.IMPL,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_high_attempts_with_none_high_water_still_stops(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=queue_peek.STOP_ATTEMPTS_MIN,
            stage_high_water=None,
        )
        assert rec == "STOP"
        assert "systemic" in reason

    def test_merged_pr_stuck_stops_even_with_finalize_high_water(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_POST_PR_MIN + 1.0,
            pr_state="MERGED",
            sentinel_status=None,
            attempts=1,
            stage_high_water=Stage.FINALIZE,
        )
        assert rec == "STOP"
        assert "merged" in reason.lower()

    def test_age_timeout_stops_even_with_finalize_high_water(self) -> None:
        rec, reason = queue_peek.recommend(
            age_min=queue_peek.STOP_AGE_MIN + 1.0,
            idle_min=0.0,
            pr_state=None,
            sentinel_status=None,
            attempts=1,
            stage_high_water=Stage.FINALIZE,
        )
        assert rec == "STOP"
        assert "timeout" in reason

    def test_grinding_but_idle_stalled_falls_through_to_nonwait(self) -> None:
        rec, _ = queue_peek.recommend(
            age_min=10.0,
            idle_min=queue_peek.IDLE_STALL_MIN + 1.0,
            pr_state=None,
            sentinel_status=None,
            attempts=queue_peek.STOP_ATTEMPTS_MIN,
            stage_high_water=Stage.REVIEW,
        )
        assert rec == "STOP-OR-PEEK"


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
            "signal_source": "transcript",
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
            "signal_source",
            "jsonl_idle_min",
            "stage_high_water",
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

    def test_stage_high_water_present_in_row(self) -> None:
        task = _make_ticket_task(stage_high_water=Stage.REVIEW)
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
        }
        row = queue_peek.format_row(task, info, _NOW)
        assert row["stage_high_water"] == Stage.REVIEW


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

    def test_passes_worktree_path_to_find_transcript(self, tmp_path: Path) -> None:
        """build_peek_rows forwards task.worktree_path to find_transcript_for_ticket."""
        wt = tmp_path / "dev-999"
        task = _make_ticket_task("999", worktree_path=wt)
        captured_wt: list[object] = []

        def _capture(
            ticket_id: str,
            session_id: object = None,
            worktree_path: object = None,
        ) -> None:
            captured_wt.append(worktree_path)

        with (
            patch("cw.queue_peek.load_running_tasks", return_value=[task]),
            patch("cw.queue_peek.find_transcript_for_ticket", side_effect=_capture),
            patch("cw.queue_peek.gh_pr_state", return_value="UNKNOWN"),
        ):
            queue_peek.build_peek_rows(None, _NOW)
        assert len(captured_wt) == 1
        assert captured_wt[0] == wt

    def test_client_filter_passed_to_load(self) -> None:
        with (
            patch("cw.queue_peek.load_running_tasks", return_value=[]) as mock_load,
        ):
            queue_peek.build_peek_rows("my-client", _NOW)
        mock_load.assert_called_once_with("my-client")

    def test_transcript_found_sets_signal_source_transcript(
        self, tmp_path: Path
    ) -> None:
        """When transcript is found, row has signal_source=transcript."""
        task = _make_ticket_task("T-1")
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
            patch("cw.queue_peek.load_running_tasks", return_value=[task]),
            patch(
                "cw.queue_peek.find_transcript_for_ticket",
                return_value=transcript_path,
            ),
            patch("cw.queue_peek.gh_pr_state", return_value="UNKNOWN"),
        ):
            rows = queue_peek.build_peek_rows(None, _NOW)
        assert len(rows) == 1
        assert rows[0]["signal_source"] == "transcript"
        assert rows[0]["jsonl_idle_min"] is None


# ---------------------------------------------------------------------------
# load_running_tasks
# ---------------------------------------------------------------------------


class TestLoadRunningTasks:
    def test_filters_out_non_running_tasks(self) -> None:
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
        with patch("cw.queue_peek.list_tickets", return_value=[pending, running]):
            result = queue_peek.load_running_tasks(None)
        assert len(result) == 1
        assert result[0].ticket_id == "T-2"


# ---------------------------------------------------------------------------
# gh_pr_state
# ---------------------------------------------------------------------------


class TestGhPrState:
    def test_returns_unknown_when_fetch_returns_none(self) -> None:
        with patch("cw.queue_peek._fetch_pr_state", return_value=None):
            assert queue_peek.gh_pr_state(42) == "UNKNOWN"

    def test_returns_unknown_when_gh_not_on_path(self) -> None:
        with patch(
            "cw.queue_peek._fetch_pr_state", side_effect=FileNotFoundError("gh")
        ):
            assert queue_peek.gh_pr_state(42) == "UNKNOWN"

    def test_returns_state_string_when_available(self) -> None:
        with patch("cw.queue_peek._fetch_pr_state", return_value="MERGED"):
            assert queue_peek.gh_pr_state(42) == "MERGED"


# ---------------------------------------------------------------------------
# _find_transcript_heuristic — missing branches
# ---------------------------------------------------------------------------


class TestFindTranscriptHeuristic:
    def test_skips_corrupt_json_lines(self, patched_peek: None, tmp_path: Path) -> None:
        """json.JSONDecodeError lines in the jsonl are skipped, not raised."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-auto-dev-100"
        proj.mkdir(parents=True)
        jsonl = proj / "aaa.jsonl"
        jsonl.write_text("not json\n")
        # No valid user line → scores as 1, but still returned as only candidate.
        result = queue_peek.find_transcript_for_ticket("100")
        assert result == jsonl

    def test_list_content_in_first_user_message(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """List-typed content is handled and text extracted for scoring."""
        proj = queue_peek.CLAUDE_PROJECTS / "-home-cw-auto-dev-101"
        proj.mkdir(parents=True)
        jsonl = proj / "bbb.jsonl"
        record = json.dumps(
            {
                "type": "user",
                "timestamp": "2026-06-01T10:00:00Z",
                "message": {
                    "content": [
                        {"type": "text", "text": "/auto-dev 101 --headless"},
                        {"type": "other", "data": "ignored"},
                    ]
                },
            }
        )
        jsonl.write_text(record + "\n")
        result = queue_peek.find_transcript_for_ticket("101")
        assert result == jsonl


# ---------------------------------------------------------------------------
# parse_transcript — defensive branches
# ---------------------------------------------------------------------------


class TestParseTranscriptDefensiveBranches:
    def test_non_list_content_is_skipped(self, tmp_path: Path) -> None:
        """assistant message with content as a string (not list) is not parsed."""
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": "plain string"},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None

    def test_non_text_block_type_is_skipped(self, tmp_path: Path) -> None:
        """Block with type != 'text' is skipped."""
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [{"type": "tool_use", "id": "tu1", "input": {}}]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None

    def test_text_without_sentinel_block_is_skipped(self, tmp_path: Path) -> None:
        """Text block that doesn't contain a sentinel marker is skipped cleanly."""
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {
                        "content": [{"type": "text", "text": "just some commentary"}]
                    },
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None

    def test_non_auto_dev_result_parse_is_skipped(self, tmp_path: Path) -> None:
        """Text with a sentinel marker that parses to a non-AutoDevResult is skipped."""
        # BlockedResult also has <<<...>>> markers but is not AutoDevResult
        blocked = (
            "<<<BLOCKED_RESULT\n"
            '{"schema_version": 1, "reason": "test"}\n'
            "BLOCKED_RESULT>>>"
        )
        p = tmp_path / "t.jsonl"
        _make_transcript(
            p,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "message": {"content": [{"type": "text", "text": blocked}]},
                }
            ],
        )
        result = queue_peek.parse_transcript(p)
        assert result["last_sentinel_status"] is None


# ---------------------------------------------------------------------------
# print_table
# ---------------------------------------------------------------------------

_WAIT_ROW = {
    "ticket": "T-1",
    "session": "abc123456789",
    "client": "c",
    "attempts": 1,
    "age_min": 10.0,
    "idle_min": 2.0,
    "stage": None,
    "status": None,
    "pr": None,
    "pr_state": None,
    "recommend": "WAIT",
    "reason": "age 10min — early/healthy",
    "signal_source": "transcript",
    "jsonl_idle_min": None,
}

_STOP_ROW = {
    **_WAIT_ROW,
    "recommend": "STOP",
    "reason": "PR merged + worker idle 10min — stuck",
    "session": "stopabc123456",
    "ticket": "T-2",
}


class TestPrintTable:
    def test_empty_rows_prints_no_running(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([])
        captured = capsys.readouterr()
        assert "No RUNNING tasks found." in captured.out

    def test_wait_row_printed_without_reason_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_WAIT_ROW])
        captured = capsys.readouterr()
        assert "T-1" in captured.out
        assert "└─" not in captured.out

    def test_non_wait_row_prints_reason_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_STOP_ROW])
        captured = capsys.readouterr()
        assert "└─" in captured.out
        assert "stuck" in captured.out

    def test_stop_row_prints_suggested_stops_footer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_STOP_ROW])
        captured = capsys.readouterr()
        assert "Suggested stops:" in captured.out
        assert "cw spawn close" in captured.out

    def test_wait_row_no_suggested_stops_footer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        queue_peek.print_table([_WAIT_ROW])
        captured = capsys.readouterr()
        assert "Suggested stops:" not in captured.out


# ---------------------------------------------------------------------------
# TestBlindRow — blind-labeling when no transcript is resolvable
# ---------------------------------------------------------------------------


def _make_blind_info(jsonl_idle_min: float | None = 12.0) -> dict[str, Any]:
    return {"signal_source": "blind", "jsonl_idle_min": jsonl_idle_min}


class TestBlindRow:
    def test_blind_signal_source(self) -> None:
        task = _make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(), _NOW)
        assert row["signal_source"] == "blind"

    def test_blind_recommend_is_peek_blind(self) -> None:
        task = _make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(), _NOW)
        assert row["recommend"] == queue_peek.RECOMMEND_BLIND
        assert row["recommend"] == "PEEK-BLIND"

    def test_blind_reason_includes_liveness(self) -> None:
        task = _make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=12.0), _NOW)
        assert "no resolvable transcript" in row["reason"]
        assert "12m ago" in row["reason"]

    def test_blind_reason_none_found_when_no_jsonl(self) -> None:
        task = _make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=None), _NOW)
        assert "no resolvable transcript" in row["reason"]
        assert "none found" in row["reason"]

    def test_blind_jsonl_idle_min_propagated(self) -> None:
        task = _make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=7.5), _NOW)
        assert row["jsonl_idle_min"] == 7.5

    def test_blind_jsonl_idle_min_none_when_no_jsonl(self) -> None:
        task = _make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(jsonl_idle_min=None), _NOW)
        assert row["jsonl_idle_min"] is None

    def test_blind_row_has_null_transcript_fields(self) -> None:
        task = _make_ticket_task()
        row = queue_peek.format_row(task, _make_blind_info(), _NOW)
        assert row["age_min"] is None
        assert row["idle_min"] is None
        assert row["stage"] is None
        assert row["status"] is None
        assert row["pr"] is None

    def test_blind_row_includes_stage_high_water(self) -> None:
        task = _make_ticket_task(stage_high_water=Stage.REVIEW)
        row = queue_peek.format_row(task, _make_blind_info(), _NOW)
        assert "stage_high_water" in row
        assert row["stage_high_water"] == Stage.REVIEW

    def test_non_blind_signal_source_is_transcript(self) -> None:
        task = _make_ticket_task()
        info: dict[str, Any] = {
            "first_user_ts": "2026-06-20T11:50:00+00:00",
            "last_asst_ts": "2026-06-20T11:55:00+00:00",
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "signal_source": "transcript",
            "jsonl_idle_min": None,
        }
        row = queue_peek.format_row(task, info, _NOW)
        assert row["signal_source"] == "transcript"
        assert row["jsonl_idle_min"] is None

    def test_format_row_defaults_to_transcript_when_signal_source_absent(
        self,
    ) -> None:
        """Backward compat: info without signal_source defaults to transcript path."""
        task = _make_ticket_task()
        info: dict[str, Any] = {}
        row = queue_peek.format_row(task, info, _NOW)
        assert row["signal_source"] == "transcript"

    def test_peek_blind_excluded_from_suggested_stops(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """PEEK-BLIND does not start with STOP — must not appear in the footer."""
        blind_row = {
            **_WAIT_ROW,
            "recommend": "PEEK-BLIND",
            "reason": "no resolvable transcript; newest jsonl 5m ago",
            "signal_source": "blind",
            "jsonl_idle_min": 5.0,
        }
        queue_peek.print_table([blind_row])
        captured = capsys.readouterr()
        assert "Suggested stops:" not in captured.out

    def test_build_peek_rows_blind_when_no_transcript(self, patched_peek: None) -> None:
        """When find_transcript_for_ticket returns None, row is blind."""
        task = _make_ticket_task("999")
        with (
            patch("cw.queue_peek.load_running_tasks", return_value=[task]),
            patch("cw.queue_peek.find_transcript_for_ticket", return_value=None),
            patch("cw.queue_peek._compute_jsonl_idle_min", return_value=5.0),
        ):
            rows = queue_peek.build_peek_rows(None, _NOW)
        assert len(rows) == 1
        assert rows[0]["signal_source"] == "blind"
        assert rows[0]["recommend"] == "PEEK-BLIND"
        assert rows[0]["jsonl_idle_min"] == 5.0

    def test_compute_jsonl_idle_min_with_jsonl(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """_compute_jsonl_idle_min returns round(elapsed, 1) for the newest *.jsonl."""
        from cw._util import claude_project_dir

        wt = tmp_path / "dev-900"
        wt.mkdir()
        proj = claude_project_dir(wt)
        proj.mkdir(parents=True)
        jsonl = proj / "session.jsonl"
        jsonl.touch()
        # Set mtime to 10 minutes before _NOW
        mtime = _NOW.timestamp() - 600
        import os

        os.utime(jsonl, (mtime, mtime))

        task = _make_ticket_task("900", worktree_path=wt)
        result = queue_peek._compute_jsonl_idle_min(task, _NOW)
        assert result == 10.0

    def test_compute_jsonl_idle_min_no_jsonl_returns_none(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """_compute_jsonl_idle_min returns None when no *.jsonl files exist."""
        from cw._util import claude_project_dir

        wt = tmp_path / "dev-901"
        wt.mkdir()
        proj = claude_project_dir(wt)
        proj.mkdir(parents=True)
        # No .jsonl files

        task = _make_ticket_task("901", worktree_path=wt)
        result = queue_peek._compute_jsonl_idle_min(task, _NOW)
        assert result is None

    def test_compute_jsonl_idle_min_no_worktree_falls_back_to_matching_dirs(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """No worktree_path + no session refs → falls back to _matching_project_dirs."""
        import os

        # Create a project dir whose name ends with "-902" (matches ticket "902")
        proj = queue_peek.CLAUDE_PROJECTS / "-home-user-dev-902"
        proj.mkdir(parents=True)
        jsonl = proj / "session.jsonl"
        jsonl.touch()
        # Set mtime to 5 minutes before _NOW
        mtime = _NOW.timestamp() - 300
        os.utime(jsonl, (mtime, mtime))

        # Task with no worktree_path; no sessions.json written → refs empty
        task = _make_ticket_task("902", worktree_path=None)
        result = queue_peek._compute_jsonl_idle_min(task, _NOW)
        assert result == 5.0
