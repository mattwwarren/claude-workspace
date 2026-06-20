"""Tests for cw.queue_peek — transcript selection logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cw import queue_peek
from cw.cli._base import main


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


def _write_sessions(state_file: Path, sessions: list[dict]) -> None:  # type: ignore[type-arg]
    state_file.write_text(json.dumps({"sessions": sessions}))


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
        main = _make_jsonl(
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
        assert result == main

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
# cw queue peek CLI command
# ---------------------------------------------------------------------------

_SAMPLE_ROWS = [
    {
        "ticket": 143,
        "session": "abc123456789",
        "client": "test-client",
        "attempts": 1,
        "age_min": 12.5,
        "idle_min": 2.1,
        "stage": None,
        "status": None,
        "pr": None,
        "pr_state": None,
        "recommend": "WAIT",
        "reason": "age 12min — early/healthy",
    }
]


class TestQueuePeekCli:
    def test_table_output_calls_print_table(self) -> None:
        """Default (no --json) routes through print_table."""
        with (
            patch("cw.queue_peek.build_peek_rows", return_value=_SAMPLE_ROWS),
            patch("cw.queue_peek.print_table") as mock_print,
        ):
            result = CliRunner().invoke(main, ["queue", "peek"])
        assert result.exit_code == 0
        mock_print.assert_called_once_with(_SAMPLE_ROWS)

    def test_json_flag_emits_json(self) -> None:
        """--json flag emits JSON to stdout instead of calling print_table."""
        with (
            patch("cw.queue_peek.build_peek_rows", return_value=_SAMPLE_ROWS),
            patch("cw.queue_peek.print_table") as mock_print,
        ):
            result = CliRunner().invoke(main, ["queue", "peek", "--json"])
        assert result.exit_code == 0
        mock_print.assert_not_called()
        parsed = json.loads(result.output)
        assert parsed[0]["ticket"] == 143

    def test_client_filter_passed_through(self) -> None:
        """--client option is forwarded to build_peek_rows."""
        with (
            patch("cw.queue_peek.build_peek_rows", return_value=[]) as mock_build,
            patch("cw.queue_peek.print_table"),
        ):
            result = CliRunner().invoke(main, ["queue", "peek", "--client", "myorg"])
        assert result.exit_code == 0
        mock_build.assert_called_once()
        call_client = mock_build.call_args[0][0]
        assert call_client == "myorg"
