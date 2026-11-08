"""``assess_resolution_round``: what a conflict-resolution round reviews (#2980).

Real git for the shapes git can build on demand; ``ScriptedMergeGit`` (default
deny) for unreadable git, timeouts and the shapes that only need a scripted
answer.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw.review_convergence import (
    MergeTolerance,
    ResolutionAssessment,
    assess_resolution_round,
    tolerance_for_client,
)
from cw.review_convergence._resolution import has_left_in_marker
from tests._review_convergence_helpers import (
    BRANCH_ONLY_TEXT,
    MAIN_ONLY_TEXT,
    MERGED_TREE_SHA,
    RESOLUTION_FILE,
    RESOLUTION_LEDGER_SHAPES,
    RESOLUTION_MARKER,
    RESOLVED_TREE_SHA,
    ScriptedMergeGit,
    branch_with_hand_edited_clean_merge,
    branch_with_overlap_merge,
    branch_with_resolution_and_unrelated_hunks,
    branch_with_two_resolutions,
    fail_when,
    fresh_ledger,
    make_review_client,
    merge_tree_key,
    non_default_parent_resolution_script,
    octopus_resolution_script,
    resolution_diff_keys,
    resolution_ledger,
    resolution_merge_script,
    side_branch_commit,
)
from tests.conftest import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.review_convergence import ConfirmBackend, ConvergenceLedger
    from tests._review_convergence_helpers import GitScript

    RepoFactory = Callable[..., tuple[Path, Path]]

TOLERANCE = MergeTolerance("main")
_CLEAN = "c" * 40
_HEAD = "d" * 40
_DEFAULT = "e" * 40

_SHAPES = [
    pytest.param(loop, confirm, id=f"{loop}+{confirm}")
    for loop, confirm in RESOLUTION_LEDGER_SHAPES
]


def _assess(
    repo: Path,
    ledger: ConvergenceLedger,
    head: str,
    tolerance: MergeTolerance | None = TOLERANCE,
) -> ResolutionAssessment:
    return assess_resolution_round(ledger, repo, head_sha=head, tolerance=tolerance)


def _resolved_repo(
    factory: RepoFactory, tmp_path: Path, **kwargs: object
) -> tuple[Path, str, str, str]:
    repo, _ = factory("wt-resolution")
    found = branch_with_resolution_and_unrelated_hunks(
        repo, tmp_path / "scratch", **kwargs
    )
    return repo, found.clean, found.head, found.merge


class TestTheResolutionSpec:
    def test_the_resolution_diff_contains_only_the_resolution(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, clean, head, merge = _resolved_repo(make_git_repo_with_origin, tmp_path)

        plan = _assess(repo, resolution_ledger(clean=clean), head).plan

        assert plan is not None
        spec = plan.spec
        assert spec.paths == (RESOLUTION_FILE,)
        assert (spec.merge, spec.reviewed_sha) == (merge, head)
        assert plan.merges == (merge,)
        diff = git_in(repo, "diff", spec.conflicted_tree, spec.resolved_tree)
        assert f"+{RESOLUTION_MARKER.strip()}" in diff
        assert "-<<<<<<<" in diff
        assert "->>>>>>>" in diff
        assert BRANCH_ONLY_TEXT.strip() not in diff
        assert MAIN_ONLY_TEXT.strip() not in diff

    def test_branch_context_is_the_branchs_own_hunks_for_touched_files_only(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, clean, head, merge = _resolved_repo(make_git_repo_with_origin, tmp_path)

        plan = _assess(repo, resolution_ledger(clean=clean), head).plan

        assert plan is not None
        spec = plan.spec
        assert spec.branch_tip == git_in(repo, "rev-parse", f"{merge}^1")
        assert spec.branch_base == git_in(
            repo, "merge-base", f"{merge}^1", f"{merge}^2"
        )
        context = git_in(
            repo, "diff", spec.branch_base, spec.branch_tip, "--", RESOLUTION_FILE
        )
        assert BRANCH_ONLY_TEXT.strip() in context
        assert MAIN_ONLY_TEXT.strip() not in context

    def test_a_clean_merge_then_a_resolution_plans_the_resolution_only(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, clean, head, merge = _resolved_repo(
            make_git_repo_with_origin, tmp_path, clean_merge_first=True
        )

        plan = _assess(repo, resolution_ledger(clean=clean), head).plan

        assert plan is not None
        assert plan.spec.merge == merge
        assert len(plan.merges) == 2
        assert plan.merges[-1] == merge
        assert plan.spec.paths == (RESOLUTION_FILE,)

    def test_a_hand_edited_clean_merge_is_reviewed_as_its_edit(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, _ = make_git_repo_with_origin("wt-hand-edit")
        found = branch_with_hand_edited_clean_merge(repo, tmp_path / "scratch")

        plan = _assess(repo, resolution_ledger(clean=found.clean), found.head).plan

        assert plan is not None
        diff = git_in(repo, "diff", plan.spec.conflicted_tree, plan.spec.resolved_tree)
        assert "+hand edit" in diff
        assert "main_clean.py" not in diff
        assert plan.spec.paths == (RESOLUTION_FILE,)

    def test_two_resolved_files_are_sorted(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, clean, head, _ = _resolved_repo(make_git_repo_with_origin, tmp_path)
        git_in(repo, "reset", "--hard", "HEAD^1")
        origin = git_in(repo, "rev-parse", "origin/main")
        stopped = subprocess.run(
            ["git", "-C", str(repo), "merge", "--no-commit", "--no-ff", origin],
            capture_output=True,
            check=False,
        )
        assert stopped.returncode != 0
        (repo / RESOLUTION_FILE).write_text("resolved\n", encoding="utf-8")
        (repo / "a_extra.py").write_text("extra = 1\n", encoding="utf-8")
        git_in(repo, "add", RESOLUTION_FILE, "a_extra.py")
        git_in(repo, "commit", "--no-edit")
        head = git_in(repo, "rev-parse", "HEAD")

        plan = _assess(repo, resolution_ledger(clean=clean), head).plan

        assert plan is not None
        assert plan.spec.paths == ("a_extra.py", RESOLUTION_FILE)


class TestRealGitRefusals:
    def test_a_clean_merge_is_no_resolution(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, _ = make_git_repo_with_origin("wt-clean-merge")
        found = branch_with_overlap_merge(repo, tmp_path / "scratch")

        assessment = _assess(repo, resolution_ledger(clean=found.clean), found.head)

        assert assessment == ResolutionAssessment(None, "no_resolution")

    def test_two_resolutions_are_refused(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, _ = make_git_repo_with_origin("wt-two")
        found = branch_with_two_resolutions(repo, tmp_path / "scratch")

        assessment = _assess(repo, resolution_ledger(clean=found.clean), found.head)

        assert assessment.refusal == "multiple_resolutions"

    def test_committed_markers_are_refused_not_empty(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        """Committed markers carry other labels, so the diff is NOT empty (#2980)."""
        repo, clean, head, _ = _resolved_repo(
            make_git_repo_with_origin, tmp_path, resolved=None
        )

        assessment = _assess(repo, resolution_ledger(clean=clean), head)

        assert assessment == ResolutionAssessment(None, "markers_left")

    @pytest.mark.parametrize("after", [True, False], ids=["after", "before"])
    def test_an_own_commit_is_refused(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
        after: bool,
    ) -> None:
        repo, clean, head, _ = _resolved_repo(make_git_repo_with_origin, tmp_path)
        ledger = resolution_ledger(clean=clean)
        if after:
            commit_tracked_file(repo, "late.py")
            own = git_in(repo, "rev-parse", "HEAD")
        else:
            ledger = resolution_ledger(clean=git_in(repo, "rev-parse", f"{clean}^"))
            own = clean
        head = git_in(repo, "rev-parse", "HEAD")

        assessment = _assess(repo, ledger, head)

        assert assessment.refusal == f"non_merge_commit {own[:7]}"

    def test_no_merge_and_not_an_ancestor(
        self, make_git_repo_with_origin: RepoFactory
    ) -> None:
        repo, _ = make_git_repo_with_origin("wt-nomerge")
        clean = git_in(repo, "rev-parse", "HEAD")
        git_in(repo, "fetch", "origin")
        commit_tracked_file(repo, "after.py")
        head = git_in(repo, "rev-parse", "HEAD")
        side = side_branch_commit(repo)
        ledger = resolution_ledger(clean=clean)

        assert _assess(repo, ledger, head).refusal == "no_merge"
        stray = resolution_ledger(clean=side)
        assert _assess(repo, stray, head).refusal == "clean_not_ancestor"

    def test_no_default_ref(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
    ) -> None:
        repo, clean, head, _ = _resolved_repo(make_git_repo_with_origin, tmp_path)

        assessment = _assess(
            repo, resolution_ledger(clean=clean), head, MergeTolerance("develop")
        )

        assert assessment.refusal == "no_default_ref"


class TestLedgerRefusals:
    """Ledger-only refusals make no git call at all (default deny)."""

    @pytest.mark.parametrize(("loop", "confirm"), _SHAPES)
    @pytest.mark.parametrize(
        ("build", "head", "tolerance", "reason"),
        [
            pytest.param(
                lambda loop, confirm: resolution_ledger(
                    loop_backend=loop, confirm=confirm, clean=_CLEAN
                ),
                _HEAD,
                None,
                "tolerance_off",
                id="tolerance-off",
            ),
            pytest.param(
                lambda loop, confirm: fresh_ledger(loop_backend=loop, confirm=confirm),
                _HEAD,
                TOLERANCE,
                "ledger_not_confirmable",
                id="in-progress",
            ),
            pytest.param(
                lambda loop, _confirm: resolution_ledger(
                    loop_backend=loop, confirm="none", clean=_CLEAN
                ),
                _HEAD,
                TOLERANCE,
                "ledger_not_confirmable",
                id="confirm-none",
            ),
            pytest.param(
                lambda loop, confirm: resolution_ledger(
                    loop_backend=loop, confirm=confirm, clean=_CLEAN
                ),
                _CLEAN,
                TOLERANCE,
                "ledger_not_confirmable",
                id="head-is-clean",
            ),
        ],
    )
    def test_refused_without_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        loop: str,
        confirm: ConfirmBackend,
        build: Callable[[str, ConfirmBackend], ConvergenceLedger],
        head: str,
        tolerance: MergeTolerance | None,
        reason: str,
    ) -> None:
        fake = ScriptedMergeGit({}).install(monkeypatch)
        ledger = build(loop, confirm)
        before = ledger.model_dump(mode="json")

        assessment = _assess(tmp_path, ledger, head, tolerance)

        assert assessment == ResolutionAssessment(None, reason)
        assert fake.calls == []
        assert ledger.model_dump(mode="json") == before

    def test_a_not_independent_confirm_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = ScriptedMergeGit({}).install(monkeypatch)
        ledger = resolution_ledger(clean=_CLEAN, confirm_not_independent=True)

        assessment = _assess(tmp_path, ledger, _HEAD)

        assert assessment.refusal == "ledger_not_confirmable"
        assert fake.calls == []

    @pytest.mark.parametrize(
        ("convergence", "tolerant"),
        [("blocking", True), ("clean", False)],
        ids=["blocking", "strict"],
    )
    def test_a_strict_client_is_tolerance_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        convergence: str,
        tolerant: bool,
    ) -> None:
        fake = ScriptedMergeGit({}).install(monkeypatch)
        client = make_review_client(
            tmp_path, convergence=convergence, default_merge_tolerance=tolerant
        )

        assessment = _assess(
            tmp_path,
            resolution_ledger(clean=_CLEAN),
            _HEAD,
            tolerance_for_client(client),
        )

        assert assessment.refusal == "tolerance_off"
        assert fake.calls == []


