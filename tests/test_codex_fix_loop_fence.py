"""Tests for cw.codex_fix_loop.fence — fix-cycle scope fence + revert guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_fix_loop.fence import check_fix_fence, fix_scope_allowlist
from cw.codex_review import CODEX_FIX_REVERTED_BRANCH, CODEX_FIX_SCOPE_DRIFT
from tests._codex_review_helpers import _write
from tests.conftest import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.codex_fix_loop.fence import FenceBreach

_PLAN = """\
# Plan

## Files Modified

- `src/a.py`
- `tests/test_a.py`

## Steps

1. Do it.
"""


def _branch_repo(
    make_git_repo: Callable[..., Path],
    name: str,
    *,
    base: dict[str, str],
    feature: dict[str, str],
) -> Path:
    """Repo with *base* committed on ``main`` and *feature* on ``feature``."""
    repo = make_git_repo(name)
    for rel, text in base.items():
        _write(repo / rel, text)
    if base:
        git_in(repo, "add", "-A")
        git_in(repo, "commit", "-m", "base")
    git_in(repo, "checkout", "-b", "feature")
    for rel, text in feature.items():
        _write(repo / rel, text)
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", "feature")
    return repo


_BASE3 = {"a.py": "a = 1\n", "b.py": "b = 1\n", "c.py": "c = 1\n"}
_FEATURE3 = {"a.py": "a = 2\n", "b.py": "b = 2\n", "c.py": "c = 2\n"}


def _check(
    repo: Path,
    *,
    allowed: frozenset[str] | None = None,
    finding_files: frozenset[str] = frozenset(),
) -> FenceBreach | None:
    return check_fix_fence(
        repo,
        default_branch="main",
        allowed_files=allowed,
        finding_files=finding_files,
        cycle=2,
    )


class TestFixScopeAllowlist:
    def test_no_plan_means_no_fence(self) -> None:
        assert fix_scope_allowlist(None, ["x.py"]) is None

    def test_plan_without_manifest_means_no_fence(self) -> None:
        assert fix_scope_allowlist("# Plan\n\nJust prose.\n", ["x.py"]) is None

    def test_fence_is_manifest_plus_cycle0_files(self) -> None:
        allowed = fix_scope_allowlist(_PLAN, ["docs/extra.md"])
        assert allowed == frozenset({"src/a.py", "tests/test_a.py", "docs/extra.md"})


class TestRevertGuard:
    def test_no_changes_passes(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _branch_repo(make_git_repo, "f-noop", base=_BASE3, feature=_FEATURE3)
        assert _check(repo) is None

    def test_emptying_the_branch_parks(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#2492: a fix that net-cancels every branch commit is refused."""
        repo = _branch_repo(make_git_repo, "f-empty", base=_BASE3, feature=_FEATURE3)
        for rel, text in _BASE3.items():
            _write(repo / rel, text)

        breach = check_fix_fence(
            repo,
            default_branch="main",
            allowed_files=None,
            # Even when every restored file is named, emptying the branch parks.
            finding_files=frozenset(_BASE3),
            cycle=2,
        )

        assert breach is not None
        assert breach.reason == CODEX_FIX_REVERTED_BRANCH
        assert breach.paths == ("a.py", "b.py", "c.py")
        assert "empty the branch diff" in breach.details
        assert "git diff --cached" in breach.recovery_hint
        # Nothing was committed: HEAD still carries the feature commit.
        assert git_in(repo, "log", "-1", "--format=%s").strip() == "feature"

    def test_deleting_branch_added_files_counts_as_revert(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_repo(
            make_git_repo, "f-added", base={}, feature={"n1.py": "1\n", "n2.py": "2\n"}
        )
        git_in(repo, "rm", "-q", "n1.py", "n2.py")

        breach = _check(repo)

        assert breach is not None
        assert breach.reason == CODEX_FIX_REVERTED_BRANCH

    def test_majority_unnamed_revert_parks(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _branch_repo(make_git_repo, "f-major", base=_BASE3, feature=_FEATURE3)
        _write(repo / "a.py", _BASE3["a.py"])
        _write(repo / "b.py", _BASE3["b.py"])

        breach = check_fix_fence(
            repo,
            default_branch="main",
            allowed_files=None,
            finding_files=frozenset({"c.py"}),
            cycle=2,
        )

        assert breach is not None
        assert breach.reason == CODEX_FIX_REVERTED_BRANCH
        assert "restore 2 of the branch's 3 changed file(s)" in breach.details

    def test_reverting_files_findings_name_passes(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """Removing code a finding called out is the fix, not a revert."""
        repo = _branch_repo(make_git_repo, "f-named", base=_BASE3, feature=_FEATURE3)
        _write(repo / "a.py", _BASE3["a.py"])
        _write(repo / "b.py", _BASE3["b.py"])

        assert _check(repo, finding_files=frozenset({"a.py", "b.py"})) is None

    def test_minority_revert_passes(self, make_git_repo: Callable[..., Path]) -> None:
        """A helper moved out of one branch file restores it; that is legitimate."""
        base = {**_BASE3, "d.py": "d = 1\n"}
        feature = {**_FEATURE3, "d.py": "d = 2\n"}
        repo = _branch_repo(make_git_repo, "f-minor", base=base, feature=feature)
        _write(repo / "a.py", base["a.py"])
        _write(repo / "helpers.py", "def helper(): ...\n")

        assert _check(repo) is None


class TestScopeFence:
    def test_out_of_fence_addition_parks(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#2485: a fix that grows the branch beyond the fence is refused."""
        repo = _branch_repo(make_git_repo, "f-drift", base=_BASE3, feature=_FEATURE3)
        _write(repo / "a.py", "a = 3\n")
        _write(repo / "server/route.py", "route = 1\n")

        breach = _check(repo, allowed=frozenset(_BASE3))

        assert breach is not None
        assert breach.reason == CODEX_FIX_SCOPE_DRIFT
        assert breach.paths == ("server/route.py",)

    def test_in_fence_changes_pass(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _branch_repo(make_git_repo, "f-infence", base=_BASE3, feature=_FEATURE3)
        _write(repo / "a.py", "a = 3\n")
        _write(repo / "tests/test_a.py", "def test(): ...\n")

        allowed = frozenset({*_BASE3, "tests/test_a.py"})
        assert _check(repo, allowed=allowed) is None

    def test_no_fence_allows_any_path(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _branch_repo(make_git_repo, "f-nofence", base=_BASE3, feature=_FEATURE3)
        _write(repo / "server/route.py", "route = 1\n")

        assert _check(repo, allowed=None) is None

    def test_removing_out_of_fence_code_passes(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """Deleting an off-fence file the branch added restores it to base."""
        feature = {**_FEATURE3, "rogue.py": "x = 1\n"}
        repo = _branch_repo(make_git_repo, "f-unrogue", base=_BASE3, feature=feature)
        git_in(repo, "rm", "-q", "rogue.py")

        assert _check(repo, allowed=frozenset(_BASE3)) is None

    def test_only_paths_this_cycle_touched_are_blamed(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        feature = {**_FEATURE3, "rogue.py": "x = 1\n"}
        repo = _branch_repo(make_git_repo, "f-blame", base=_BASE3, feature=feature)
        _write(repo / "a.py", "a = 3\n")

        assert _check(repo, allowed=frozenset(_BASE3)) is None
