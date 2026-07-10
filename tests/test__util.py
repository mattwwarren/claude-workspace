"""Tests for cw._util - shared utility helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


class TestIterAssistantTextBlocks:
    """Tests for _iter_assistant_text_blocks (shared transcript walker)."""

    def test_yields_assistant_text_skipping_other_records(self, tmp_path: Path) -> None:
        """Yields assistant text blocks in order, skipping non-assistant and
        malformed records."""
        from cw._util import _iter_assistant_text_blocks

        transcript = tmp_path / "t.jsonl"
        records = [
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "ignored"}],
                    },
                }
            ),
            "{ not valid json",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "first"},
                            {"type": "tool_use", "name": "noise"},
                            {"type": "text", "text": "second"},
                        ],
                    },
                }
            ),
        ]
        transcript.write_text("\n".join(records) + "\n")

        assert list(_iter_assistant_text_blocks(transcript)) == ["first", "second"]

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        from cw._util import _iter_assistant_text_blocks

        assert list(_iter_assistant_text_blocks(tmp_path / "nope.jsonl")) == []

    def test_oserror_on_open_yields_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An I/O error mid-read is swallowed — the walker yields nothing."""
        from cw._util import _iter_assistant_text_blocks

        transcript = tmp_path / "t.jsonl"
        transcript.write_text('{"type": "assistant"}\n')

        def _boom(*_a: object, **_kw: object) -> None:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr("pathlib.Path.open", _boom)
        assert list(_iter_assistant_text_blocks(transcript)) == []


class TestIterSentinelTextBlocks:
    """Tests for _iter_sentinel_text_blocks (assistant text + tool_result stdout).

    A worker may emit the AUTO_DEV_RESULT sentinel via a Bash ``cat`` command,
    landing it in a tool_result block rather than assistant text (#731). The
    sentinel scanner must see both — but NOT user prose or the tool_use command
    echo (which would duplicate the frame / pull in the prompt's schema example).
    """

    def test_yields_assistant_text_and_tool_result_skipping_user_prose(
        self, tmp_path: Path
    ) -> None:
        from cw._util import _iter_sentinel_text_blocks

        transcript = tmp_path / "t.jsonl"
        records = [
            # user prose (e.g. prompt) — must be skipped to avoid false frames
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "prompt prose"}],
                    },
                }
            ),
            # assistant text + tool_use command echo (tool_use must be skipped)
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "narrative"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "cat <<EOF\nframe\nEOF"},
                            },
                        ],
                    },
                }
            ),
            # tool_result (Bash stdout) carrying the real sentinel — string form
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "content": "stdout-sentinel"}
                        ],
                    },
                }
            ),
            # tool_result list form — text sub-blocks yielded
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "list-stdout"}],
                            }
                        ],
                    },
                }
            ),
        ]
        transcript.write_text("\n".join(records) + "\n")

        assert list(_iter_sentinel_text_blocks(transcript)) == [
            "narrative",
            "stdout-sentinel",
            "list-stdout",
        ]

    def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        from cw._util import _iter_sentinel_text_blocks

        assert list(_iter_sentinel_text_blocks(tmp_path / "nope.jsonl")) == []

    def test_malformed_and_non_dict_records_skipped(self, tmp_path: Path) -> None:
        """Invalid JSON, non-dict records, non-list content, non-dict blocks are
        all skipped rather than raising."""
        from cw._util import _iter_sentinel_text_blocks

        transcript = tmp_path / "t.jsonl"
        records = [
            "{ not valid json",
            json.dumps([1, 2, 3]),  # record is not a dict
            json.dumps({"type": "assistant", "message": "not-a-dict"}),
            json.dumps({"type": "assistant", "message": {"content": "not-a-list"}}),
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": ["not-a-dict-block"]},
                }
            ),
        ]
        transcript.write_text("\n".join(records) + "\n")

        assert list(_iter_sentinel_text_blocks(transcript)) == []

    def test_oserror_on_open_yields_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw._util import _iter_sentinel_text_blocks

        transcript = tmp_path / "t.jsonl"
        transcript.write_text('{"type": "assistant"}\n')

        def _boom(*_a: object, **_kw: object) -> None:
            msg = "boom"
            raise OSError(msg)

        monkeypatch.setattr("pathlib.Path.open", _boom)
        assert list(_iter_sentinel_text_blocks(transcript)) == []


