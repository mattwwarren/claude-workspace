"""Tests for cw.codex_fix_loop.growth — the in-file growth budget (#2633)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop.baseline import cycle_touched_paths
from cw.codex_fix_loop.fence import LEFT_STAGED_HINT
from cw.codex_fix_loop.growth import (
    DEFAULT_GROWTH_BUDGET_LINES,
    AdditionKind,
    check_growth_budget,
    detect_additions,
    effective_budget,
    is_source_path,
    justified_kinds,
    net_source_lines,
)
from cw.codex_fix_loop.posted_text import (
    POSTED_TEXT_MAX_CHARS,
    TRUNCATION_MARKER,
    describe_added_line,
)
from cw.codex_review import CODEX_FIX_GROWTH_BUDGET, _parse_unified_diff
from tests._codex_review_helpers import _head_baseline, _write
from tests.conftest import _make_finding, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_fix_loop.fence import FenceBreach


def _diff(path: str, added: list[str], removed: tuple[str, ...] = ()) -> str:
    """A minimal ``-U0`` unified diff of one file (our own contract)."""
    body = [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            f"@@ -1,{len(removed)} +1,{len(added)} @@",
            *body,
        ]
    )


def _kinds(*diffs: str) -> list[AdditionKind]:
    file_diffs, added, _window, _changed = _parse_unified_diff("\n".join(diffs))
    return [a.kind for a in detect_additions(added, file_diffs)]


class TestDetectAdditions:
    @pytest.mark.parametrize(
        "line",
        [
            "_L = threading.Lock()",
            "from filelock import FileLock",
            "    fcntl.flock(fd, fcntl.LOCK_EX)",
        ],
    )
    def test_lock_detected(self, line: str) -> None:
        assert _kinds(_diff("src/a.py", [line])) == [AdditionKind.LOCK]

    @pytest.mark.parametrize("line", ["_OUTBOX_PATH = Path('x')", "STATE_FILE = 'x'"])
    def test_path_constant_detected(self, line: str) -> None:
        assert AdditionKind.PATH_CONSTANT in _kinds(_diff("src/a.py", [line]))

    def test_state_filename_literal_detected(self) -> None:
        line = '    path = Path(root) / "outbox.json"'
        assert _kinds(_diff("src/a.py", [line])) == [AdditionKind.STATE_FILE]

    def test_json_literal_without_filesystem_call_not_detected(self) -> None:
        assert _kinds(_diff("src/a.py", ['name = "outbox.json"'])) == []

    def test_top_level_def_and_class_counted(self) -> None:
        kinds = _kinds(_diff("src/a.py", ["def f():", "async def g():", "class C:"]))
        assert kinds == [AdditionKind.TOP_LEVEL_DEF] * 3

    def test_indented_method_not_counted(self) -> None:
        assert _kinds(_diff("src/a.py", ["    def method(self):"])) == []

    def test_tests_and_docs_are_excluded(self) -> None:
        lock = "_L = threading.Lock()"
        assert (
            _kinds(_diff("tests/test_x.py", [lock]), _diff("README.md", [lock])) == []
        )

    def test_non_python_files_get_no_regex_detectors(self) -> None:
        file_diffs, added, *_ = _parse_unified_diff(_diff("a.js", ["flock(x)"] * 3))
        assert detect_additions(added, file_diffs) == []
        assert net_source_lines(file_diffs, added) == 3

    def test_edited_line_that_already_held_the_construct_is_not_an_addition(
        self,
    ) -> None:
        diff = _diff(
            "src/a.py",
            ["_L = threading.Lock()  # guards x"],
            removed=("_L = threading.Lock()",),
        )
        assert _kinds(diff) == []

    def test_moved_construct_with_net_increase_is_reported(self) -> None:
        diff = _diff(
            "src/a.py",
            ["_L = threading.Lock()", "_M = threading.Lock()"],
            removed=("_L = threading.Lock()",),
        )
        assert _kinds(diff) == [AdditionKind.LOCK]


class TestIsSourcePath:
    @pytest.mark.parametrize(
        "path", ["src/a.py", "pkg/mod.ts", "docsify/a.py", "src/documents.py"]
    )
    def test_source_paths_are_counted(self, path: str) -> None:
        assert is_source_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_a.py",
            "README.md",
            "notes.mdx",
            "guide.rst",
            "n.txt",
            "man.adoc",
            "docs/events.md",
            "docs/notes.yaml",
            "pkg/docs/notes.yaml",
        ],
    )
    def test_tests_and_prose_are_not(self, path: str) -> None:
        assert not is_source_path(path)


class TestNetLines:
    def test_added_minus_removed(self) -> None:
        diff = _diff("src/a.py", [f"a{i} = 1" for i in range(60)], tuple("x" * 55))
        file_diffs, added, *_ = _parse_unified_diff(diff)
        assert net_source_lines(file_diffs, added) == 5

    def test_budget_scales_with_open_finding_count(self) -> None:
        assert effective_budget(40, 3) == 120
        assert effective_budget(40, 0) == 40


class TestJustification:
    def test_finding_requesting_lock_waives_lock_kind(self) -> None:
        finding = _make_finding(suggested_fix="Add a lock around the update.")
        assert AdditionKind.LOCK in justified_kinds([finding])

    def test_finding_saying_not_crash_safe_does_not_waive_state_file_kind(
        self,
    ) -> None:
        """The #2591 shape: "not crash-safe" never licenses an outbox file."""
        finding = _make_finding(suggested_fix="Event delivery is not crash-safe.")
        assert justified_kinds([finding]) == frozenset()


