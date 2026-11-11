"""Stage-aware crash-gate HEAD comparison (#3054), against real git repos.

REVIEW (non-committing) must still sit exactly on its baseline; IMPL (committing)
may have advanced onto a descendant. An unknown baseline is ``None`` for both,
which the caller parks as a git error.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cw.models import ClientConfig, Stage, TicketTask
from cw.models.enums import QueueItemStatus
from cw.reconcile.codex_boot import _head_matches_pre_review_ref
from tests._shared.git_repos import commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable

_UNKNOWN_BASE = "deadbeef" * 5


def _task(stage: Stage, base_ref: str | None) -> TicketTask:
    return TicketTask(
        ticket_id="T-gate",
        client="test",
        stage=stage,
        status=QueueItemStatus.RUNNING,
        stage_base_ref=base_ref,
    )


def _two_commits(make_git_repo: Callable[[str], Path]) -> tuple[Path, str, str]:
    """Return (repo, C1 sha, C0 sha): C1 on top of C0, HEAD at C1."""
    repo = make_git_repo("wt-head-gate")
    c0 = git_in(repo, "rev-parse", "HEAD")
    commit_tracked_file(repo, "one.py", "x = 1\n")
    c1 = git_in(repo, "rev-parse", "HEAD")
    return repo, c1, c0


class TestBaselineComparison:
    def test_review_at_baseline_is_clean(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo, c1, _ = _two_commits(make_git_repo)
        result = _head_matches_pre_review_ref(
            repo, _task(Stage.REVIEW, c1), {}, stage=Stage.REVIEW
        )
        assert result is True

    def test_review_advanced_head_is_not_clean(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo, _, c0 = _two_commits(make_git_repo)
        result = _head_matches_pre_review_ref(repo, _task(Stage.REVIEW, c0), {})
        assert result is False

    def test_impl_descendant_of_baseline_is_clean(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo, _, c0 = _two_commits(make_git_repo)
        assert _head_matches_pre_review_ref(repo, _task(Stage.IMPL, c0), {}) is True

    def test_impl_head_not_descending_from_baseline_is_not_clean(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo, c1, _ = _two_commits(make_git_repo)
        # Rewind to C0, then commit a sibling: C1 (the baseline) is not an ancestor.
        git_in(repo, "reset", "--hard", "HEAD~1")
        commit_tracked_file(repo, "two.py", "y = 2\n")
        assert _head_matches_pre_review_ref(repo, _task(Stage.IMPL, c1), {}) is False

    def test_impl_unknown_baseline_is_none(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo, _, _ = _two_commits(make_git_repo)
        assert (
            _head_matches_pre_review_ref(repo, _task(Stage.IMPL, _UNKNOWN_BASE), {})
            is None
        )

    def test_default_stage_is_task_stage(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """Without the keyword, an IMPL row is judged as committing."""
        repo, _, c0 = _two_commits(make_git_repo)
        assert _head_matches_pre_review_ref(repo, _task(Stage.IMPL, c0), {}) is True


class TestOriginFallback:
    def test_fallback_uses_origin_tracking_ref(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        from cw.models import resolve_feature_branch

        repo, _, c0 = _two_commits(make_git_repo)
        client = ClientConfig(name="test", workspace_path=repo, default_branch="main")
        task = _task(Stage.IMPL, None)
        branch = resolve_feature_branch(task.ticket_id, client, task)
        git_in(repo, "update-ref", f"refs/remotes/origin/{branch}", c0)

        assert _head_matches_pre_review_ref(repo, task, {"test": client}) is True
        assert (
            _head_matches_pre_review_ref(
                repo, _task(Stage.REVIEW, None), {"test": client}
            )
            is False
        )
