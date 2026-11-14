"""Tests for cw.codex_review._diff — unified-diff capture and parsing (#1236)."""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw._git import git_output
from cw.codex_review import (
    _branch_diff_totals,
    _capture_delta_diff,
    _capture_diff,
    _measure_large_diff,
    _parse_unified_diff,
)
from cw.codex_review._diff import (
    _capture_branch_context,
    _capture_tree_diff,
    _limited_to_paths,
)
from cw.exceptions import CwError
from cw.plan_files import SMALL_TIER_MAX_FILES
from cw.worktree import _parse_numstat_totals
from tests import _git_sandbox
from tests._shared.git_repos import commit_small_py_files, commit_tracked_file, git_in

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

_NO_TRAILING_NEWLINE_DIFF = """diff --git a/src/cw/baz.py b/src/cw/baz.py
index 666..777 100644
--- a/src/cw/baz.py
+++ b/src/cw/baz.py
@@ -1,2 +1,2 @@
 unchanged = 0
-old_tail = 1
\\ No newline at end of file
+new_tail = 2
\\ No newline at end of file
"""


class TestParseUnifiedDiff:
    def test_per_file_line_numbers(self) -> None:
        _file_diffs, file_line_text, _window, _changed = _parse_unified_diff(
            _MULTI_FILE_DIFF
        )
        assert file_line_text["src/cw/foo.py"] == {
            2: "added_one = 1",
            3: "added_two = 2",
        }
        # bar.py: hunk starts at new line 5; context advances to 6, removed does
        # not advance, added lands at 6.
        assert file_line_text["src/cw/bar.py"] == {6: "bar_added = 3"}

    def test_context_lines_captured_in_window_text(self) -> None:
        # #1738: file_window_text is the superset of file_line_text that also
        # captures unchanged CONTEXT lines at their real new-file line
        # numbers -- the substrate _resolve_hunk_window/_nearest_hunk_line
        # need to widen the evidence-matching window without touching
        # file_line_text's added-only domain (_line_reference_valid's anchor
        # gate stays on file_line_text, untouched).
        _file_diffs, file_line_text, file_window_text, _changed = _parse_unified_diff(
            _MULTI_FILE_DIFF
        )
        assert file_window_text["src/cw/foo.py"] == {
            1: "unchanged = 0",
            2: "added_one = 1",
            3: "added_two = 2",
        }
        assert file_window_text["src/cw/bar.py"] == {5: "ctx = 1", 6: "bar_added = 3"}
        # file_line_text stays added-only, byte-identical to today.
        assert file_line_text["src/cw/foo.py"] == {
            2: "added_one = 1",
            3: "added_two = 2",
        }
        assert file_line_text["src/cw/bar.py"] == {6: "bar_added = 3"}

    def test_file_diffs_capture_hunk_text(self) -> None:
        file_diffs, _, _window, _changed = _parse_unified_diff(_MULTI_FILE_DIFF)
        assert "+added_one = 1" in file_diffs["src/cw/foo.py"]
        assert "+bar_added = 3" in file_diffs["src/cw/bar.py"]

    def test_changed_files_in_diff_order(self) -> None:
        # SHOULD_FIX 11 (#1236): changed_files is derived from the same parse
        # pass as file_diffs/file_line_text — no second subprocess needed.
        _file_diffs, _file_line_text, _window, changed = _parse_unified_diff(
            _MULTI_FILE_DIFF
        )
        assert changed == ["src/cw/foo.py", "src/cw/bar.py"]

    def test_no_newline_at_eof_marker_excluded_from_window_text(self) -> None:
        # #1738 fix-loop finding: the `\ No newline at end of file` marker is
        # diff metadata, not file content -- it must not be captured into
        # file_window_text as if it were a real context/added line.
        _file_diffs, file_line_text, file_window_text, _changed = _parse_unified_diff(
            _NO_TRAILING_NEWLINE_DIFF
        )
        assert file_window_text["src/cw/baz.py"] == {
            1: "unchanged = 0",
            2: "new_tail = 2",
        }
        assert file_line_text["src/cw/baz.py"] == {2: "new_tail = 2"}
        assert "No newline at end of file" not in str(file_window_text)

    def test_deleted_file_contributes_no_lines_but_is_changed(self) -> None:
        file_diffs, file_line_text, file_window_text, changed = _parse_unified_diff(
            _DELETED_FILE_DIFF
        )
        assert "gone.py" not in file_line_text
        assert file_diffs == {}
        assert file_window_text == {}
        # A pure deletion has no hunk text/added lines, but it IS a changed
        # file and must still appear in the changed-file list.
        assert changed == ["gone.py"]

    def test_empty_diff(self) -> None:
        file_diffs, file_line_text, file_window_text, changed = _parse_unified_diff("")
        assert file_diffs == {}
        assert file_line_text == {}
        assert file_window_text == {}
        assert changed == []


