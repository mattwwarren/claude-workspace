"""Tests for the ``claude -p`` seam ``cw.claude_review._runner`` (#2791).

No test here reaches a real ``claude``: ``RealClaudeRunner`` runs against a
patched ``subprocess.Popen``, and ``_which_claude`` against a patched
``shutil.which``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from cw.claude_review import (
    CLAUDE_REVIEW_TOOLS,
    ClaudeRunner,
    ClaudeRunResult,
    FakeClaudeRunner,
    RealClaudeRunner,
    _which_claude,
    build_claude_review_argv,
)
from tests._claude_review_helpers import CAPTURED_REVIEW_ENVELOPE

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA = '{"title": "ReviewerFindingsDocument"}'
_WRITE_OR_SHELL_TOOLS = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit")


def _value_after(argv: list[str], flag: str) -> str:
    assert argv.count(flag) == 1, f"{flag} must appear exactly once"
    return argv[argv.index(flag) + 1]


class TestBuildClaudeReviewArgv:
    def test_pins_every_posture_flag(self) -> None:
        argv = build_claude_review_argv(model=None, schema=_SCHEMA)
        assert argv == [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            _SCHEMA,
            "--tools",
            "Read,Grep,Glob",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]

    def test_model_is_appended_only_when_set(self) -> None:
        assert "--model" not in build_claude_review_argv(model=None, schema=_SCHEMA)
        assert "--model" not in build_claude_review_argv(model="", schema=_SCHEMA)
        argv = build_claude_review_argv(model="claude-sonnet-5-5", schema=_SCHEMA)
        assert argv[-2:] == ["--model", "claude-sonnet-5-5"]

    def test_tool_set_is_exactly_the_read_only_three(self) -> None:
        argv = build_claude_review_argv(model="m", schema=_SCHEMA)
        assert _value_after(argv, "--tools") == "Read,Grep,Glob"
        assert CLAUDE_REVIEW_TOOLS == "Read,Grep,Glob"

    def test_no_widening_flag_or_write_tool_appears(self) -> None:
        argv = build_claude_review_argv(model="m", schema=_SCHEMA)
        joined = " ".join(argv)
        assert "--add-dir" not in argv
        assert "bypassPermissions" not in joined
        assert "--dangerously-skip-permissions" not in argv
        assert "--mcp-config" not in argv
        assert "--restricted" not in argv
        for tool in _WRITE_OR_SHELL_TOOLS:
            assert tool not in _value_after(argv, "--tools")

    def test_settings_sources_are_the_empty_set(self) -> None:
        """DP3 variant adopted: one empty argv element, no user or project hooks."""
        argv = build_claude_review_argv(model=None, schema=_SCHEMA)
        assert _value_after(argv, "--setting-sources") == ""
        assert _value_after(argv, "--permission-mode") == "dontAsk"

    def test_the_prompt_never_travels_in_argv(self) -> None:
        argv = build_claude_review_argv(model="m", schema=_SCHEMA)
        # -p with no positional prompt: every element is a flag or a flag's
        # value, so the prompt can only arrive over stdin.
        flags_with_values = {
            "--output-format",
            "--json-schema",
            "--tools",
            "--permission-mode",
            "--setting-sources",
            "--model",
        }
        values = {argv[i + 1] for i, a in enumerate(argv) if a in flags_with_values}
        assert argv[1] == "-p"
        positional = [a for a in argv[1:] if not a.startswith("-") and a not in values]
        assert positional == []


class TestRealClaudeRunner:
    def _proc(self, *, stdout: str = "", stderr: str = "", rc: int = 0) -> MagicMock:
        proc = MagicMock()
        proc.communicate.return_value = (stdout, stderr)
        proc.returncode = rc
        return proc

    def test_success_returns_stdout_and_runs_in_the_worktree(
        self, tmp_path: Path
    ) -> None:
        proc = self._proc(stdout=CAPTURED_REVIEW_ENVELOPE)
        with patch("subprocess.Popen", return_value=proc) as popen:
            result = RealClaudeRunner().run(
                tmp_path, ["claude", "-p"], 60, stdin="the prompt"
            )
        assert result == ClaudeRunResult(
            returncode=0, stdout=CAPTURED_REVIEW_ENVELOPE, stderr=""
        )
        kwargs = popen.call_args.kwargs
        assert kwargs["cwd"] == tmp_path
        assert kwargs["stdin"] is subprocess.PIPE
        assert "env" not in kwargs
        proc.communicate.assert_called_once_with(input="the prompt", timeout=60)

    def test_missing_binary_is_exit_127(self, tmp_path: Path) -> None:
        with patch("subprocess.Popen", side_effect=FileNotFoundError("claude")):
            result = RealClaudeRunner().run(tmp_path, ["claude", "-p"], None, stdin="")
        assert result.returncode == 127
        assert result.stderr == "claude: command not found"
        assert result.timed_out is False

    def test_timeout_kills_and_reports_timed_out(self, tmp_path: Path) -> None:
        proc = self._proc()
        proc.communicate.side_effect = subprocess.TimeoutExpired("claude", 5)
        with patch("subprocess.Popen", return_value=proc):
            result = RealClaudeRunner().run(tmp_path, ["claude"], 5, stdin="p")
        assert result.timed_out is True
        assert result.returncode == -1
        proc.kill.assert_called_once()
        proc.wait.assert_called_once()

    def test_stderr_is_capped(self, tmp_path: Path) -> None:
        proc = self._proc(stderr="x" * 10_000 + "TAIL", rc=1)
        with patch("subprocess.Popen", return_value=proc):
            result = RealClaudeRunner().run(tmp_path, ["claude"], None, stdin="p")
        assert len(result.stderr) == 4000
        assert result.stderr.endswith("TAIL")
        assert result.returncode == 1

    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(RealClaudeRunner(), ClaudeRunner)
        assert isinstance(FakeClaudeRunner(), ClaudeRunner)


class TestFakeClaudeRunner:
    def test_records_argv_cwd_timeout_and_stdin(self, tmp_path: Path) -> None:
        runner = FakeClaudeRunner(stdout="out")
        argv = ["claude", "-p"]
        result = runner.run(tmp_path, argv, 42, stdin="prompt")
        argv.append("mutated-after")
        assert runner.calls == [
            {
                "argv": ["claude", "-p"],
                "cwd": tmp_path,
                "timeout": 42,
                "stdin": "prompt",
            }
        ]
        assert result.stdout == "out"

    def test_replays_results_in_order_then_falls_back(self, tmp_path: Path) -> None:
        first = ClaudeRunResult(returncode=1, stdout="", stderr="boom")
        runner = FakeClaudeRunner(stdout="default", results=[first])
        assert runner.run(tmp_path, [], None, stdin="") is first
        assert runner.run(tmp_path, [], None, stdin="").stdout == "default"

    def test_simulated_timeout(self, tmp_path: Path) -> None:
        result = FakeClaudeRunner(simulate_timeout=True).run(tmp_path, [], 1, stdin="")
        assert result.timed_out is True

    def test_side_effect_sees_the_worktree(self, tmp_path: Path) -> None:
        seen: list[Path] = []
        FakeClaudeRunner(side_effect=seen.append).run(tmp_path, [], None, stdin="")
        assert seen == [tmp_path]


class TestWhichClaude:
    @pytest.mark.parametrize("found", ["/usr/bin/claude", None])
    def test_resolves_the_claude_binary(self, found: str | None) -> None:
        with patch("shutil.which", return_value=found) as which:
            assert _which_claude() == found
        which.assert_called_once_with("claude")
