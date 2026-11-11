"""Tests for cw.codex_impl._feedback: the live-PR bind and the merge (#3061).

``gh`` is a scripted ``FakeGhCli`` answering with unpolished, observed-shape
payloads (sparse, null-riddled); git is real and sandboxed.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.codex_impl._feedback import (
    STALE_PREFIX,
    merge_default_on_conflict,
    probe_default_branch,
    verify_review_feedback,
)
from cw.codex_impl._result import IMPL_FAILED, ImplBlock
from cw.merge_probe import MergeProbe, MergeTreeResult
from cw.models import TicketTask
from cw.models.review_feedback import ReviewFeedback
from tests._codex_review_helpers import fresh_impl_repo
from tests._shared.git_repos import (
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)
from tests._tool_fakes import FakeGhCli

if TYPE_CHECKING:
    from collections.abc import Callable

_BRANCH = "dev/T-1"
_REPO = "o/r"
_PR = 7
_URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    worktree, _origin = fresh_impl_repo(make_git_repo, "fb-wt", _BRANCH)
    return worktree


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGhCli:
    return FakeGhCli().install(monkeypatch)


def _task(repo: Path, **fb: Any) -> TicketTask:
    sha = git_in(repo, "rev-parse", "HEAD")
    feedback = ReviewFeedback(
        repo=fb.get("repo", _REPO),
        pr_number=fb.get("pr_number", _PR),
        reviewed_sha=fb.get("reviewed_sha", sha),
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        report="red",
    )
    return TicketTask(
        ticket_id="T-1", client="test", branch=_BRANCH, review_feedback=feedback
    )


def _script(repo: Path, gh: FakeGhCli, **pr: Any) -> None:
    sha = git_in(repo, "rev-parse", "HEAD")
    view = {
        "headRefName": _BRANCH,
        "headRefOid": sha,
        "headRepository": {"nameWithOwner": _REPO},
        "number": _PR,
        "url": _URL,
        **pr,
    }
    gh.reply("repo", "view", stdout=json.dumps({"nameWithOwner": _REPO}))
    gh.reply("pr", "view", stdout=json.dumps(view))


def _stale(block: ImplBlock | None) -> None:
    assert block is not None
    assert block.reason == IMPL_FAILED
    assert block.details.startswith(STALE_PREFIX)


class TestVerifyReviewFeedback:
    def test_rows_without_feedback_skip_the_bind(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        task = TicketTask(ticket_id="T-1", client="test")
        assert verify_review_feedback(task, repo) is None
        assert gh.records == []

    def test_passes_when_everything_matches(self, repo: Path, gh: FakeGhCli) -> None:
        _script(repo, gh)
        assert verify_review_feedback(_task(repo), repo) is None

    def test_gh_called_with_url_ref_and_worktree_cwd_and_no_cached_record(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _script(repo, gh)
        verify_review_feedback(_task(repo), repo)
        assert [r.argv for r in gh.records] == [
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            [
                "gh",
                "pr",
                "view",
                _URL,
                "--json",
                "number,headRefOid,headRefName,url,headRepository",
            ],
        ]
        assert all(r.cwd == repo for r in gh.records)

    def test_repo_mismatch(self, repo: Path, gh: FakeGhCli) -> None:
        _script(repo, gh)
        gh.reply("repo", "view", stdout=json.dumps({"nameWithOwner": "x/y"}))
        _stale(verify_review_feedback(_task(repo), repo))

    @pytest.mark.parametrize(
        "override",
        [
            {"number": 8},
            {"url": "https://github.com/o/r/pull/8"},
            {"headRefOid": "b" * 40},
            {"headRefName": "dev/other"},
            {"number": None, "url": None},
            {"headRefOid": None},
        ],
    )
    def test_pr_view_mismatch_fails_closed(
        self, repo: Path, gh: FakeGhCli, override: dict[str, Any]
    ) -> None:
        _script(repo, gh, **override)
        _stale(verify_review_feedback(_task(repo), repo))

    def test_local_head_differs_from_reviewed_sha(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        reviewed = "c" * 40
        _script(repo, gh, headRefOid=reviewed)
        _stale(verify_review_feedback(_task(repo, reviewed_sha=reviewed), repo))

    def test_detached_head(self, repo: Path, gh: FakeGhCli) -> None:
        _script(repo, gh)
        git_in(repo, "checkout", "--detach")
        _stale(verify_review_feedback(_task(repo), repo))

    def test_unreadable_local_state_fails_closed(
        self, repo: Path, gh: FakeGhCli
    ) -> None:
        _script(repo, gh)
        boom = subprocess.CalledProcessError(128, "rev-parse")
        with patch("cw.codex_impl._feedback.git_output", side_effect=boom):
            _stale(verify_review_feedback(_task(repo), repo))

    def test_gh_nonzero_exit(self, repo: Path, gh: FakeGhCli) -> None:
        gh.reply("repo", "view", returncode=1, stderr="gh: not logged in")
        _stale(verify_review_feedback(_task(repo), repo))

    def test_pr_view_nonzero_exit(self, repo: Path, gh: FakeGhCli) -> None:
        _script(repo, gh)
        gh.reply("pr", "view", returncode=1, stderr="no PR")
        _stale(verify_review_feedback(_task(repo), repo))

    def test_timeout(self, repo: Path, gh: FakeGhCli) -> None:
        gh.reply("repo", "view", raises=subprocess.TimeoutExpired(cmd="gh", timeout=1))
        _stale(verify_review_feedback(_task(repo), repo))

    def test_malformed_json(self, repo: Path, gh: FakeGhCli) -> None:
        gh.reply("repo", "view", stdout="{not json")
        _stale(verify_review_feedback(_task(repo), repo))

    def test_missing_keys_in_a_sparse_payload(self, repo: Path, gh: FakeGhCli) -> None:
        gh.reply("repo", "view", stdout=json.dumps({"nameWithOwner": _REPO}))
        gh.reply("pr", "view", stdout=json.dumps({"number": _PR}))
        _stale(verify_review_feedback(_task(repo), repo))


def _origin_default_moves(
    repo: Path, tmp_path: Path, filename: str, content: str = "upstream\n"
) -> None:
    origin = git_in(repo, "remote", "get-url", "origin")
    push_commit_to_origin(Path(origin), "main", tmp_path / "side", filename, content)


class TestProbeDefaultBranch:
    def test_fetch_failure_is_friction_and_no_merge(
        self, repo: Path, tmp_path: Path
    ) -> None:
        git_in(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        outcome = probe_default_branch(repo, default_branch="main", repair=True)
        assert outcome.block is None
        assert outcome.needs_merge is False
        assert len(outcome.friction) == 1
        assert outcome.friction[0].startswith("merge_probe_unavailable")

    def test_a_fetch_that_cannot_run_is_friction_and_no_merge(self, repo: Path) -> None:
        timeout = subprocess.TimeoutExpired(cmd="fetch", timeout=1)
        with patch("cw.codex_impl._feedback.run_git", side_effect=timeout):
            outcome = probe_default_branch(repo, default_branch="main", repair=True)
        assert outcome.needs_merge is False
        assert outcome.friction[0].startswith("merge_probe_unavailable")
        assert "did not run" in outcome.friction[0]

    @pytest.mark.parametrize("repair", [False, True])
    def test_up_to_date_does_not_merge(self, repo: Path, repair: bool) -> None:
        outcome = probe_default_branch(repo, default_branch="main", repair=repair)
        assert (outcome.block, outcome.friction, outcome.needs_merge) == (
            None,
            (),
            False,
        )

    @pytest.mark.parametrize("repair", [False, True])
    def test_clean_probe_does_not_merge(
        self, repo: Path, tmp_path: Path, repair: bool
    ) -> None:
        commit_tracked_file(repo, "mine.py", "mine\n")
        git_in(repo, "push", "origin", _BRANCH)
        _origin_default_moves(repo, tmp_path, "theirs.py")
        outcome = probe_default_branch(repo, default_branch="main", repair=repair)
        assert (outcome.block, outcome.friction, outcome.needs_merge) == (
            None,
            (),
            False,
        )

    def _conflict(self, repo: Path, tmp_path: Path) -> None:
        commit_tracked_file(repo, "clash.py", "mine\n")
        _origin_default_moves(repo, tmp_path, "clash.py", "theirs\n")

    def test_conflict_on_fresh_row_blocks(self, repo: Path, tmp_path: Path) -> None:
        self._conflict(repo, tmp_path)
        outcome = probe_default_branch(repo, default_branch="main", repair=False)
        assert outcome.block is not None
        assert outcome.block.reason == IMPL_FAILED
        assert outcome.block.details.startswith("merge_conflict_with_default_branch")
        assert "clash.py" in outcome.block.details
        assert outcome.needs_merge is False

    def test_conflict_on_repair_row_asks_for_the_merge(
        self, repo: Path, tmp_path: Path
    ) -> None:
        self._conflict(repo, tmp_path)
        outcome = probe_default_branch(repo, default_branch="main", repair=True)
        assert outcome.block is None
        assert outcome.needs_merge is True

    def test_probe_is_read_only(self, repo: Path, tmp_path: Path) -> None:
        self._conflict(repo, tmp_path)
        probe_default_branch(repo, default_branch="main", repair=True)
        assert git_in(repo, "status", "--porcelain") == ""

    def test_probe_unavailable_after_a_good_fetch_is_friction_and_continues(
        self, repo: Path
    ) -> None:
        unavailable = MergeProbe("unavailable", None, (), "merge-tree too old")
        with patch(
            "cw.codex_impl._feedback.probe_default_merge", return_value=unavailable
        ):
            outcome = probe_default_branch(repo, default_branch="main", repair=True)
        assert outcome.block is None
        assert outcome.needs_merge is False
        assert outcome.friction == ("merge_probe_unavailable: merge-tree too old",)

    def test_unavailable_probe_is_friction_and_continues(
        self, repo: Path, tmp_path: Path
    ) -> None:
        git_in(repo, "update-ref", "-d", "refs/remotes/origin/main")
        git_in(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        outcome = probe_default_branch(repo, default_branch="main", repair=True)
        assert outcome.block is None
        assert outcome.needs_merge is False
        assert outcome.friction
        assert all(f.startswith("merge_probe_unavailable") for f in outcome.friction)


class TestMergeDefaultOnConflict:
    def test_merges_leaving_conflicts_and_computes_allowance_and_tree(
        self, repo: Path, tmp_path: Path
    ) -> None:
        commit_tracked_file(repo, "clash.py", "mine\n")
        _origin_default_moves(repo, tmp_path, "clash.py", "theirs\n")
        _origin_default_moves(repo, tmp_path, "other.py", "other\n")
        probe = probe_default_branch(repo, default_branch="main", repair=True)
        assert probe.needs_merge
        outcome = merge_default_on_conflict(repo, default_branch="main")
        assert not isinstance(outcome, ImplBlock)
        assert outcome.merge_files == frozenset({"clash.py", "other.py"})
        assert (repo / ".git" / "MERGE_HEAD").is_file()
        assert git_in(repo, "ls-files", "-u")
        assert git_in(repo, "cat-file", "-t", outcome.growth_tree) == "tree"

    def test_an_unmeasurable_merge_is_an_impl_block_and_does_not_merge(
        self, repo: Path
    ) -> None:
        unavailable = MergeTreeResult("unavailable", None, (), "too old")
        with patch("cw.codex_impl._feedback.merge_tree", return_value=unavailable):
            outcome = merge_default_on_conflict(repo, default_branch="main")
        assert isinstance(outcome, ImplBlock)
        assert "too old" in outcome.details
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_a_merge_git_refuses_to_start_is_an_impl_block(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _origin_default_moves(repo, tmp_path, "clash.py", "theirs\n")
        git_in(repo, "fetch", "origin", "main")
        (repo / "clash.py").write_text("untracked local\n", encoding="utf-8")
        outcome = merge_default_on_conflict(repo, default_branch="main")
        assert isinstance(outcome, ImplBlock)
        assert outcome.details.startswith("git merge origin/main failed")

    def test_a_failed_merge_is_an_impl_block(self, repo: Path) -> None:
        outcome = merge_default_on_conflict(repo, default_branch="no-such-branch")
        assert isinstance(outcome, ImplBlock)
        assert outcome.reason == IMPL_FAILED
