"""Tests for cw.handoff - handoff document parsing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from cw.handoff import (
    extract_resumption_prompt,
    find_handoffs_newer_than,
    find_latest_handoff,
)

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
