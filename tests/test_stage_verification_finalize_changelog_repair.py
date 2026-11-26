"""Tests for ``cw.stage_verification.finalize_changelog_repair`` (#3861).

Real git throughout (``tests._changelog_cut_helpers.epic_sibling_repo``: two
sibling epic children that both add a bullet to the same ``### Fixed`` hunk).
GitHub's mergeability is ``FakeGhCli`` (the shared ``gh`` fixture); the
post-push re-read is scripted by wrapping :func:`push_ahead` so the next
``gh pr view`` answers the state GitHub would report after the push.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.changelog_sync import ChangelogSync, SyncState
from cw.merge_probe import probe_default_merge
from cw.stage_verification import finalize_changelog_repair as repair
from cw.stage_verification.finalize_arm import (
    FREEZE_PREFIX,
    PR_CONFLICTING_PREFIX,
    ArmResult,
    _refusal,
)
from cw.stage_verification.finalize_pr import BEHIND_FRICTION
from cw.stage_verification.finalize_result import (
    AUTOMERGE_NOT_ARMED,
    IMPL_NOT_PUSHED,
    MERGE_CONFLICT_POST_PUSH,
    OPERATOR_UNAVAILABLE,
    FinalizeBlock,
)
from tests import _changelog_cut_helpers
from tests._codex_review_helpers import merge_view
from tests._git_sandbox import run as git_run
from tests._shared.git_repos import git_in

pytestmark = pytest.mark.release_cut

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests._tool_fakes import FakeGhCli

_URL = "https://github.com/acme/app/pull/9"
_BRANCH = "dev/GEN-1"
BRANCH_ARM = _changelog_cut_helpers.BRANCH
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
        assert outcome.block.details.endswith("mergeStateStatus=DIRTY)")
        assert "#3542" not in outcome.block.details
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

    @pytest.mark.parametrize(
        "pushed_block",
        [
            FinalizeBlock(IMPL_NOT_PUSHED, "push refused"),
            FinalizeBlock(OPERATOR_UNAVAILABLE, "ssh dropped", retry_eligible=True),
        ],
        ids=["impl_not_pushed", "unavailability_signature"],
    )
    def test_push_block_after_merge_is_surfaced_not_retry_eligible(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh: FakeGhCli,
        monkeypatch: pytest.MonkeyPatch,
        pushed_block: FinalizeBlock,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        gh.reply("pr", "view", _URL, stdout=_DIRTY)
        monkeypatch.setattr(repair, "push_ahead", lambda *_a, **_k: pushed_block)

        outcome = _check(clone)

        block = outcome.block
        assert block is not None
        assert block.reason == pushed_block.reason
        assert block.retry_eligible is False
        assert block.details.startswith(f"{pushed_block.details}; ")
        merge_sha = git_in(clone, "rev-parse", "HEAD")
        assert merge_sha in block.details
        assert f"the CHANGELOG union merge commit {merge_sha} exists only" in (
            block.details
        )
        assert "exists only in the local worktree" in block.details
        assert f'"git push origin HEAD:refs/heads/{_BRANCH}"' in block.details
        assert outcome.head is None
        # No reset: the unpushed merge is still local HEAD, ahead of origin.
        assert git_in(clone, "rev-parse", f"origin/{_BRANCH}") != merge_sha
        ahead = git_run(
            "merge-base",
            "--is-ancestor",
            f"origin/{_BRANCH}",
            "HEAD",
            cwd=clone,
            check=False,
        )
        assert ahead.returncode == 0

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


_FREEZE_STDERR = f"{FREEZE_PREFIX} the release-cut freeze holds this PR"
_CONFLICT_STDERR = f"{PR_CONFLICTING_PREFIX} GitHub reports the PR conflicting"


_PR: dict[str, object] = {"number": 9, "url": _URL}


def _arm_block(gh_stderr: str) -> FinalizeBlock:
    """The block ``finalize_arm.arm`` builds for an exit-1 refusal, hint included."""
    return _refusal(gh_stderr, "", _PR)


def _arm_refusal(gh_stderr: str) -> ArmResult:
    """An exit-1 arm refusal as ``finalize_arm.arm`` builds it."""
    return ArmResult(block=_arm_block(gh_stderr), gh_stderr=gh_stderr)


def _repair_arm(
    clone: Path,
    gh_stderr: str,
    *,
    base: str = "main",
    spent: frozenset[str] = frozenset(),
) -> repair.ArmRepair | None:
    return repair.repair_arm_refusal(
        clone,
        _arm_refusal(gh_stderr),
        spent=spent,
        base_branch=base,
        default_branch="main",
        branch=BRANCH_ARM,
    )


def _forbid_push(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_push(*_a: object, **_k: object) -> None:
        message = "a sync that moved nothing must not push"
        raise AssertionError(message)

    monkeypatch.setattr(repair, "push_ahead", _no_push)


class TestArmRefusalRepair:
    """The arm-time repairs of #3939: a release-cut freeze and a ``pr_conflicting:``."""

    def test_freeze_refusal_syncs_and_pushes_for_a_re_arm(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        clone = _changelog_cut_helpers.cut_straddle_repo(tmp_path, make_git_repo).clone
        before = git_in(clone, "rev-parse", "HEAD")

        outcome = _repair_arm(clone, _FREEZE_STDERR)

        assert outcome is not None
        assert outcome.block is None
        assert outcome.head == git_in(clone, "rev-parse", "HEAD")
        assert outcome.head == git_in(clone, "rev-parse", f"origin/{BRANCH_ARM}")
        git_in(clone, "merge-base", "--is-ancestor", before, outcome.head or "")
        assert outcome.friction == repair.RELEASE_CUT_SYNC_FRICTION

    def test_freeze_refusal_with_nothing_to_sync_re_arms_without_a_push(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _changelog_cut_helpers.epic_sibling_repo(tmp_path, make_git_repo)
        _forbid_push(monkeypatch)

        outcome = _repair_arm(clone, _FREEZE_STDERR)

        assert outcome == repair.ArmRepair(FREEZE_PREFIX)
        assert outcome is not None
        assert outcome.block is None
        assert outcome.head is None
        assert outcome.friction is None

    @pytest.mark.parametrize(
        "state", [SyncState.REFUSED, SyncState.CONFLICT, SyncState.UNAVAILABLE]
    )
    def test_a_freeze_sync_that_cannot_help_keeps_the_block_and_hint(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        state: SyncState,
    ) -> None:
        clone = _changelog_cut_helpers.epic_sibling_repo(tmp_path, make_git_repo)
        stuck = ChangelogSync(state, None, None, "x-disposition", (), (), "why")
        monkeypatch.setattr(repair, "sync_changelog", lambda *_a, **_k: stuck)
        _forbid_push(monkeypatch)

        outcome = _repair_arm(clone, _FREEZE_STDERR)

        assert outcome is not None
        assert outcome.block is not None
        assert outcome.head is None
        assert outcome.block.details.startswith(
            f"{FREEZE_PREFIX} changelog-sync {state} (x-disposition): why"
        )
        original = _arm_block(_FREEZE_STDERR)
        assert outcome.block.details.endswith(original.details)
        assert outcome.block.reason == original.reason
        assert outcome.block.pr_info == original.pr_info
        assert outcome.block.retry_eligible is True

    def test_a_dirty_worktree_refuses_the_freeze_sync(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        clone = _changelog_cut_helpers.cut_straddle_repo(tmp_path, make_git_repo).clone
        head = git_in(clone, "rev-parse", "HEAD")
        (clone / "CHANGELOG.md").write_text("dirty\n", encoding="utf-8")

        outcome = _repair_arm(clone, _FREEZE_STDERR)

        assert outcome is not None
        assert outcome.block is not None
        assert "refused (dirty_worktree)" in outcome.block.details
        assert git_in(clone, "rev-parse", "HEAD") == head

    def test_conflicting_refusal_union_syncs_and_pushes(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)

        outcome = _repair_arm(clone, _CONFLICT_STDERR, base="epic/1")

        assert outcome is not None
        assert outcome.block is None
        assert outcome.head == git_in(clone, "rev-parse", f"origin/{_BRANCH}")
        assert outcome.friction == repair.CHANGELOG_UNION_FRICTION

    def test_conflicting_refusal_on_the_default_base_is_not_repaired(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)

        def _boom(*_a: object, **_k: object) -> None:
            message = "the default base must not be fetched or synced"
            raise AssertionError(message)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(repair, "fetch_default_branch", _boom)
            patch.setattr(repair, "sync_changelog", _boom)
            assert _repair_arm(clone, _CONFLICT_STDERR) is None

    @pytest.mark.parametrize(
        "state",
        [
            SyncState.CLEAN,
            SyncState.CONFLICT,
            SyncState.REFUSED,
            SyncState.UNAVAILABLE,
        ],
    )
    def test_a_union_sync_that_does_not_move_keeps_the_block(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        state: SyncState,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        stuck = ChangelogSync(state, None, None, None, (), (), "why")
        monkeypatch.setattr(repair, "sync_changelog", lambda *_a, **_k: stuck)

        outcome = _repair_arm(clone, _CONFLICT_STDERR, base="epic/1")

        assert outcome is not None
        assert outcome.block is not None
        assert outcome.block.details.startswith(
            f"{PR_CONFLICTING_PREFIX} changelog-sync {state}"
        )
        assert outcome.head is None

    def test_a_union_fetch_failure_keeps_the_block_unsynced(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        git_in(clone, "remote", "set-url", "origin", str(tmp_path / "nope"))

        outcome = _repair_arm(clone, _CONFLICT_STDERR, base="epic/1")

        assert outcome is not None
        assert outcome.block is not None
        original = _arm_block(_CONFLICT_STDERR)
        assert outcome.block.details.startswith(
            f"{original.details}; repair not attempted: "
            "git fetch origin epic/1 failed: "
        )
        assert outcome.block.reason == original.reason
        assert outcome.block.retry_eligible == original.retry_eligible
        assert outcome.block.pr_info == original.pr_info
        assert not outcome.block.details.startswith(
            f"{PR_CONFLICTING_PREFIX} changelog-sync"
        )
        assert outcome.head is None

    @pytest.mark.parametrize(
        ("gh_stderr", "base", "merge_name"),
        [
            (FREEZE_PREFIX + " x", "main", "release-cut CHANGELOG merge commit"),
            (PR_CONFLICTING_PREFIX + " x", "epic/1", "CHANGELOG union merge commit"),
        ],
    )
    def test_a_failed_push_keeps_the_block_and_says_the_merge_is_local(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
        gh_stderr: str,
        base: str,
        merge_name: str,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        refused = FinalizeBlock(
            OPERATOR_UNAVAILABLE, "push refused", retry_eligible=True
        )
        monkeypatch.setattr(repair, "push_ahead", lambda *_a, **_k: refused)
        monkeypatch.setattr(
            repair,
            "sync_changelog",
            lambda *_a, **_k: ChangelogSync(
                SyncState.MOVED, "a" * 40, "b" * 40, None, (), (), ""
            ),
        )

        outcome = _repair_arm(clone, gh_stderr, base=base)

        assert outcome is not None
        assert outcome.head is None
        assert outcome.block is not None
        assert outcome.block.reason == AUTOMERGE_NOT_ARMED
        assert outcome.block.retry_eligible is False
        assert outcome.friction is None
        original = _arm_block(gh_stderr)
        assert outcome.block.details.startswith(f"{original.details}; push refused; ")
        assert f"the {merge_name} {'b' * 40} exists only in the local" in (
            outcome.block.details
        )
        assert "git push origin HEAD:refs/heads/" in outcome.block.details

    def test_moved_without_a_head_keeps_the_block_not_retryable(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)
        moved = ChangelogSync(SyncState.MOVED, "a" * 40, None, None, (), (), "")
        monkeypatch.setattr(repair, "sync_changelog", lambda *_a, **_k: moved)

        outcome = _repair_arm(clone, _FREEZE_STDERR)

        assert outcome is not None
        assert outcome.block is not None
        assert "moved but reported no head_sha" in outcome.block.details
        assert outcome.block.retry_eligible is False

    @pytest.mark.parametrize(
        ("gh_stderr", "spent"),
        [
            ("plain refusal", frozenset()),
            ("", frozenset()),
            (_FREEZE_STDERR, frozenset({FREEZE_PREFIX})),
            (_CONFLICT_STDERR, frozenset({PR_CONFLICTING_PREFIX})),
        ],
    )
    def test_unrepairable_refusals_return_none(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        gh_stderr: str,
        spent: frozenset[str],
    ) -> None:
        clone = _epic_repo(tmp_path, make_git_repo)

        assert _repair_arm(clone, gh_stderr, base="epic/1", spent=spent) is None
        no_block = repair.repair_arm_refusal(
            clone,
            ArmResult(),
            spent=frozenset(),
            base_branch="epic/1",
            default_branch="main",
            branch=BRANCH_ARM,
        )
        assert no_block is None
