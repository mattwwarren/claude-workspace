"""Tests for the repair recipe's review-approval and tip fence (#2771).

Real temp git repos. The Step 4.0 approval matrix lives once in
``test_review_approval.py`` (#2773); what stays here is the repair-level wiring
(``evaluate_repair_fence`` hands the row's approval fields and ``new_sha`` to
the shared predicate) and the clean-mode Step 4.0b check, which loads a ledger
built with the real convergence engine.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cw.models import QueueItemStatus
from cw.models.client import ReviewConfig
from cw.reconcile.review_recipes.repair_fence import (
    FenceSnapshot,
    build_fence_snapshot,
    evaluate_repair_fence,
)
from cw.review_approval import APPROVAL_NOT_REQUIRED, APPROVAL_RECORDED, APPROVAL_STALE
from tests._review_approval_helpers import (
    ApprovalRepo,
    advance_main,
    head,
    make_approval_repo,
)
from tests._review_convergence_helpers import (
    TICKET_ID,
    confirmed_ledger,
    fresh_ledger,
    write_ledger,
)
from tests.conftest import git_in
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


@pytest.fixture
def repo(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> ApprovalRepo:
    return make_approval_repo(make_git_repo_with_origin, "fence-repo")


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


@pytest.mark.parametrize(
    ("review", "expected"),
    [
        pytest.param(ReviewConfig(convergence="clean"), True, id="default"),
        pytest.param(
            ReviewConfig(convergence="clean", default_merge_tolerance=False),
            False,
            id="strict-opt-out",
        ),
    ],
)
def test_build_fence_snapshot_carries_the_strict_tolerance_opt_out(
    review: ReviewConfig, expected: bool
) -> None:
    client = _client_with_lanes().model_copy(update={"review": review})

    snapshot = build_fence_snapshot(_make_task(), client)

    assert snapshot.default_merge_tolerance is expected


def test_build_fence_snapshot_without_worktree() -> None:
    snapshot = build_fence_snapshot(
        _make_task(worktree_path=None), _client_with_lanes()
    )

    assert snapshot.ledger_path is None


# --- Step 4.0 wiring ----------------------------------------------------------


def test_small_tier_passes_outside_clean_mode(repo: ApprovalRepo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(tier="small"),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=None,
    )

    assert verdict.passed is True
    assert verdict.approval_token == APPROVAL_NOT_REQUIRED
    assert verdict.tip_reason is None


def test_large_with_approval_at_head_passes(repo: ApprovalRepo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved[:12]),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=None,
    )

    assert verdict.passed is True
    assert verdict.approval_token == APPROVAL_RECORDED


def test_history_rewrite_fails_large_tier_ancestry(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    advance_main(repo.origin, repo.worktree, tmp_path / "side", "main_only.py")
    git_in(repo.worktree, "rebase", "origin/main")

    verdict = evaluate_repair_fence(
        _snapshot(review_approved_sha=repo.approved),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=None,
    )

    assert verdict.passed is False
    assert verdict.approval_token == APPROVAL_STALE


# --- Step 4.0b (clean mode) -------------------------------------------------


def test_clean_mode_without_ledger_is_stale(repo: ApprovalRepo, tmp_path: Path) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(
            tier="small",
            clean_mode=True,
            ledger_path=tmp_path / "missing.json",
        ),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=head(repo.worktree),
    )

    assert verdict.passed is False
    assert verdict.approval_token == APPROVAL_NOT_REQUIRED
    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_with_no_worktree_is_stale(repo: ApprovalRepo) -> None:
    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=None),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=head(repo.worktree),
    )

    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_foreign_ledger_is_stale(repo: ApprovalRepo, tmp_path: Path) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=head(repo.worktree)))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path, ticket_id="T-9"),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=head(repo.worktree),
    )

    assert verdict.passed is False
    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_corrupt_ledger_is_stale(repo: ApprovalRepo, tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=head(repo.worktree),
    )

    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_in_progress_ledger_is_stale(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    path = write_ledger(tmp_path, fresh_ledger())

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=head(repo.worktree),
    )

    assert verdict.tip_reason == "no_clean_round"


def test_clean_mode_origin_tip_mismatch_is_stale(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=head(repo.worktree)))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip="f" * 40,
    )

    assert verdict.tip_reason == "head_not_origin_tip"


def test_clean_mode_unreadable_origin_tip_is_fetch_failed(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=head(repo.worktree)))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=None,
    )

    assert verdict.passed is False
    # #2910: an unreadable origin tip is a fetch failure, not a stale head.
    assert verdict.tip_reason == "tip_check_fetch_failed"
    assert verdict.detail == (
        "small tier: no review approval needed; origin's branch tip could not be read "
        "(ls-remote failed, timed out or returned no ref)"
    )


def test_clean_mode_converged_at_new_head_passes(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    path = write_ledger(tmp_path, confirmed_ledger(sha=head(repo.worktree)))

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=head(repo.worktree),
    )

    assert verdict.passed is True
    assert verdict.tip_reason is None


def test_history_rewrite_fails_clean_mode_tip(
    repo: ApprovalRepo, tmp_path: Path
) -> None:
    """The #2815 tolerance is NOT extended to a rewritten head."""
    path = write_ledger(tmp_path, confirmed_ledger(sha=head(repo.worktree)))
    advance_main(repo.origin, repo.worktree, tmp_path / "side", "main_only.py")
    git_in(repo.worktree, "rebase", "origin/main")

    verdict = evaluate_repair_fence(
        _snapshot(tier="small", clean_mode=True, ledger_path=path),
        repo=repo.worktree,
        new_sha=head(repo.worktree),
        origin_tip=head(repo.worktree),
    )

    assert verdict.passed is False
    assert verdict.tip_reason == "commits_after_clean_round"
