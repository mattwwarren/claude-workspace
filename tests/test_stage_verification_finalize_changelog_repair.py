"""Tests for ``cw.stage_verification.finalize_changelog_repair`` (#3861).

Real git throughout (``tests._changelog_cut_helpers.epic_sibling_repo``: two
sibling epic children that both add a bullet to the same ``### Fixed`` hunk).
GitHub's mergeability is ``FakeGhCli`` (the shared ``gh`` fixture); the
post-push re-read is scripted by wrapping :func:`push_ahead` so the next
``gh pr view`` answers the state GitHub would report after the push.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.changelog_sync import ChangelogSync, SyncState
from cw.merge_probe import probe_default_merge
from cw.stage_verification import finalize_changelog_repair as repair
from cw.stage_verification.finalize_pr import BEHIND_FRICTION
from cw.stage_verification.finalize_result import (
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    OPERATOR_UNAVAILABLE,
    FinalizeBlock,
)
from tests import _changelog_cut_helpers
from tests._codex_review_helpers import merge_view
from tests._git_sandbox import run as git_run
from tests._shared.git_repos import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from tests._tool_fakes import FakeGhCli

_URL = "https://github.com/acme/app/pull/9"
_BRANCH = "dev/GEN-1"
_DIRTY = merge_view("CONFLICTING", "DIRTY")
_CLEAN = merge_view("MERGEABLE", "CLEAN")


def _epic_repo(tmp_path: Path, make_git_repo: Callable[..., Path]) -> Path:
    clone = _changelog_cut_helpers.epic_sibling_repo(tmp_path, make_git_repo)
    git_in(clone, "push", "-u", "origin", _BRANCH)
    return clone


def _view_count(gh: FakeGhCli) -> int:
    return sum(1 for argv in gh.argvs() if "view" in argv and _URL in argv)


def _after_push(
    monkeypatch: pytest.MonkeyPatch, gh: FakeGhCli, *, reads_after: str
) -> None:
    """Re-script the mergeability read that follows a successful push."""
    real_push = repair.push_ahead

    def _push_then_read(worktree: Path, *, branch: str, head_sha: str) -> object:
        block = real_push(worktree, branch=branch, head_sha=head_sha)
        if block is None:
            gh.reply("pr", "view", _URL, stdout=reads_after)
        return block

    monkeypatch.setattr(repair, "push_ahead", _push_then_read)


def _check(
    clone: Path, *, base: str = "epic/1", default: str = "main"
) -> repair.MergeabilityOutcome:
    return repair.check_mergeability_repairing(
        clone,
        _URL,
        base_branch=base,
        default_branch=default,
        branch=_BRANCH,
    )


class TestRepairAcceptance:
    def test_dirty_sibling_pr_is_repaired_with_one_plain_push(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        assert probe_default_merge(clone, "epic/1").state == "clean"
        assert _changelog_cut_helpers.github_would_conflict(
            clone, "HEAD", "origin/epic/1"
        )
        old_tip = git_in(clone, "rev-parse", "HEAD")
        gh.reply("pr", "view", _URL, stdout=_DIRTY)
        _after_push(monkeypatch, gh, reads_after=_CLEAN)

        outcome = _check(clone)

        assert outcome.block is None
        new_tip = git_in(clone, "rev-parse", "HEAD")
        assert outcome.head == new_tip
        assert git_in(clone, "rev-parse", f"origin/{_BRANCH}") == new_tip
        assert new_tip != old_tip
        ancestor = git_run(
            "merge-base", "--is-ancestor", old_tip, new_tip, cwd=clone, check=False
        )
        assert ancestor.returncode == 0
        assert repair.CHANGELOG_UNION_FRICTION in outcome.friction
        assert _view_count(gh) == 2

    def test_still_dirty_after_one_repair_parks_with_the_new_head(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        gh.reply("pr", "view", _URL, stdout=_DIRTY)
        _after_push(monkeypatch, gh, reads_after=_DIRTY)

        outcome = _check(clone)

        assert outcome.block is not None
        assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
        assert "still DIRTY after one CHANGELOG union merge" in outcome.block.details
        assert outcome.head == git_in(clone, "rev-parse", "HEAD")
        assert repair.CHANGELOG_UNION_FRICTION not in outcome.friction
        assert _view_count(gh) == 2


class TestRepairPassthrough:
    def test_default_base_is_never_repaired_or_fetched(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        gh.reply("pr", "view", _URL, stdout=_DIRTY)

        def _no_fetch(*_a: object, **_k: object) -> None:
            msg = "default base must not fetch"
            raise AssertionError(msg)

        monkeypatch.setattr(repair, "fetch_default_branch", _no_fetch)
        head_before = git_in(clone, "rev-parse", "HEAD")

        outcome = _check(clone, base="main", default="main")

        assert outcome.block is not None
        assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
        assert outcome.block.details.endswith("cw does not resolve conflicts (#3542)")
        assert outcome.head is None
        assert git_in(clone, "rev-parse", "HEAD") == head_before

    def test_genuine_conflict_parks_naming_the_file_and_pushes_nothing(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        git_in(clone, "checkout", "epic/1")
        _changelog_cut_helpers.commit_files(
            clone, {"src/app.py": "def app():\n    return 3\n"}, "feat: epic app"
        )
        git_in(clone, "push", "origin", "epic/1")
        git_in(clone, "checkout", _BRANCH)
        head_before = git_in(clone, "rev-parse", "HEAD")
        remote_before = git_in(clone, "rev-parse", f"origin/{_BRANCH}")
        gh.reply("pr", "view", _URL, stdout=_DIRTY)

        outcome = _check(clone)

        assert outcome.block is not None
        assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
        assert "src/app.py" in outcome.block.details
        assert outcome.head is None
        assert git_in(clone, "rev-parse", "HEAD") == head_before
        assert git_in(clone, "rev-parse", f"origin/{_BRANCH}") == remote_before

    def test_changelog_changed_on_one_side_keeps_the_block_with_a_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        gh.reply("pr", "view", _URL, stdout=_DIRTY)

        outcome = repair.check_mergeability_repairing(
            clone,
            _URL,
            base_branch="main",
            default_branch="trunk",
            branch=_BRANCH,
        )

        assert outcome.block is not None
        assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
        assert "not explained by CHANGELOG union" in outcome.block.details
        assert outcome.head is None

    def test_fetch_failure_keeps_the_block_and_says_repair_not_attempted(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        git_in(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
        gh.reply("pr", "view", _URL, stdout=_DIRTY)

        outcome = _check(clone)

        assert outcome.block is not None
        assert outcome.block.retry_eligible is True
        assert "repair not attempted" in outcome.block.details
        assert outcome.head is None

    def test_push_block_is_surfaced(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        gh.reply("pr", "view", _URL, stdout=_DIRTY)
        pushed_block = FinalizeBlock(IMPL_NOT_PUSHED, "push refused")
        monkeypatch.setattr(repair, "push_ahead", lambda *_a, **_k: pushed_block)

        outcome = _check(clone)

        assert outcome.block is pushed_block
        assert outcome.head is None

    def test_mergeable_behind_and_unreadable_view_pass_through_without_fetch(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)

        def _no_fetch(*_a: object, **_k: object) -> None:
            msg = "passthrough must not fetch"
            raise AssertionError(msg)

        monkeypatch.setattr(repair, "fetch_default_branch", _no_fetch)

        gh.reply("pr", "view", _URL, stdout=_CLEAN)
        assert _check(clone).block is None

        gh.reply("pr", "view", _URL, stdout=merge_view("MERGEABLE", "BEHIND"))
        behind = _check(clone)
        assert behind.block is None
        assert behind.friction == [BEHIND_FRICTION]

        gh.reply("pr", "view", _URL, returncode=1, stderr="boom")
        unreadable = _check(clone)
        assert unreadable.block is not None
        assert unreadable.block.reason == OPERATOR_UNAVAILABLE


class TestRepairSeamStates:
    def test_refused_seam_appends_the_note(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        gh.reply("pr", "view", _URL, stdout=_DIRTY)
        refused = ChangelogSync(
            SyncState.REFUSED,
            "a" * 40,
            "b" * 40,
            "dirty_worktree",
            (),
            (),
            "tracked files have uncommitted changes",
        )
        monkeypatch.setattr(repair, "sync_changelog", lambda *_a, **_k: refused)

        outcome = _check(clone)

        assert outcome.block is not None
        assert outcome.block.reason == MERGE_CONFLICT_POST_PUSH
        assert "changelog-sync refused (dirty_worktree)" in outcome.block.details
        assert outcome.head is None

    def test_moved_without_a_head_is_an_unavailable_block(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        gh.reply("pr", "view", _URL, stdout=_DIRTY)
        moved = ChangelogSync(SyncState.MOVED, "a" * 40, None, None, (), (), "")
        monkeypatch.setattr(repair, "sync_changelog", lambda *_a, **_k: moved)

        outcome = _check(clone)

        assert outcome.block is not None
        assert outcome.block.reason == OPERATOR_UNAVAILABLE
        assert "no head_sha" in outcome.block.details
        assert outcome.head is None
