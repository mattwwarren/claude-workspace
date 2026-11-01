"""Tests for the repair recipe's review-approval and tip fence (#2771).

Real temp git repos: the Step 4.0 port runs ``rev-parse``, ``merge-base`` and
``rev-list`` against them, and the clean-mode Step 4.0b check loads a ledger
built with the real convergence engine.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cw.models import QueueItemStatus
from cw.reconcile.review_recipes import changelog_repair_fence as fence
from cw.reconcile.review_recipes.changelog_repair_fence import (
    APPROVAL_MISSING,
    APPROVAL_NOT_REQUIRED,
    APPROVAL_RECORDED,
    APPROVAL_STALE,
    APPROVAL_UNREADABLE,
    APPROVAL_UNRESOLVED_TIER,
    FenceSnapshot,
    _resolve_row_tier,
    build_fence_snapshot,
    evaluate_repair_fence,
)
from tests._review_convergence_helpers import (
    TICKET_ID,
    confirmed_ledger,
    fresh_ledger,
    write_ledger,
)
from tests.conftest import add_bare_origin, commit_tracked_file, git_in
from tests.test_reconcile_gate_recipes import _client_with_lanes, _make_task


def _snapshot(**fields: object) -> FenceSnapshot:
    base = FenceSnapshot(
        ticket_id=TICKET_ID,
        tier="large",
        review_approver_kind="operator",
        review_approved_sha=None,
        clean_mode=False,
        ledger_path=None,
        default_branch="main",
    )
    return base._replace(**fields)


class _Repo:
    """A clone with a bare origin: main, plus a feature branch off it."""

    def __init__(self, make_git_repo: Callable[..., Path]) -> None:
        self.path = make_git_repo("fence-repo")
        commit_tracked_file(self.path, "base.py")
        add_bare_origin(self.path)
        git_in(self.path, "checkout", "-b", "feature")
        commit_tracked_file(self.path, "feature.py")
        self.approved = git_in(self.path, "rev-parse", "HEAD")

    def head(self) -> str:
        return git_in(self.path, "rev-parse", "HEAD")

    def advance_main(self, filename: str) -> str:
        git_in(self.path, "checkout", "main")
        commit_tracked_file(self.path, filename)
        git_in(self.path, "push", "origin", "main")
        git_in(self.path, "fetch", "origin")
        sha = git_in(self.path, "rev-parse", "HEAD")
        git_in(self.path, "checkout", "feature")
        return sha


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> _Repo:
    return _Repo(make_git_repo)


# --- tier resolution --------------------------------------------------------


@pytest.mark.parametrize(
    ("scope_hint", "computed", "expected"),
    [
        ("large", "small", "large"),
        ("small", "large", "large"),
        (None, "small", "small"),
        ("small", None, "small"),
        (None, None, None),
        ("medium", None, None),
        (None, "Small", None),
    ],
)
def test_resolve_row_tier_precedence(
    scope_hint: str | None, computed: str | None, expected: str | None
) -> None:
    task = _make_task(scope_hint=scope_hint, computed_scope_tier=computed)

    assert _resolve_row_tier(task) == expected


def test_build_fence_snapshot_reads_the_row(tmp_path: Path) -> None:
    task = _make_task(
        status=QueueItemStatus.COMPLETED,
        computed_scope_tier="large",
        review_approver_kind="recipe",
        review_approved_sha="abc1234",
        worktree_path=tmp_path,
    )
    client = _client_with_lanes()

    snapshot = build_fence_snapshot(task, client)

    assert snapshot.tier == "large"
    assert snapshot.review_approver_kind == "recipe"
    assert snapshot.review_approved_sha == "abc1234"
    assert snapshot.clean_mode is False
    assert snapshot.ledger_path == tmp_path / ".cw" / "review-convergence.json"
    assert snapshot.default_branch == "main"


def test_build_fence_snapshot_without_worktree() -> None:
    snapshot = build_fence_snapshot(
        _make_task(worktree_path=None), _client_with_lanes()
    )

    assert snapshot.ledger_path is None


# --- Step 4.0 port ----------------------------------------------------------


def test_small_tier_passes_outside_clean_mode(repo: _Repo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(tier="small"), repo=repo.path, new_sha=repo.head(), origin_tip=None
    )

    assert verdict.passed is True
    assert verdict.approval_token == APPROVAL_NOT_REQUIRED
    assert verdict.tip_reason is None


def test_unresolved_tier_parks(repo: _Repo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(tier=None), repo=repo.path, new_sha=repo.head(), origin_tip=None
    )

    assert verdict.passed is False
    assert verdict.approval_token == APPROVAL_UNRESOLVED_TIER


@pytest.mark.parametrize("kind", [None, "human", ""])
def test_large_without_approval_is_missing(repo: _Repo, kind: str | None) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(review_approver_kind=kind, review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.passed is False
    assert verdict.approval_token == APPROVAL_MISSING


def test_large_with_approval_at_head_passes(repo: _Repo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved[:12]),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.passed is True
    assert verdict.approval_token == APPROVAL_RECORDED


def test_large_with_only_default_branch_merge_after_approval_passes(
    repo: _Repo,
) -> None:
    repo.advance_main("main_only.py")
    git_in(repo.path, "merge", "--no-edit", "origin/main")

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.passed is True
    assert verdict.approval_token == APPROVAL_RECORDED


@pytest.mark.parametrize("sha", ["not-hex", "abc", "Z" * 40])
def test_unusable_reviewed_sha_is_stale(repo: _Repo, sha: str) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=sha),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_STALE


def test_unresolvable_reviewed_sha_is_stale(repo: _Repo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha="0" * 40),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_STALE


def test_non_ancestor_reviewed_sha_is_stale(repo: _Repo) -> None:
    main_sha = repo.advance_main("main_only.py")

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=main_sha),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_STALE
    assert "not an ancestor" in verdict.detail


def test_new_non_merge_work_after_approval_is_stale(repo: _Repo) -> None:
    commit_tracked_file(repo.path, "more.py")

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_STALE
    assert "non-merge commit" in verdict.detail


def test_merge_bringing_in_a_non_default_parent_is_stale(repo: _Repo) -> None:
    # The side tip is a merge commit (no non-merge new work for the walk to
    # see) that is itself not on origin/main, so only the parent check fires.
    main_tip = repo.advance_main("main_only.py")
    git_in(repo.path, "checkout", "-b", "side", f"{main_tip}^")
    git_in(repo.path, "merge", "--no-ff", "--no-edit", main_tip)
    side_tip = git_in(repo.path, "rev-parse", "HEAD")
    git_in(repo.path, "checkout", "feature")
    git_in(repo.path, "merge", "--no-ff", "--no-edit", "side")

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_STALE
    assert f"brought in {side_tip}" in verdict.detail


def test_history_rewrite_fails_large_tier_ancestry(repo: _Repo) -> None:
    repo.advance_main("main_only.py")
    git_in(repo.path, "rebase", "origin/main")

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.passed is False
    assert verdict.approval_token == APPROVAL_STALE


def test_missing_default_branch_ref_is_unreadable(repo: _Repo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved, default_branch="nope"),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_UNREADABLE


def test_missing_repo_dir_is_unreadable(tmp_path: Path) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha="a" * 40),
        repo=tmp_path / "gone",
        new_sha="b" * 40,
        origin_tip=None,
    )

    assert verdict.passed is False
    assert verdict.approval_token == APPROVAL_UNREADABLE


def test_git_timeout_is_unreadable(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _timeout(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["git"], 60)

    monkeypatch.setattr(fence, "run_git", _timeout)

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_UNREADABLE


def test_rev_list_failure_is_unreadable(
    repo: _Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fence.run_git

    def _fail_rev_list(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "rev-list":
            return subprocess.CompletedProcess(argv, 128, "", "boom")
        return real(argv, **kwargs)

    monkeypatch.setattr(fence, "run_git", _fail_rev_list)

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_UNREADABLE


@pytest.mark.parametrize(
    ("fail_prefix", "outcome"),
    [
        pytest.param(("merge-base",), 128, id="merge-base-error"),
        pytest.param(("merge-base",), None, id="merge-base-timeout"),
        pytest.param(("rev-list", "--merges"), 128, id="merges-error"),
        pytest.param(("rev-list", "--parents"), 128, id="parents-error"),
    ],
)
def test_git_errors_in_the_ancestry_walk_are_unreadable(
    repo: _Repo,
    monkeypatch: pytest.MonkeyPatch,
    fail_prefix: tuple[str, ...],
    outcome: int | None,
) -> None:
    main_tip = repo.advance_main("main_only.py")
    git_in(repo.path, "merge", "--no-ff", "--no-edit", main_tip)
    real = fence.run_git

    def _inject(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if tuple(argv[: len(fail_prefix)]) == fail_prefix:
            if outcome is None:
                raise subprocess.TimeoutExpired(["git"], 60)
            return subprocess.CompletedProcess(argv, outcome, "", "boom")
        return real(argv, **kwargs)

    monkeypatch.setattr(fence, "run_git", _inject)

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.approval_token == APPROVAL_UNREADABLE


# --- Step 4.0b (clean mode) -------------------------------------------------


def test_clean_mode_without_ledger_is_stale(repo: _Repo, tmp_path: Path) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(
            tier="small",
            clean_mode=True,
            ledger_path=tmp_path / "missing.json",
        ),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=repo.head(),
    )

    assert verdict.passed is False
    assert verdict.approval_token == APPROVAL_NOT_REQUIRED
    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_with_no_worktree_is_stale(repo: _Repo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=None),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=repo.head(),
    )

    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_foreign_ledger_is_stale(repo: _Repo, tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=repo.head()))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path, ticket_id="T-9"),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=repo.head(),
    )

    assert verdict.passed is False
    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_corrupt_ledger_is_stale(repo: _Repo, tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=repo.head(),
    )

    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_in_progress_ledger_is_stale(repo: _Repo, tmp_path: Path) -> None:
    path = write_ledger(tmp_path, fresh_ledger())

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=repo.head(),
    )

    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_origin_tip_mismatch_is_stale(repo: _Repo, tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=repo.head()))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip="f" * 40,
    )

    assert verdict.tip_reason == "head_not_origin_tip"


def test_clean_mode_unreadable_origin_tip_is_stale(repo: _Repo, tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=repo.head()))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=None,
    )

    assert verdict.passed is False
    assert verdict.tip_reason == "head_not_origin_tip"


def test_clean_mode_converged_at_new_head_passes(repo: _Repo, tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=repo.head()))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=repo.head(),
    )

    assert verdict.passed is True
    assert verdict.tip_reason is None


def test_history_rewrite_fails_clean_mode_tip(repo: _Repo, tmp_path: Path) -> None:
    """The #2815 tolerance is NOT extended to a rewritten head."""
    path = write_ledger(tmp_path, confirmed_ledger(sha=repo.head()))
    repo.advance_main("main_only.py")
    git_in(repo.path, "rebase", "origin/main")

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.path,
        new_sha=repo.head(),
        origin_tip=repo.head(),
    )

    assert verdict.passed is False
    assert verdict.tip_reason == "commits_after_clean_round"
