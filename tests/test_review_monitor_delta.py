"""Characterization tests for review-monitor code-change detection (#2499).

Covers ``check_code_changed``, ``parse_diff_changed_lines``,
``_apply_code_changes`` and ``_detect_touched_threads`` (moving to
``review_monitor_lib/delta.py``). The diff under test is produced by real git
in a tmp repo; the script's own ``_run_git`` is routed through
``FakeCommands`` so it replays that diff without inheriting the test env.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests import _review_monitor_helpers as helpers
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_BASE_LINES = [f"line {n}" for n in range(1, 21)]


def _status(file: str, line: int | None) -> Any:
    return helpers.get("ThreadStatus")(file=file, line=line)


@pytest.fixture
def real_diff(make_git_repo: Callable[..., Path]) -> str:
    """A unified diff that edits line 12 of a.py and adds two-line b.py."""
    repo = make_git_repo("widgets")
    (repo / "a.py").write_text("\n".join(_BASE_LINES) + "\n")
    git_in(repo, "add", "a.py")
    git_in(repo, "commit", "-q", "-m", "base")
    base = git_in(repo, "rev-parse", "HEAD")
    edited = [*_BASE_LINES]
    edited[11] = "line 12 edited"
    del edited[2]
    (repo / "a.py").write_text("\n".join(edited) + "\n")
    (repo / "b.py").write_text("one\ntwo\n")
    git_in(repo, "add", "a.py", "b.py")
    git_in(repo, "commit", "-q", "-m", "edit")
    return git_in(repo, "diff", f"{base}..HEAD")


def test_parse_real_diff_records_only_added_new_file_lines(real_diff: str) -> None:
    assert helpers.get("parse_diff_changed_lines")(real_diff) == {
        "a.py": {11},
        "b.py": {1, 2},
    }


def test_parse_diff_ignores_lines_before_any_file_header() -> None:
    diff = (
        "+stray\n@@ -1 +1 @@\n+orphan\n+++ b/c.py\n@@ -3,2 +7,3 @@\n ctx\n-gone\n+new\n"
    )

    assert helpers.get("parse_diff_changed_lines")(diff) == {"c.py": {8}}


@pytest.mark.parametrize(
    ("file", "line", "changed", "expected"),
    [
        ("a.py", 10, {15}, True),
        ("a.py", 10, {5}, True),
        ("a.py", 10, {16}, False),
        ("a.py", 10, {4}, False),
        ("a.py", None, {10}, False),
        ("other.py", 10, {10}, False),
    ],
)
def test_check_code_changed_window(
    file: str, line: int | None, changed: set[int], expected: bool
) -> None:
    assert helpers.get("check_code_changed")(file, line, {"a.py": changed}) is expected


@pytest.mark.parametrize(("role", "has_delta"), [("reviewer", True), ("author", False)])
def test_apply_code_changes_reports_touched_threads(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_diff: str,
    role: str,
    has_delta: bool,
) -> None:
    fake = helpers.FakeCommands().add(("git", "diff"), real_diff)
    fake.install(monkeypatch)
    pr = helpers.make_pr(
        role=role,
        repo_path="/canon/widgets",
        thread_status={
            "near": _status("a.py", 14),
            "far": _status("a.py", 1),
            "outdated": _status("a.py", None),
            "new": _status("b.py", 2),
        },
    )

    result = helpers.get("_apply_code_changes")(pr, "base", "head")

    assert result == (has_delta, real_diff if has_delta else None, ["near", "new"])
    assert fake.calls == [(("git", "diff", "base..head"), "/canon/widgets")]


def test_apply_code_changes_reviewer_with_empty_diff_has_no_delta(
    review_monitor_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers.FakeCommands().add(("git", "diff"), "").install(monkeypatch)
    pr = helpers.make_pr(role="reviewer")

    assert helpers.get("_apply_code_changes")(pr, "a", "b") == (False, None, [])


@pytest.mark.parametrize(
    ("base", "repo_path_kind"),
    [("head", "dir"), ("base", "missing"), ("base", "empty")],
    ids=["same-sha", "stale-repo-path", "no-repo-path"],
)
def test_detect_touched_threads_skips_without_diff_or_valid_repo(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    base: str,
    repo_path_kind: str,
) -> None:
    fake = helpers.FakeCommands().install(monkeypatch)
    repo_path = {
        "dir": str(tmp_path),
        "missing": str(tmp_path / "cleaned-up-worktree"),
        "empty": "",
    }[repo_path_kind]
    pr = helpers.make_pr(role="reviewer", repo_path=repo_path)

    result = helpers.get("_detect_touched_threads")(
        pr, delta_base_sha=base, new_sha="head"
    )

    assert result == (False, None, [])
    assert fake.calls == []


def test_detect_touched_threads_diffs_from_delta_base(
    review_monitor_state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    real_diff: str,
) -> None:
    fake = helpers.FakeCommands().add(("git", "diff"), real_diff)
    fake.install(monkeypatch)
    pr = helpers.make_pr(
        role="reviewer",
        repo_path=str(tmp_path),
        thread_status={"t": _status("b.py", 1)},
    )

    result = helpers.get("_detect_touched_threads")(
        pr, delta_base_sha="base", new_sha="head"
    )

    assert result == (True, real_diff, ["t"])
    assert fake.argvs() == [("git", "diff", "base..head")]
