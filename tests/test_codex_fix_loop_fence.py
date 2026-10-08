"""Tests for cw.codex_fix_loop.fence — fix-cycle scope fence + revert guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_fix_loop.baseline import (
    CycleBaseline,
    DirtyStart,
    capture_cycle_baseline,
    cycle_touched_paths,
)
from cw.codex_fix_loop.fence import (
    LEFT_STAGED_HINT,
    StagedSetMismatchError,
    check_fix_fence,
    cycle_start_breach,
    fix_scope_allowlist,
    scope_violation_breach,
    staged_set_breach,
)
from cw.codex_review import (
    CODEX_FIX_DIRTY_START,
    CODEX_FIX_REVERTED_BRANCH,
    CODEX_FIX_SCOPE_DRIFT,
    CODEX_FIX_SCOPE_VIOLATION,
    _SensitiveHit,
)
from tests._codex_review_helpers import (
    _measured_from_head,
    _seed_conflicting_cherry_pick,
    _stage_merge_from_other_branch,
    _write,
)
from tests.conftest import git_in
from tests.test_branch_ahead import _seed_conflicting_merge

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
        touched=set(_measured_from_head(repo)),
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
            touched=set(_measured_from_head(repo)),
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
            touched=set(_measured_from_head(repo)),
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


class TestCheckFixFence:
    def test_cycle_edit_off_fence_parks_naming_only_that_path(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        """#2633: the fence blames exactly the measured cycle paths."""
        repo = _branch_repo(make_git_repo, "f-measured", base=_BASE3, feature=_FEATURE3)
        start = capture_cycle_baseline(repo)
        assert isinstance(start, CycleBaseline)
        _write(repo / "server/route.py", "route = 1\n")
        touched = cycle_touched_paths(repo, start)

        breach = check_fix_fence(
            repo,
            default_branch="main",
            allowed_files=frozenset(_BASE3),
            finding_files=frozenset(),
            cycle=2,
            touched=touched,
        )

        assert breach is not None
        assert breach.reason == CODEX_FIX_SCOPE_DRIFT
        assert breach.paths == ("server/route.py",)


def _start(repo: Path) -> DirtyStart:
    """The real ``capture_cycle_baseline`` result for a dirty *repo*."""
    start = capture_cycle_baseline(repo)
    assert isinstance(start, DirtyStart)
    return start


def _feature_repo(make_git_repo: Callable[..., Path], name: str) -> Path:
    return _branch_repo(make_git_repo, name, base=_BASE3, feature=_FEATURE3)