class TestScriptedRefusals:
    @pytest.mark.parametrize(("loop", "confirm"), _SHAPES)
    def test_the_scripted_resolution_plans(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        loop: str,
        confirm: ConfirmBackend,
    ) -> None:
        ScriptedMergeGit(resolution_merge_script(_CLEAN, _HEAD, _DEFAULT)).install(
            monkeypatch
        )
        ledger = resolution_ledger(loop_backend=loop, confirm=confirm, clean=_CLEAN)

        plan = _assess(tmp_path, ledger, _HEAD).plan

        assert plan is not None
        assert (plan.spec.conflicted_tree, plan.spec.resolved_tree) == (
            MERGED_TREE_SHA,
            RESOLVED_TREE_SHA,
        )
        assert plan.spec.branch_tip == _CLEAN

    @pytest.mark.parametrize(("loop", "confirm"), _SHAPES)
    @pytest.mark.parametrize(
        ("edit", "reason"),
        [
            pytest.param(
                lambda s: s.update({merge_tree_key(_HEAD): 129}),
                f"merge_tree_unavailable {_HEAD[:7]}",
                id="merge-tree-rc129",
            ),
            pytest.param(
                lambda s: s.update(
                    {merge_tree_key(_HEAD): subprocess.TimeoutExpired("git", 10)}
                ),
                f"merge_tree_unavailable {_HEAD[:7]}",
                id="merge-tree-timeout",
            ),
            pytest.param(
                lambda s: s.update({merge_tree_key(_HEAD): OSError("gone")}),
                f"merge_tree_unavailable {_HEAD[:7]}",
                id="merge-tree-oserror",
            ),
            pytest.param(
                lambda s: s.update({resolution_diff_keys()[0]: ""}),
                "empty_resolution",
                id="empty",
            ),
            pytest.param(
                lambda s: s.update({resolution_diff_keys()[0]: 128}),
                "git_unreadable",
                id="names-unreadable",
            ),
            pytest.param(
                lambda s: s.update({resolution_diff_keys()[1]: 128}),
                "git_unreadable",
                id="diff-unreadable",
            ),
            pytest.param(
                lambda s: s.update({("merge-base", f"{_HEAD}^1", f"{_HEAD}^2"): 1}),
                "git_unreadable",
                id="merge-base-unreadable",
            ),
            pytest.param(
                lambda s: s.update({("rev-parse", f"{_HEAD}^1"): 128}),
                "git_unreadable",
                id="branch-tip-unreadable",
            ),
            pytest.param(
                lambda s: s.update({("rev-parse", f"{_HEAD}^{{tree}}"): 128}),
                "git_unreadable",
                id="own-tree-unreadable",
            ),
        ],
    )
    def test_a_git_failure_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        loop: str,
        confirm: ConfirmBackend,
        edit: Callable[[GitScript], None],
        reason: str,
    ) -> None:
        script = resolution_merge_script(_CLEAN, _HEAD, _DEFAULT)
        edit(script)
        ScriptedMergeGit(script).install(monkeypatch)
        ledger = resolution_ledger(loop_backend=loop, confirm=confirm, clean=_CLEAN)

        assessment = _assess(tmp_path, ledger, _HEAD)

        assert assessment == ResolutionAssessment(None, reason)

    @pytest.mark.parametrize(
        ("script", "reason"),
        [
            pytest.param(
                octopus_resolution_script(_CLEAN, _HEAD, _DEFAULT),
                f"not_two_parent {_HEAD[:7]}",
                id="octopus",
            ),
            pytest.param(
                non_default_parent_resolution_script(_CLEAN, _HEAD, _DEFAULT),
                f"second_parent_not_on_default {_HEAD[:7]}",
                id="off-default",
            ),
        ],
    )
    def test_a_per_merge_rejection_stops_before_any_diff(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        script: GitScript,
        reason: str,
    ) -> None:
        fake = ScriptedMergeGit(script).install(monkeypatch)

        assessment = _assess(tmp_path, resolution_ledger(clean=_CLEAN), _HEAD)

        assert assessment.refusal == reason
        assert not any(call[0] == "diff" for call in fake.calls)

    def test_a_spent_budget_is_merge_tree_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = ScriptedMergeGit(
            resolution_merge_script(_CLEAN, _HEAD, _DEFAULT)
        ).install(monkeypatch)
        ticks = iter([0.0, 1000.0])

        assessment = assess_resolution_round(
            resolution_ledger(clean=_CLEAN),
            tmp_path,
            head_sha=_HEAD,
            tolerance=TOLERANCE,
            clock=lambda: next(ticks),
        )

        assert assessment.refusal == f"merge_tree_unavailable {_HEAD[:7]}"
        assert not any(call[0] == "merge-tree" for call in fake.calls)

    def test_an_unreadable_span_is_refused(
        self,
        make_git_repo_with_origin: RepoFactory,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo, clean, head, _ = _resolved_repo(make_git_repo_with_origin, tmp_path)
        failed = fail_when(monkeypatch, ["rev-list"], "rc128")

        assessment = _assess(repo, resolution_ledger(clean=clean), head)

        assert assessment.refusal == "git_unreadable"
        assert failed


class TestMarkers:
    @pytest.mark.parametrize(
        ("line", "found"),
        [
            ("+<<<<<<< HEAD", True),
            ("+=======", True),
            ("+>>>>>>> main", True),
            ("+<<<<<<<", True),
            ("-<<<<<<< HEAD", False),
            ("+<<<<<<<< eight", False),
            ("+ =======", False),
            ("+++ b/<<<<<<< x", False),
            ("+plain", False),
        ],
    )
    def test_only_an_added_marker_counts(self, line: str, found: bool) -> None:
        assert has_left_in_marker(f"--- a/x\n+++ b/x\n@@ -1 +1 @@\n{line}\n") is found
