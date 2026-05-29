"""Tests for cw_queue_peek.py — transcript selection logic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _load_peek_module() -> Any:
    script = Path(__file__).parent.parent / ".claude" / "scripts" / "cw_queue_peek.py"
    spec = importlib.util.spec_from_file_location("cw_queue_peek", script)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def peek(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Load the peek module with CLAUDE_PROJECTS and CW_STATE redirected to tmp_path."""
    mod = _load_peek_module()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    state_file = tmp_path / "sessions.json"
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS", projects_dir)
    monkeypatch.setattr(mod, "CW_STATE", state_file)
    return mod


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
    def test_no_project_dir_returns_none(self, peek: Any, tmp_path: Path) -> None:
        peek.CLAUDE_PROJECTS = tmp_path / "nonexistent"
        assert peek.find_transcript_for_ticket("143") is None

    def test_no_matching_project_dir_returns_none(
        self, peek: Any, tmp_path: Path
    ) -> None:
        unrelated = peek.CLAUDE_PROJECTS / "-home-cw-auto-dev-999"
        unrelated.mkdir()
        assert peek.find_transcript_for_ticket("143") is None

    def test_single_transcript_returned(self, peek: Any, tmp_path: Path) -> None:
        proj = peek.CLAUDE_PROJECTS / "-home-cw-auto-dev-143"
        expected = _make_jsonl(
            proj,
            "aaaa1111-0000-0000-0000-000000000000",
            "2026-05-27T01:00:00Z",
            "/auto-dev 143 --headless",
        )
        result = peek.find_transcript_for_ticket("143")
        assert result == expected

    def test_two_runs_picks_latest(self, peek: Any, tmp_path: Path) -> None:
        """Regression test for GH #358: two dispatch runs → picks the fresh one."""
        proj = peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
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
        result = peek.find_transcript_for_ticket("143")
        assert result == fresh

    def test_session_id_match_selects_exact_jsonl(
        self, peek: Any, tmp_path: Path
    ) -> None:
        """When session_id resolves to a claude_session_id, that exact jsonl is used."""
        proj = peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
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
            peek.CW_STATE,
            [{"id": "23d81c88", "claude_session_id": fresh_uuid, "status": "active"}],
        )
        result = peek.find_transcript_for_ticket("143", session_id="23d81c88")
        assert result == fresh

    def test_session_id_not_in_sessions_json_falls_back_to_latest(
        self, peek: Any, tmp_path: Path
    ) -> None:
        """If session_id has no match in sessions.json, falls back to latest."""
        proj = peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
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
        _write_sessions(peek.CW_STATE, [])
        result = peek.find_transcript_for_ticket("143", session_id="unknown1")
        assert result == fresh

    def test_session_id_resolves_but_jsonl_missing_falls_back_to_latest(
        self, peek: Any, tmp_path: Path
    ) -> None:
        """If session resolves but jsonl doesn't exist on disk, falls back to latest."""
        proj = peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
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
            peek.CW_STATE,
            [
                {
                    "id": "23d81c88",
                    "claude_session_id": "23d81c88-0000-0000-0000-000000000000",
                    "status": "active",
                }
            ],
        )
        result = peek.find_transcript_for_ticket("143", session_id="23d81c88")
        assert result == fresh

    def test_main_vs_subagent_within_single_run(
        self, peek: Any, tmp_path: Path
    ) -> None:
        """Within one run, main session (score=0) wins over subagent (score=1)."""
        proj = peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-143"
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
        result = peek.find_transcript_for_ticket("143")
        assert result == main

    def test_no_session_id_arg_uses_heuristic(self, peek: Any, tmp_path: Path) -> None:
        """Calling without session_id still works (backward compat)."""
        proj = peek.CLAUDE_PROJECTS / "-home-cw-wt-auto-dev-200"
        expected = _make_jsonl(
            proj,
            "bbbb2222-0000-0000-0000-000000000000",
            "2026-05-29T03:00:00Z",
            "/auto-dev 200 --headless",
        )
        result = peek.find_transcript_for_ticket("200")
        assert result == expected


# ---------------------------------------------------------------------------
# load_claude_session_id
# ---------------------------------------------------------------------------


class TestLoadClaudeSessionId:
    def test_returns_none_when_no_state_file(self, peek: Any, tmp_path: Path) -> None:
        assert peek.load_claude_session_id("abc12345") is None

    def test_returns_none_when_session_id_is_none(
        self, peek: Any, tmp_path: Path
    ) -> None:
        assert peek.load_claude_session_id(None) is None

    def test_returns_claude_session_id_when_found(
        self, peek: Any, tmp_path: Path
    ) -> None:
        uuid = "23d81c88-1234-5678-abcd-000000000000"
        _write_sessions(
            peek.CW_STATE,
            [{"id": "23d81c88", "claude_session_id": uuid, "status": "active"}],
        )
        assert peek.load_claude_session_id("23d81c88") == uuid

    def test_returns_none_when_id_not_in_sessions(
        self, peek: Any, tmp_path: Path
    ) -> None:
        _write_sessions(
            peek.CW_STATE,
            [
                {
                    "id": "other123",
                    "claude_session_id": "other123-0000-0000-0000-000000000000",
                }
            ],
        )
        assert peek.load_claude_session_id("notfound") is None

    def test_handles_corrupt_sessions_json_gracefully(
        self, peek: Any, tmp_path: Path
    ) -> None:
        peek.CW_STATE.write_text("{not valid json}")
        assert peek.load_claude_session_id("abc12345") is None