def _repo(make_git_repo: Callable[..., Path]) -> Path:
    repo = make_git_repo("growth")
    _write(repo / "src" / "a.py", "a = 1\n")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "base")
    return repo


def _check(repo: Path, *, open_findings: int = 1) -> FenceBreach | None:
    baseline = _head_baseline(repo)
    cycle_touched_paths(repo, baseline)  # stages the cycle, as the loop does
    return check_growth_budget(
        repo,
        baseline,
        open_findings=[_make_finding()] * open_findings,
        budget_lines=DEFAULT_GROWTH_BUDGET_LINES,
        cycle=2,
    )


class TestCheckGrowthBudget:
    def test_breach_names_each_addition_with_path_and_line(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(
            repo / "src" / "a.py", "a = 1\nimport threading\n_L = threading.Lock()\n"
        )

        breach = _check(repo)

        assert breach is not None
        assert breach.reason == CODEX_FIX_GROWTH_BUDGET
        assert "- src/a.py:3 new lock: _L = threading.Lock()" in breach.details
        assert breach.paths == ("src/a.py",)

    def test_breach_hint_mentions_staged_budget_key_and_guard_switch(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "".join(f"x{i} = {i}\n" for i in range(50)))

        breach = _check(repo)

        assert breach is not None
        assert "net non-test source lines +49 exceeds the budget of 40" in (
            breach.details
        )
        assert LEFT_STAGED_HINT in breach.recovery_hint
        assert "codex_fix_loop_growth_budget_lines" in breach.recovery_hint
        assert "codex_fix_loop_growth_guard_enabled" in breach.recovery_hint

    def test_earlier_committed_additions_not_counted(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        _write(repo / "src" / "a.py", "a = 1\n_L = threading.Lock()\n")
        git_in(repo, "commit", "-am", "lock committed before the cycle")
        _write(repo / "src" / "a.py", "a = 2\n_L = threading.Lock()\n")

        assert _check(repo) is None

    def test_no_diff_returns_none(self, make_git_repo: Callable[..., Path]) -> None:
        assert _check(_repo(make_git_repo)) is None

    def test_binary_file_ignored(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _repo(make_git_repo)
        (repo / "blob.bin").write_bytes(bytes(range(256)) * 40)

        assert _check(repo) is None


class TestPostedGrowthText:
    def test_secret_named_assignment_line_is_withheld(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        added = 'secret_lock = threading.Lock(); password = "hunter2"'
        _write(repo / "src" / "a.py", f"a = 1\n{added}\n")

        breach = _check(repo)

        assert breach is not None
        assert f"src/a.py:2 new lock: {describe_added_line(added)}" in (breach.details)
        assert "hunter2" not in breach.details

    def test_long_added_line_is_capped_and_details_total_is_capped(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        long_lock = "_L = threading.Lock()  # " + "z" * 300
        locks = "".join(f"_L{i} = threading.Lock()  # {'w' * 100}\n" for i in range(40))
        _write(repo / "src" / "a.py", f"a = 1\n{long_lock}\n{locks}")

        breach = _check(repo, open_findings=5)

        assert breach is not None
        assert "z" * 121 not in breach.details
        assert len(breach.details) <= POSTED_TEXT_MAX_CHARS + len(TRUNCATION_MARKER)

    def test_known_secret_shape_in_added_line_is_redacted(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _repo(make_git_repo)
        token = "ghp_" + "t" * 36
        _write(repo / "src" / "a.py", f"a = 1\n_L = threading.Lock()  # {token}\n")

        breach = _check(repo)

        assert breach is not None
        assert token not in breach.details
        assert "<redacted>" in breach.details
