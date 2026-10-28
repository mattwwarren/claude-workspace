"""Tests for ``cw.queue_peek.locate`` -- session refs and transcript location."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cw import queue_peek
from cw.queue_peek import locate
from tests._opencode_helpers import write_opencode_log
from tests._queue_peek_helpers import (
    NOW,
    make_jsonl,
    make_ticket_task,
    redirect_peek_paths,
    write_sessions,
)


@pytest.fixture
def patched_peek(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect ``locate.CLAUDE_PROJECTS`` and ``locate.CW_STATE`` to tmp_path."""
    redirect_peek_paths(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# find_transcript_for_ticket
# ---------------------------------------------------------------------------


class TestFindTranscriptForTicket:
    def test_no_project_dir_returns_none(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        locate.CLAUDE_PROJECTS = tmp_path / "nonexistent"
        assert queue_peek.find_transcript_for_ticket("143") is None

    def test_no_matching_project_dir_returns_none(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        unrelated = locate.CLAUDE_PROJECTS / "-home-cw-auto-dev-999"
        unrelated.mkdir()
        assert queue_peek.find_transcript_for_ticket("143") is None

    def test_single_transcript_returned(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        proj = locate.CLAUDE_PROJECTS / "-home-cw-auto-dev-143"
        expected = make_jsonl(
            proj,
            "aaaa1111-0000-0000-0000-000000000000",
            "2026-05-27T01:00:00Z",
            "/auto-dev 143 --headless",
        )
        result = queue_peek.find_transcript_for_ticket("143")
        assert result == expected

    def test_two_runs_picks_latest(self, patched_peek: None, tmp_path: Path) -> None:
        """Regression test for GH #358: two dispatch runs → picks the fresh one."""
        proj = locate.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = make_jsonl(
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
        proj = locate.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        fresh_uuid = "23d81c88-0000-0000-0000-000000000000"
        make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = make_jsonl(
            proj,
            fresh_uuid,
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        write_sessions(
            locate.CW_STATE,
            [{"id": "23d81c88", "claude_session_id": fresh_uuid, "status": "active"}],
        )
        result = queue_peek.find_transcript_for_ticket("143", session_id="23d81c88")
        assert result == fresh

    def test_session_id_not_in_sessions_json_falls_back_to_latest(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """If session_id has no match in sessions.json, falls back to latest."""
        proj = locate.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = make_jsonl(
            proj,
            "fresh000-0000-0000-0000-000000000000",
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        write_sessions(locate.CW_STATE, [])
        result = queue_peek.find_transcript_for_ticket("143", session_id="unknown1")
        assert result == fresh

    def test_session_id_resolves_but_jsonl_missing_falls_back_to_latest(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """If session resolves but jsonl doesn't exist on disk, falls back to latest."""
        proj = locate.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        make_jsonl(
            proj,
            "old00000-0000-0000-0000-000000000000",
            "2026-05-27T02:00:00Z",
            "/auto-dev 143 --headless",
        )
        fresh = make_jsonl(
            proj,
            "fresh000-0000-0000-0000-000000000000",
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        # session maps to a UUID that has no corresponding jsonl file
        write_sessions(
            locate.CW_STATE,
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
        proj = locate.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
        main_jsonl = make_jsonl(
            proj,
            "main0000-0000-0000-0000-000000000000",
            "2026-05-29T02:35:00Z",
            "/auto-dev 143 --headless",
        )
        # Subagent: different first user text → score=1 but later timestamp
        make_jsonl(
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
        proj = locate.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-200"
        expected = make_jsonl(
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
        proj.mkdir(parents=True, exist_ok=True)
        jsonl.write_text("")

        session_id = "deadbeef"
        write_sessions(
            locate.CW_STATE,
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
        proj.mkdir(parents=True, exist_ok=True)
        surface_ref = "cafe1234"
        jsonl = proj / f"{surface_ref}-full-uuid.jsonl"
        jsonl.write_text("")

        session_id = "cafe1234"
        write_sessions(
            locate.CW_STATE,
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
        proj.mkdir(parents=True, exist_ok=True)
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
        proj = locate.CLAUDE_PROJECTS / "-home-cw-auto-dev-503"
        make_jsonl(
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
        proj.mkdir(parents=True, exist_ok=True)
        csid = "feed0000-0000-0000-0000-000000000000"
        jsonl = proj / f"{csid}.jsonl"
        jsonl.write_text("")

        session_id = "feedbeef"
        write_sessions(
            locate.CW_STATE,
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

    def test_daemon_task_no_worktree_path_no_session_row_uses_heuristic(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """No matching Session row and no worktree_path: legacy heuristic kept.

        With no row at all the session's ID state is unknown, so the name-based
        project-dir heuristic still applies (#2417 preserves this behavior).
        """
        write_sessions(locate.CW_STATE, [{"id": "other999"}])
        proj = locate.CLAUDE_PROJECTS / "-home-cw-dev-504"
        make_jsonl(
            proj,
            "cccc0000-0000-0000-0000-000000000000",
            "2026-06-01T10:00:00Z",
            "/auto-dev 504 --headless",
        )
        result = queue_peek.find_transcript_for_ticket(
            "504", session_id="cafe0000", worktree_path=None
        )
        assert result is not None
        assert result.name == "cccc0000-0000-0000-0000-000000000000.jsonl"

    def test_daemon_task_matched_both_null_session_suppresses_heuristic(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """A matched Session with both Claude IDs null never borrows a transcript.

        Same fixture as the no-row variant above, but the Session row exists
        with ``claude_session_id`` and ``surface_ref`` both null: the legacy
        ticket-name heuristic would attribute another run's transcript to it
        (#2417), so resolution returns None (the row renders blind).
        """
        session_id = "cafe0000"
        write_sessions(
            locate.CW_STATE,
            [{"id": session_id, "claude_session_id": None, "worktree_path": None}],
        )
        proj = locate.CLAUDE_PROJECTS / "-home-cw-dev-504"
        make_jsonl(
            proj,
            "cccc0000-0000-0000-0000-000000000000",
            "2026-06-01T10:00:00Z",
            "/auto-dev 504 --headless",
        )
        assert (
            queue_peek.find_transcript_for_ticket(
                "504", session_id=session_id, worktree_path=None
            )
            is None
        )


# ---------------------------------------------------------------------------
# _matching_project_dirs — endswith fix
# ---------------------------------------------------------------------------


class TestMatchingProjectDirs:
    def test_matches_dev_prefix_dir(self, patched_peek: None) -> None:
        """dev-816 slug now matches ticket 816 (the regression this PR fixes)."""
        proj = locate.CLAUDE_PROJECTS / "-home-matthew--cw-wt-7dc983e2-dev-816"
        proj.mkdir(parents=True)
        result = queue_peek._matching_project_dirs("816")
        assert proj in result

    def test_matches_auto_dev_prefix_dir(self, patched_peek: None) -> None:
        """auto-dev-816 dirs still match — regression guard for other clients."""
        proj = locate.CLAUDE_PROJECTS / "-home-user-projects-auto-dev-816"
        proj.mkdir(parents=True)
        result = queue_peek._matching_project_dirs("816")
        assert proj in result

    def test_does_not_match_longer_id(self, patched_peek: None) -> None:
        """dev-8160 does not match ticket 816 — no substring false positives."""
        proj = locate.CLAUDE_PROJECTS / "-home-user-dev-8160"
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
        write_sessions(
            locate.CW_STATE,
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
        assert refs["matched"] is True
        assert refs["local_liveness"] is None

    def test_exposes_local_liveness_handle(self, patched_peek: None) -> None:
        handle = {"pid": 4242, "start_time_ns": 7, "backend": "codex"}
        write_sessions(
            locate.CW_STATE,
            [{"id": "abc12345", "local_liveness": handle}],
        )
        refs = queue_peek._load_session_refs("abc12345")
        assert refs["matched"] is True
        assert refs["local_liveness"] == handle

    def test_returns_empty_dict_when_not_found(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        write_sessions(locate.CW_STATE, [{"id": "other111"}])
        assert queue_peek._load_session_refs("abc12345") == {}

    def test_handles_corrupt_json_gracefully(self, patched_peek: None) -> None:
        locate.CW_STATE.write_text("{bad json")
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
        write_sessions(
            locate.CW_STATE,
            [{"id": "23d81c88", "claude_session_id": uuid, "status": "active"}],
        )
        assert queue_peek.load_claude_session_id("23d81c88") == uuid

    def test_returns_none_when_id_not_in_sessions(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        write_sessions(
            locate.CW_STATE,
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
        locate.CW_STATE.write_text("{not valid json}")
        assert queue_peek.load_claude_session_id("abc12345") is None


# ---------------------------------------------------------------------------
# _find_transcript_heuristic — missing branches
# ---------------------------------------------------------------------------


class TestFindTranscriptHeuristic:
    def test_skips_corrupt_json_lines(self, patched_peek: None, tmp_path: Path) -> None:
        """json.JSONDecodeError lines in the jsonl are skipped, not raised."""
        proj = locate.CLAUDE_PROJECTS / "-home-cw-auto-dev-100"
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
        proj = locate.CLAUDE_PROJECTS / "-home-cw-auto-dev-101"
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


def test_find_transcript_finds_opencode_log(patched_peek: None, tmp_path: Path) -> None:
    """find_transcript_for_ticket returns .cw/opencode.log when it exists."""
    worktree = tmp_path / "wt-opencode-peek"
    worktree.mkdir()
    write_opencode_log(worktree, [{"type": "text", "part": {"text": "hello"}}])

    result = queue_peek.find_transcript_for_ticket(
        "T-1", session_id=None, worktree_path=worktree
    )
    assert result is not None
    assert result.name == "opencode.log"
    assert result.parent.name == ".cw"


def test_find_transcript_prefers_opencode_over_claude(
    patched_peek: None, tmp_path: Path
) -> None:
    """When both opencode.log and a claude transcript exist, opencode.log wins."""
    from cw._util import claude_project_dir

    worktree = tmp_path / "wt-both"
    worktree.mkdir()
    write_opencode_log(worktree, [{"type": "text", "part": {"text": "opencode"}}])

    # Also create a claude transcript
    proj = claude_project_dir(worktree)
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "session.jsonl").write_text('{"type":"init"}\n')

    result = queue_peek.find_transcript_for_ticket(
        "T-1", session_id=None, worktree_path=worktree
    )
    assert result is not None
    assert result.name == "opencode.log"


# ---------------------------------------------------------------------------
# _compute_jsonl_idle_min
# ---------------------------------------------------------------------------


class TestComputeJsonlIdleMin:
    def test_compute_jsonl_idle_min_with_jsonl(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """_compute_jsonl_idle_min returns round(elapsed, 1) for the newest *.jsonl."""
        from cw._util import claude_project_dir

        wt = tmp_path / "dev-900"
        wt.mkdir()
        proj = claude_project_dir(wt)
        proj.mkdir(parents=True, exist_ok=True)
        jsonl = proj / "session.jsonl"
        jsonl.touch()
        # Set mtime to 10 minutes before NOW
        mtime = NOW.timestamp() - 600
        import os

        os.utime(jsonl, (mtime, mtime))

        task = make_ticket_task("900", worktree_path=wt)
        result = queue_peek._compute_jsonl_idle_min(task, NOW)
        assert result == 10.0

    def test_compute_jsonl_idle_min_no_jsonl_returns_none(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """_compute_jsonl_idle_min returns None when no *.jsonl files exist."""
        from cw._util import claude_project_dir

        wt = tmp_path / "dev-901"
        wt.mkdir()
        proj = claude_project_dir(wt)
        proj.mkdir(parents=True, exist_ok=True)
        # No .jsonl files

        task = make_ticket_task("901", worktree_path=wt)
        result = queue_peek._compute_jsonl_idle_min(task, NOW)
        assert result is None

    def test_compute_jsonl_idle_min_no_worktree_falls_back_to_matching_dirs(
        self, patched_peek: None, tmp_path: Path
    ) -> None:
        """No worktree_path + no session refs → falls back to _matching_project_dirs."""
        import os

        # Create a project dir whose name ends with "-902" (matches ticket "902")
        proj = locate.CLAUDE_PROJECTS / "-home-user-dev-902"
        proj.mkdir(parents=True)
        jsonl = proj / "session.jsonl"
        jsonl.touch()
        # Set mtime to 5 minutes before NOW
        mtime = NOW.timestamp() - 300
        os.utime(jsonl, (mtime, mtime))

        # Task with no worktree_path; no sessions.json written → refs empty
        task = make_ticket_task("902", worktree_path=None)
        result = queue_peek._compute_jsonl_idle_min(task, NOW)
        assert result == 5.0