class TestParseSentinelFromTranscriptToolResult:
    """Integration test: _parse_sentinel_from_transcript finds sentinel in tool_result.

    Regression for GitHub #774 / #731: the skill-script scanner previously only
    walked assistant text blocks, missing sentinels emitted via Bash stdout
    (tool_result). This test drives the full chain — _parse_sentinel_from_transcript
    → _iter_sentinel_text_blocks → extract_block → parse_stdout — with a transcript
    whose sentinel lives exclusively inside a tool_result block.
    """

    _SENTINEL_TOOL_RESULT = (
        "<<<AUTO_DEV_RESULT\n"
        "{\n"
        '  "schema_version": 4,\n'
        '  "ticket_id": "774",\n'
        '  "status": "stage_complete",\n'
        '  "stage_reached": "stage2_impl",\n'
        '  "scope": {"tier": "small", "files": 2, "lines_estimate": 20,'
        ' "lines_actual": 18, "forbidden_touched": false},\n'
        '  "plan_source": "github_issue_existing",\n'
        '  "branch": "dev/774",\n'
        '  "worktree_path": "/tmp/wt/774",\n'
        '  "fork_point_sha": "abc1234",\n'
        '  "commits": ["sha1"],\n'
        '  "pr": null,\n'
        '  "review": {"must_fix_initial": 0, "should_fix": 0, "fix_cycles_used": 0},\n'
        '  "health": {"lowest_agent_confidence": "HIGH", "any_incomplete_risk": false,'
        ' "shortcuts": [], "recommendation": "PROCEED",'
        ' "downgrade_applied": false, "fix_loop_escalated": false},\n'
        '  "friction_highlights": [],\n'
        '  "blocker": null,\n'
        '  "next_actions": []\n'
        "}\n"
        "AUTO_DEV_RESULT>>>"
    )

    def _write_transcript_tool_result(
        self,
        worktree: Path,
        claude_session_id: str,
        sentinel_text: str,
        home: Path,
    ) -> None:
        """Write a transcript where the sentinel is in a tool_result block only."""
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        # Assistant block with NO sentinel — just a narrative line
        assistant_record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Impl complete."}],
            },
        }
        # User block carrying the tool_result (Bash stdout) with the real sentinel
        tool_result_record = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": sentinel_text}],
            },
        }
        (project_dir / f"{claude_session_id}.jsonl").write_text(
            json.dumps(assistant_record) + "\n" + json.dumps(tool_result_record) + "\n"
        )

    def test_parses_sentinel_from_tool_result_block(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sentinel in tool_result stdout → AutoDevResult, not None."""
        from cw.auto_dev_result import AutoDevResult
        from cw.cli._sentinels import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-774"
        worktree.mkdir(parents=True)
        self._write_transcript_tool_result(
            worktree, "uuid-774", self._SENTINEL_TOOL_RESULT, fake_home
        )

        parsed = _parse_sentinel_from_transcript(str(worktree), "uuid-774")
        assert isinstance(parsed, AutoDevResult)
        assert parsed.ticket_id == "774"
        assert parsed.status == "stage_complete"

    def test_no_sentinel_in_assistant_text_alone_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity: when sentinel is NOT in tool_result or assistant text → None."""
        from cw.cli._sentinels import _parse_sentinel_from_transcript

        fake_home = tmp_path / "fake-home"
        monkeypatch.setattr("cw._util.Path.home", lambda: fake_home)

        worktree = tmp_path / "wt" / "auto-dev-774b"
        worktree.mkdir(parents=True)
        encoded = str(worktree).replace("/", "-").replace(".", "-")
        project_dir = fake_home / ".claude" / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        # Only a plain assistant narrative — no sentinel anywhere
        record = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Just a progress note."}],
            },
        }
        (project_dir / "uuid-774b.jsonl").write_text(json.dumps(record) + "\n")

        assert _parse_sentinel_from_transcript(str(worktree), "uuid-774b") is None


class TestShortenWorktree:
    """Tests for _shorten_worktree (shared worktree-path display helper)."""

    def test_home_collapsed_to_tilde(self) -> None:
        from cw._util import _shorten_worktree

        out = _shorten_worktree(Path("/home/u/wt/dev-1"), "/home/u")
        assert out == "~/wt/dev-1"

    def test_long_path_capped(self) -> None:
        from cw._util import _shorten_worktree

        long_path = "/very/long/worktree/path/" + ("segment/" * 8) + "end"
        out = _shorten_worktree(long_path, "")
        assert out.startswith("…")
        assert len(out) <= 40

    def test_none_renders_dash(self) -> None:
        from cw._util import _shorten_worktree

        assert _shorten_worktree(None, "/home/u") == "—"


class TestMcpExtraMsg:
    """Tests for MCP_EXTRA_MSG (shared channel-server remediation constant)."""

    def test_names_uv_tool_install_remediation(self) -> None:
        from cw._util import MCP_EXTRA_MSG

        assert "channel server requires [mcp] extra" in MCP_EXTRA_MSG
        assert "uv tool install" in MCP_EXTRA_MSG