class TestCycleStartBreach:
    """#2633: a dirty worktree refuses the cycle; the hint never hard-resets."""

    @staticmethod
    def _assert_refusal(breach: FenceBreach) -> None:
        assert breach.reason == CODEX_FIX_DIRTY_START
        assert "git reset --hard" not in breach.recovery_hint

    def test_merge_head_details_and_hint(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _feature_repo(make_git_repo, "f-start-merge")
        _stage_merge_from_other_branch(repo, {"pyproject.toml": "[project]\n"})

        breach = cycle_start_breach(_start(repo), 3)

        self._assert_refusal(breach)
        assert "MERGE_HEAD" in breach.details
        assert "pyproject.toml" in breach.details
        assert "git merge --abort" in breach.recovery_hint
        assert breach.paths == ("pyproject.toml",)

    def test_conflicting_merge_lists_unmerged_paths(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _seed_conflicting_merge(make_git_repo, "f-start-conflict")

        breach = cycle_start_breach(_start(repo), 1)

        self._assert_refusal(breach)
        assert "Unresolved (unmerged) path(s):\n- work.txt" in breach.details
        assert "work.txt" in breach.paths

    def test_cherry_pick_conflict_names_cherry_pick_head(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _feature_repo(make_git_repo, "f-start-pick")
        _seed_conflicting_cherry_pick(repo)

        breach = cycle_start_breach(_start(repo), 1)

        self._assert_refusal(breach)
        assert "CHERRY_PICK_HEAD" in breach.details
        assert "git cherry-pick --abort" in breach.recovery_hint
        assert "git merge --abort" not in breach.recovery_hint

    def test_revert_names_revert_head(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _feature_repo(make_git_repo, "f-start-revert")
        git_in(repo, "revert", "--no-commit", "HEAD")

        breach = cycle_start_breach(_start(repo), 1)

        self._assert_refusal(breach)
        assert "REVERT_HEAD" in breach.details
        assert "git revert --abort" in breach.recovery_hint

    def test_squash_has_no_marker_and_no_abort_command(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _feature_repo(make_git_repo, "f-start-squash")
        _stage_merge_from_other_branch(
            repo, {"pyproject.toml": "[project]\n"}, squash=True
        )

        breach = cycle_start_breach(_start(repo), 1)

        self._assert_refusal(breach)
        assert (
            "Uncommitted changes (git status --porcelain):\n- A  pyproject.toml"
            in breach.details
        )
        assert "git status" in breach.recovery_hint
        assert "git diff --cached" in breach.recovery_hint
        assert "--abort" not in breach.recovery_hint

    def test_listing_is_capped_at_20(self, make_git_repo: Callable[..., Path]) -> None:
        repo = _feature_repo(make_git_repo, "f-start-cap")
        for i in range(30):
            _write(repo / f"stray{i:02d}.py", "x = 1\n")

        breach = cycle_start_breach(_start(repo), 1)

        assert "- ?? stray19.py" in breach.details
        assert "stray20.py" not in breach.details
        assert "- ... and 10 more" in breach.details
        assert len(breach.paths) == 30

    def test_hint_states_cw_staged_nothing(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _feature_repo(make_git_repo, "f-start-nothing")
        _write(repo / "stray.py", "x = 1\n")

        breach = cycle_start_breach(_start(repo), 4)

        assert "codex fix cycle 4 was not started" in breach.details
        assert "No fix invocation ran" in breach.details
        assert "cw staged, committed and pushed nothing" in breach.details


class TestScopeViolationBreach:
    def test_details_keep_the_and_gate_text_and_hint_states_staged_state(
        self,
    ) -> None:
        hits = [_SensitiveHit("pyproject.toml", "build", "packaging metadata")]

        breach = scope_violation_breach(hits, 2)

        assert breach.reason == CODEX_FIX_SCOPE_VIOLATION
        assert "out of" in breach.details.lower()
        assert "sensitive" in breach.details.lower()
        assert "- pyproject.toml (build): packaging metadata" in breach.details
        assert breach.paths == ("pyproject.toml",)
        # Advisory soundness RISK 1: the measurement stages the cycle's
        # changes, so the hint says where they are.
        assert LEFT_STAGED_HINT in breach.recovery_hint


class TestLeftStagedHint:
    def test_hint_is_public_and_states_the_clean_start_premise(self) -> None:
        assert "git reset --hard HEAD" in LEFT_STAGED_HINT
        assert "This cycle began from a clean tree" in LEFT_STAGED_HINT
        assert "discards only that" in LEFT_STAGED_HINT


class TestStagedSetBreach:
    def test_unmeasured_staged_path_hint(self) -> None:
        breach = staged_set_breach(
            frozenset({"a.py"}), frozenset({"a.py", "x.py"}), 2, start_head="abc123"
        )

        assert breach.reason == CODEX_FIX_SCOPE_DRIFT
        assert "Staged but not measured:\n- x.py" in breach.details
        assert breach.paths == ("x.py",)
        assert "git restore --staged" in breach.recovery_hint
        assert "git reset --hard" not in breach.recovery_hint
        assert LEFT_STAGED_HINT not in breach.recovery_hint

    def test_fix_invocation_committed_itself_hint(self) -> None:
        breach = staged_set_breach(
            frozenset({"a.py"}), frozenset(), 2, start_head="abc123"
        )

        assert "Measured but not staged:\n- a.py" in breach.details
        assert "git log abc123..HEAD" in breach.recovery_hint
        assert "would not undo" in breach.recovery_hint
        assert "git reset --hard" not in breach.recovery_hint

    def test_partial_staged_subset_hint(self) -> None:
        breach = staged_set_breach(
            frozenset({"a.py", "b.py"}), frozenset({"a.py"}), 2, start_head="abc123"
        )

        assert "Measured but not staged:\n- b.py" in breach.details
        assert "git log abc123..HEAD" in breach.recovery_hint
        assert "partial" in breach.recovery_hint
        assert "git reset --hard" not in breach.recovery_hint

    def test_mixed_extra_and_missing_reads_as_unmeasured_case(self) -> None:
        breach = staged_set_breach(
            frozenset({"a.py", "b.py"}),
            frozenset({"a.py", "x.py"}),
            2,
            start_head="abc123",
        )

        assert "Staged but not measured:\n- x.py" in breach.details
        assert "Measured but not staged:\n- b.py" in breach.details
        assert breach.paths == ("b.py", "x.py")
        assert "git restore --staged" in breach.recovery_hint

    def test_mismatch_exception_carries_both_sets(self) -> None:
        exc = StagedSetMismatchError(frozenset({"a.py"}), frozenset({"b.py"}))

        assert exc.measured == frozenset({"a.py"})
        assert exc.staged == frozenset({"b.py"})
        assert "a.py" in str(exc)
