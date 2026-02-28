"""Tests for cw.handoff - handoff document parsing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from cw.handoff import (
    build_task_prompt,
    extract_resumption_prompt,
    find_handoffs_newer_than,
    find_latest_handoff,
)
from cw.models import SessionPurpose, TaskSpec

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
        f.write_text("## Resumption Prompt\n\n```\nLine 1\nLine 2\nLine 3\n```\n")
        result = extract_resumption_prompt(f)
        assert result is not None
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_prompt_is_stripped(self, tmp_path: Path) -> None:
        f = tmp_path / "padded.md"
        f.write_text("## Resumption Prompt\n\n```\n\n  Resume the work  \n\n```\n")
        result = extract_resumption_prompt(f)
        assert result == "Resume the work"


class TestBuildTaskPrompt:
    def _make_task(
        self,
        description: str = "Refactor auth module",
        purpose: SessionPurpose = SessionPurpose.IMPL,
        prompt: str = "Move auth logic into its own service layer.",
        context_files: list[str] | None = None,
        success_criteria: str | None = None,
    ) -> TaskSpec:
        return TaskSpec(
            description=description,
            purpose=purpose,
            prompt=prompt,
            context_files=context_files or [],
            success_criteria=success_criteria,
        )

    def test_includes_description(self) -> None:
        task = self._make_task(description="Write integration tests")
        result = build_task_prompt(task)
        assert "Write integration tests" in result

    def test_includes_instructions_header(self) -> None:
        task = self._make_task(prompt="Use pytest parametrize for edge cases.")
        result = build_task_prompt(task)
        assert "Instructions:" in result

    def test_includes_prompt_text(self) -> None:
        task = self._make_task(prompt="Use pytest parametrize for edge cases.")
        result = build_task_prompt(task)
        assert "Use pytest parametrize for edge cases." in result

    def test_includes_context_files(self) -> None:
        files = ["src/cw/handoff.py", "tests/test_handoff.py"]
        task = self._make_task(context_files=files)
        result = build_task_prompt(task)
        assert "Context files:" in result
        assert "src/cw/handoff.py" in result
        assert "tests/test_handoff.py" in result

    def test_includes_success_criteria(self) -> None:
        task = self._make_task(success_criteria="100% pass rate, zero ruff violations.")
        result = build_task_prompt(task)
        assert "Success criteria:" in result
        assert "100% pass rate, zero ruff violations." in result

    def test_no_context_files_section_when_empty(self) -> None:
        task = self._make_task(context_files=[])
        result = build_task_prompt(task)
        assert "Context files:" not in result

    def test_no_success_criteria_section_when_none(self) -> None:
        task = self._make_task(success_criteria=None)
        result = build_task_prompt(task)
        assert "Success criteria:" not in result

    def test_minimal_task_has_required_sections(self) -> None:
        task = self._make_task(
            description="Minimal task",
            prompt="Just do the thing.",
            context_files=[],
            success_criteria=None,
        )
        result = build_task_prompt(task)
        assert "Minimal task" in result
        assert "Instructions:" in result
        assert "Just do the thing." in result

    def test_description_appears_before_instructions(self) -> None:
        task = self._make_task(
            description="Important task",
            prompt="The actual instructions.",
        )
        result = build_task_prompt(task)
        desc_pos = result.index("Important task")
        instr_pos = result.index("Instructions:")
        assert desc_pos < instr_pos

    def test_context_files_appear_before_instructions(self) -> None:
        task = self._make_task(
            context_files=["some/file.py"],
            prompt="Do something.",
        )
        result = build_task_prompt(task)
        files_pos = result.index("Context files:")
        instr_pos = result.index("Instructions:")
        assert files_pos < instr_pos

    def test_success_criteria_appears_before_instructions(self) -> None:
        task = self._make_task(
            success_criteria="All green.",
            prompt="Do something.",
        )
        result = build_task_prompt(task)
        criteria_pos = result.index("Success criteria:")
        instr_pos = result.index("Instructions:")
        assert criteria_pos < instr_pos

    def test_multiple_context_files_each_listed(self) -> None:
        files = ["a.py", "b.py", "c.py"]
        task = self._make_task(context_files=files)
        result = build_task_prompt(task)
        for f in files:
            assert f in result
