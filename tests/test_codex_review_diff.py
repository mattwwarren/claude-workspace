"""Tests for cw.codex_review._diff — unified-diff capture and parsing (#1236)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from cw.codex_review import _capture_diff, _parse_unified_diff
from tests._codex_review_helpers import _git

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# _parse_unified_diff / _capture_diff
# ---------------------------------------------------------------------------

_MULTI_FILE_DIFF = """diff --git a/src/cw/foo.py b/src/cw/foo.py
index 111..222 100644
--- a/src/cw/foo.py
+++ b/src/cw/foo.py
@@ -1,2 +1,3 @@
 unchanged = 0
+added_one = 1
+added_two = 2
diff --git a/src/cw/bar.py b/src/cw/bar.py
index 333..444 100644
--- a/src/cw/bar.py
+++ b/src/cw/bar.py
@@ -5,3 +5,4 @@
 ctx = 1
-removed = 2
+bar_added = 3
"""

_DELETED_FILE_DIFF = """diff --git a/gone.py b/gone.py
deleted file mode 100644
index 555..000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line_one = 1
-line_two = 2
"""


class TestParseUnifiedDiff:
    def test_per_file_line_numbers(self) -> None:
        _file_diffs, file_line_text, _changed = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert file_line_text["src/cw/foo.py"] == {
            2: "added_one = 1",
            3: "added_two = 2",
        }
        # bar.py: hunk starts at new line 5; context advances to 6, removed does
        # not advance, added lands at 6.
        assert file_line_text["src/cw/bar.py"] == {6: "bar_added = 3"}

    def test_file_diffs_capture_hunk_text(self) -> None:
        file_diffs, _, _changed = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert "+added_one = 1" in file_diffs["src/cw/foo.py"]
        assert "+bar_added = 3" in file_diffs["src/cw/bar.py"]

    def test_changed_files_in_diff_order(self) -> None:
        # SHOULD_FIX 11 (#1236): changed_files is derived from the same parse
        # pass as file_diffs/file_line_text — no second subprocess needed.
        _file_diffs, _file_line_text, changed = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert changed == ["src/cw/foo.py", "src/cw/bar.py"]

    def test_deleted_file_contributes_no_lines_but_is_changed(self) -> None:
        file_diffs, file_line_text, changed = _parse_unified_diff(_DELETED_FILE_DIFF)
        assert "gone.py" not in file_line_text
        assert file_diffs == {}
        # A pure deletion has no hunk text/added lines, but it IS a changed
        # file and must still appear in the changed-file list.
        assert changed == ["gone.py"]

    def test_empty_diff(self) -> None:
        file_diffs, file_line_text, changed = _parse_unified_diff("")
        assert file_diffs == {}
        assert file_line_text == {}
        assert changed == []


class TestCaptureDiff:
    def test_captures_added_file(self, make_git_repo: Callable[[str], Path]) -> None:
        repo = make_git_repo("wt-capture")
        _git(repo, "checkout", "-b", "feature")
        (repo / "new.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
        _git(repo, "add", "new.py")
        _git(repo, "commit", "-m", "add new.py")

        diff, reviewed_sha, changed_files = _capture_diff(repo, "main")

        assert "new.py" in diff.files
        assert diff.file_line_text["new.py"] == {1: "alpha = 1", 2: "beta = 2"}
        assert diff.files["new.py"] == [1, 2]
        assert changed_files == ["new.py"]
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        assert reviewed_sha == head