class TestCaptureDiff:
    def test_captures_added_file(self, make_git_repo: Callable[[str], Path]) -> None:
        repo = make_git_repo("wt-capture")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "new.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
        git_in(repo, "add", "new.py")
        git_in(repo, "commit", "-m", "add new.py")

        diff, reviewed_sha, changed_files = _capture_diff(repo, "main")

        assert "new.py" in diff.files
        assert diff.file_line_text["new.py"] == {1: "alpha = 1", 2: "beta = 2"}
        # A brand-new file is all added lines (no context) -- file_window_text
        # matches file_line_text exactly here.
        assert diff.file_window_text["new.py"] == diff.file_line_text["new.py"]
        assert diff.files["new.py"] == [1, 2]
        assert changed_files == ["new.py"]
        head = _git_sandbox.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
        assert reviewed_sha == head

    def test_an_inherited_git_dir_cannot_redirect_the_diff(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2264: the diff must describe the worktree the caller named.

        Before the shared seam, ``reviewed_sha`` was already env-clean (via
        ``capture_head_sha``) but the diff was not — so a hook's ``GIT_DIR``
        paired the target's sha with the decoy's diff.
        """
        repo = make_git_repo("wt-capture-target")
        git_in(repo, "checkout", "-b", "feature")
        commit_tracked_file(repo, "new.py", "alpha = 1\n")
        decoy = make_git_repo("wt-capture-decoy")
        git_in(decoy, "checkout", "-b", "feature")
        commit_tracked_file(decoy, "decoy.py", "decoy = True\n")
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

        diff, reviewed_sha, changed_files = _capture_diff(repo, "main")

        assert changed_files == ["new.py"]
        assert "decoy.py" not in diff.text
        assert reviewed_sha == git_in(repo, "rev-parse", "HEAD")


_DIFF_LOGGER = "cw.codex_review._diff"


def _numstat_totals(repo: Path, base: str) -> tuple[int, int]:
    """The ``(files, lines)`` numstat of ``<base>...HEAD``, independently parsed."""
    return _parse_numstat_totals(
        git_output(["diff", "--numstat", f"{base}...HEAD"], cwd=repo)
    )


def _diff_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _DIFF_LOGGER and r.levelno == logging.WARNING
    ]


class TestBranchDiffTotals:
    """``_branch_diff_totals`` — the whole-branch numstat (#2086)."""

    def test_totals_match_the_branch_numstat(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-totals")
        commit_small_py_files(repo, 3)
        commit_tracked_file(repo, "two.py", "a = 1\nb = 2\n")

        totals = _branch_diff_totals(repo, "main")

        assert totals == (4, 5)
        assert totals == _numstat_totals(repo, "main")

    def test_a_binary_file_counts_as_a_changed_file_with_no_lines(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-totals-binary")
        commit_small_py_files(repo, 1)
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00")
        git_in(repo, "add", "blob.bin")
        git_in(repo, "commit", "-m", "add blob")

        assert _branch_diff_totals(repo, "main") == (2, 1)

    def test_a_pinned_full_sha_base_passes_through(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-totals-pinned")
        base = git_in(repo, "rev-parse", "main")
        commit_small_py_files(repo, 2)

        assert _branch_diff_totals(repo, base) == _branch_diff_totals(repo, "main")
        assert _branch_diff_totals(repo, base) == (2, 2)

    def test_an_unresolvable_base_raises(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """The totals reader never swallows: ``pre_loop_diff_lines`` relies on it."""
        repo = make_git_repo("wt-totals-raise")
        commit_small_py_files(repo, 1)

        with pytest.raises(subprocess.CalledProcessError):
            _branch_diff_totals(repo, "no-such-branch")


class TestMeasureLargeDiff:
    """``_measure_large_diff`` — the fail-open advisory flag (#2086)."""

    def test_eleven_files_is_large(self, make_git_repo: Callable[[str], Path]) -> None:
        repo = make_git_repo("wt-measure-large")
        commit_small_py_files(repo, SMALL_TIER_MAX_FILES + 1)

        assert _measure_large_diff(repo, "main") is True

    def test_two_files_is_not_large(self, make_git_repo: Callable[[str], Path]) -> None:
        repo = make_git_repo("wt-measure-small")
        commit_small_py_files(repo, 2)

        assert _measure_large_diff(repo, "main") is False

    def test_a_failing_git_diff_fails_open_with_one_warning(
        self,
        make_git_repo: Callable[[str], Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = make_git_repo("wt-measure-git-fail")
        commit_small_py_files(repo, SMALL_TIER_MAX_FILES + 1)
        caplog.set_level(logging.WARNING)

        assert _measure_large_diff(repo, "no-such-branch") is False

        warnings = _diff_warnings(caplog)
        assert len(warnings) == 1
        assert "CalledProcessError" in warnings[0].getMessage()

    def test_an_unresolvable_base_fails_open_with_one_warning(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = make_git_repo("wt-measure-base-fail")
        commit_small_py_files(repo, SMALL_TIER_MAX_FILES + 1)

        def _unresolvable(_worktree: Path, _default: str) -> str:
            msg = "merge-base failed"
            raise CwError(msg)

        monkeypatch.setattr(
            "cw.codex_review._diff.resolve_loop_diff_base", _unresolvable
        )
        caplog.set_level(logging.WARNING)

        assert _measure_large_diff(repo, "main") is False

        warnings = _diff_warnings(caplog)
        assert len(warnings) == 1
        assert "CwError" in warnings[0].getMessage()

    def test_an_os_error_fails_open(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = make_git_repo("wt-measure-os-fail")

        def _no_git(*_args: object, **_kwargs: object) -> str:
            msg = "git vanished"
            raise OSError(msg)

        monkeypatch.setattr("cw.codex_review._diff.git_output", _no_git)
        caplog.set_level(logging.WARNING)

        assert _measure_large_diff(repo, "main") is False
        assert len(_diff_warnings(caplog)) == 1


class TestCaptureDeltaDiff:
    """``_capture_delta_diff`` — the two-SHA delta capture (#1837)."""

    def test_captures_only_the_second_commit(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-delta")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "first.py").write_text("alpha = 1\n", encoding="utf-8")
        git_in(repo, "add", "first.py")
        git_in(repo, "commit", "-m", "first")
        from_sha = git_in(repo, "rev-parse", "HEAD")
        (repo / "second.py").write_text("beta = 2\ngamma = 3\n", encoding="utf-8")
        git_in(repo, "add", "second.py")
        git_in(repo, "commit", "-m", "second")
        to_sha = git_in(repo, "rev-parse", "HEAD")

        diff, changed_files = _capture_delta_diff(repo, from_sha, to_sha)

        assert changed_files == ["second.py"]
        assert set(diff.files) == {"second.py"}
        assert diff.file_line_text["second.py"] == {1: "beta = 2", 2: "gamma = 3"}
        assert diff.file_window_text["second.py"] == diff.file_line_text["second.py"]
        assert diff.file_diffs["second.py"].startswith("+++ b/second.py")
        # The first commit's file is entirely absent -- the whole point of a
        # delta capture over `_capture_diff`'s full `main...HEAD` range.
        assert "first.py" not in diff.text

    def test_no_op_delta_is_empty(self, make_git_repo: Callable[[str], Path]) -> None:
        repo = make_git_repo("wt-delta-noop")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "only.py").write_text("alpha = 1\n", encoding="utf-8")
        git_in(repo, "add", "only.py")
        git_in(repo, "commit", "-m", "only")
        sha = git_in(repo, "rev-parse", "HEAD")

        diff, changed_files = _capture_delta_diff(repo, sha, sha)

        assert diff.files == {}
        assert diff.file_diffs == {}
        assert diff.file_line_text == {}
        assert diff.file_window_text == {}
        assert diff.text == ""
        assert changed_files == []

    def test_pure_deletion_in_changed_files_only(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-delta-delete")
        git_in(repo, "checkout", "-b", "feature")
        (repo / "doomed.py").write_text("alpha = 1\n", encoding="utf-8")
        git_in(repo, "add", "doomed.py")
        git_in(repo, "commit", "-m", "add doomed")
        from_sha = git_in(repo, "rev-parse", "HEAD")
        git_in(repo, "rm", "doomed.py")
        git_in(repo, "commit", "-m", "remove doomed")
        to_sha = git_in(repo, "rev-parse", "HEAD")

        diff, changed_files = _capture_delta_diff(repo, from_sha, to_sha)

        # Same contract `_capture_diff` already has: a pure deletion appears in
        # `changed_files` but contributes nothing to the per-file maps.
        assert changed_files == ["doomed.py"]
        assert diff.files == {}
        assert diff.file_diffs == {}


_MAGIC_NAME = ":x.py"


def _tree(repo: Path, rev: str = "HEAD") -> str:
    return git_in(repo, "rev-parse", f"{rev}^{{tree}}")


class TestCaptureTreeDiff:
    """``_capture_tree_diff``: a diff of two tree oids, never renames (#2980)."""

    def test_diffs_two_trees(self, make_git_repo: Callable[[str], Path]) -> None:
        repo = make_git_repo("wt-tree-diff")
        commit_tracked_file(repo, "a.py", "alpha = 1\n")
        before = _tree(repo)
        commit_tracked_file(repo, "a.py", "alpha = 2\n")

        diff, changed_files = _capture_tree_diff(repo, before, _tree(repo))

        assert changed_files == ["a.py"]
        assert diff.file_line_text["a.py"] == {1: "alpha = 2"}
        assert "-alpha = 1" in diff.text

    def test_a_moved_file_is_a_delete_plus_an_add(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-tree-move")
        body = "".join(f"line {index}\n" for index in range(30))
        commit_tracked_file(repo, "old.py", body)
        before = _tree(repo)
        git_in(repo, "mv", "old.py", "new.py")
        git_in(repo, "commit", "-m", "move")

        _, changed_files = _capture_tree_diff(repo, before, _tree(repo))

        assert sorted(changed_files) == ["new.py", "old.py"]

    def test_identical_trees_give_an_empty_diff(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-tree-same")
        tree = _tree(repo)

        diff, changed_files = _capture_tree_diff(repo, tree, tree)

        assert (diff.text, changed_files) == ("", [])


class TestLiteralPathspecs:
    """Context paths are literal pathspecs, never magic (#2980).

    A file named ``:x.py`` is pathspec magic to a plain ``git diff -- :x.py``:
    it matches nothing, exits 0 and prints an empty diff, so the reviewer
    would see no branch context for a file the resolution touched.
    """

    def test_limited_argv_is_literal(self) -> None:
        argv = _limited_to_paths(["diff", "a..b"], [_MAGIC_NAME])

        assert argv == ["--literal-pathspecs", "diff", "a..b", "--", _MAGIC_NAME]

    def test_branch_context_includes_a_magic_named_path(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-literal-context")
        git_in(repo, "checkout", "-b", "feature")
        base = git_in(repo, "rev-parse", "HEAD")
        (repo / _MAGIC_NAME).write_text("beta = 2\n", encoding="utf-8")
        (repo / "other.py").write_text("gamma = 3\n", encoding="utf-8")
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "magic")
        clean = git_in(repo, "rev-parse", "HEAD")

        context = _capture_branch_context(repo, base, clean, [_MAGIC_NAME])

        assert set(context) == {_MAGIC_NAME}
        assert "+beta = 2" in context[_MAGIC_NAME]


class TestEmptyPathsFailClosed:
    """Empty ``paths`` raises instead of diffing the whole range (#2980).

    A trailing ``--`` with no pathspec makes git diff everything: an unbounded
    diff inlined into the reviewer prompt as "context".
    """

    def test_limited_to_paths_rejects_empty_paths(self) -> None:
        with pytest.raises(ValueError, match="at least one path"):
            _limited_to_paths(["diff"], [])

    def test_branch_context_rejects_empty_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        git = MagicMock(return_value="")
        monkeypatch.setattr("cw.codex_review._diff.git_output", git)

        with pytest.raises(ValueError, match="at least one path"):
            _capture_branch_context(tmp_path, "a" * 40, "b" * 40, [])

        git.assert_not_called()
