"""Tests for cw.handoff - handoff document parsing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from cw.handoff import (
    build_cross_session_prompt,
    build_daemon_workflow_prompt,
    extract_resumption_prompt,
    find_handoffs_newer_than,
    find_latest_handoff,
    parse_handoff_reason,
)
from cw.models import HandoffReason, SessionPurpose, TaskSpec

if TYPE_CHECKING:
    from pathlib import Path


class TestFindLatestHandoff:
    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        result = find_latest_handoff(tmp_path / "nonexistent")
        assert result is None

    def test_no_matching_files_returns_none(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()
        (handoffs_dir / "other-file.md").write_text("not a session handoff")
        result = find_latest_handoff(tmp_path)
        assert result is None

    def test_returns_newest_by_mtime(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()

        older = handoffs_dir / "session-001.md"
        older.write_text("older handoff")

        # Ensure different mtimes
        time.sleep(0.05)

        newer = handoffs_dir / "session-002.md"
        newer.write_text("newer handoff")

        result = find_latest_handoff(tmp_path)
        assert result is not None
        assert result.name == "session-002.md"

    def test_single_file(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()
        only = handoffs_dir / "session-abc.md"
        only.write_text("only handoff")

        result = find_latest_handoff(tmp_path)
        assert result is not None
        assert result.name == "session-abc.md"


class TestFindHandoffsNewerThan:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        result = find_handoffs_newer_than(tmp_path / "nonexistent", 0.0)
        assert result == []

    def test_filters_by_mtime(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()

        old = handoffs_dir / "session-old.md"
        old.write_text("old")

        cutoff = time.time()
        time.sleep(0.05)

        new = handoffs_dir / "session-new.md"
        new.write_text("new")

        result = find_handoffs_newer_than(tmp_path, cutoff)
        assert len(result) == 1
        assert result[0].name == "session-new.md"

    def test_sorted_newest_first(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()

        cutoff = time.time()
        time.sleep(0.05)

        first = handoffs_dir / "session-first.md"
        first.write_text("first")
        time.sleep(0.05)

        second = handoffs_dir / "session-second.md"
        second.write_text("second")

        result = find_handoffs_newer_than(tmp_path, cutoff)
        assert len(result) == 2
        assert result[0].name == "session-second.md"
        assert result[1].name == "session-first.md"

    def test_no_files_newer(self, tmp_path: Path) -> None:
        handoffs_dir = tmp_path / ".handoffs"
        handoffs_dir.mkdir()
        (handoffs_dir / "session-old.md").write_text("old")

        future = time.time() + 9999
        result = find_handoffs_newer_than(tmp_path, future)
        assert result == []


class TestExtractResumptionPrompt:
    def test_extracts_from_valid_handoff(self, sample_handoff_file: Path) -> None:
        result = extract_resumption_prompt(sample_handoff_file)
        assert result is not None
        assert "Continue working on the auth feature" in result
        assert "signup endpoint still needs validation" in result

    def test_returns_none_if_section_missing(self, tmp_path: Path) -> None:
        f = tmp_path / "no-section.md"
        f.write_text("# Handoff\n\nNo resumption section here.\n")
        result = extract_resumption_prompt(f)
        assert result is None

    def test_returns_none_if_code_block_missing(self, tmp_path: Path) -> None:
        f = tmp_path / "no-codeblock.md"
        f.write_text(
            "# Handoff\n\n"
            "## Resumption Prompt\n\n"
            "Just some text without a code block.\n"
        )
        result = extract_resumption_prompt(f)
        assert result is None

    def test_multiline_prompt(self, tmp_path: Path) -> None:
        f = tmp_path / "multiline.md"
        f.write_text(
            "## Resumption Prompt\n\n"
            "```\n"
            "Line 1\n"
            "Line 2\n"
            "Line 3\n"
            "```\n"
        )
        result = extract_resumption_prompt(f)
        assert result is not None
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_prompt_is_stripped(self, tmp_path: Path) -> None:
        f = tmp_path / "padded.md"
        f.write_text(
            "## Resumption Prompt\n\n"
            "```\n"
            "\n  Resume the work  \n\n"
            "```\n"
        )
        result = extract_resumption_prompt(f)
        assert result == "Resume the work"


class TestBuildCrossSessionPrompt:
    def test_with_branch_and_prompt(self) -> None:
        result = build_cross_session_prompt(
            "impl", "review", "feat/search", "Continue the auth work.",
        )
        assert "impl → review" in result
        assert "feat/search" in result
        assert "Continue the auth work." in result

    def test_without_branch(self) -> None:
        result = build_cross_session_prompt(
            "impl", "review", None, "Some context.",
        )
        assert "impl → review" in result
        assert "branch" not in result.lower()
        assert "Some context." in result

    def test_without_raw_prompt(self) -> None:
        result = build_cross_session_prompt(
            "impl", "review", "feat/search", None,
        )
        assert "impl → review" in result
        assert "No resumption context" in result

    def test_without_branch_or_prompt(self) -> None:
        result = build_cross_session_prompt("impl", "review", None, None)
        assert "impl → review" in result
        assert "No resumption context" in result


class TestParseHandoffReason:
    def test_normal_session_done_returns_none(self, tmp_path: Path) -> None:
        """Normal /session-done handoffs lack frontmatter with a reason field."""
        f = tmp_path / "session-done.md"
        f.write_text(
            "# Session Handoff\n\n"
            "## Summary\n\n"
            "Work is complete.\n"
        )
        assert parse_handoff_reason(f) is None

    def test_context_reason(self, tmp_path: Path) -> None:
        f = tmp_path / "session-context.md"
        f.write_text(
            "---\n"
            "reason: context\n"
            "---\n"
            "# Handoff\n\n"
            "Context exhausted.\n"
        )
        assert parse_handoff_reason(f) is HandoffReason.CONTEXT

    def test_debug_fork_reason(self, tmp_path: Path) -> None:
        f = tmp_path / "session-debug.md"
        f.write_text(
            "---\n"
            "reason: debug-fork\n"
            "---\n"
            "# Handoff\n"
        )
        assert parse_handoff_reason(f) is HandoffReason.DEBUG_FORK

    def test_scope_reason(self, tmp_path: Path) -> None:
        f = tmp_path / "session-scope.md"
        f.write_text(
            "---\n"
            "reason: scope\n"
            "---\n"
            "# Handoff\n"
        )
        assert parse_handoff_reason(f) is HandoffReason.SCOPE

    def test_unknown_reason_returns_none(self, tmp_path: Path) -> None:
        """Unrecognised reason values are ignored (treated as normal)."""
        f = tmp_path / "session-typo.md"
        f.write_text(
            "---\n"
            "reason: contxt\n"
            "---\n"
            "# Handoff\n"
        )
        assert parse_handoff_reason(f) is None

    def test_frontmatter_without_reason_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "session-no-reason.md"
        f.write_text(
            "---\n"
            "title: Some handoff\n"
            "---\n"
            "# Handoff\n"
        )
        assert parse_handoff_reason(f) is None

    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "does-not-exist.md"
        assert parse_handoff_reason(f) is None

    def test_reason_with_extra_whitespace(self, tmp_path: Path) -> None:
        f = tmp_path / "session-ws.md"
        f.write_text(
            "---\n"
            "reason:   context  \n"
            "---\n"
            "# Handoff\n"
        )
        assert parse_handoff_reason(f) is HandoffReason.CONTEXT


class TestBuildDaemonWorkflowPrompt:
    def _make_task(self) -> TaskSpec:
        return TaskSpec(
            description="Fix ruff violations in session.py",
            purpose=SessionPurpose.DEBT,
            prompt="Run ruff check and fix all violations.",
        )

    def test_wraps_task_in_workflow(self) -> None:
        task = self._make_task()
        result = build_daemon_workflow_prompt(task)
        assert "Fix ruff violations in session.py" in result
        assert "Run ruff check and fix all violations." in result

    def test_includes_workflow_steps(self) -> None:
        task = self._make_task()
        result = build_daemon_workflow_prompt(task)
        assert "/session-done" in result
        assert "/handoff --reason context" in result
        assert "Code Quality Reviewer" in result
        assert "Architecture Reviewer" in result

    def test_includes_quality_gates(self) -> None:
        task = self._make_task()
        result = build_daemon_workflow_prompt(task)
        assert "ruff check" in result
        assert "mypy" in result
        assert "pytest" in result

    def test_includes_daemon_header(self) -> None:
        task = self._make_task()
        result = build_daemon_workflow_prompt(task)
        assert "daemon queue system" in result
