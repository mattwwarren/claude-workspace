"""The codex FINALIZE conflict paths end to end (#3542).

The same driver-core harness as ``test_codex_finalize_run.py``: a real
sandboxed worktree on a bare origin, a scripted codex runner and ``gh``, and
the real ``classify_merge_conflict.py`` in the per-test HOME. The sibling push
that races the PR lands from the turn callback, after the pre-push probe and
before ``gh pr create``: the real window Step 4c.5 closes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cw.changelog_sync import ChangelogSync, SyncState
from cw.stage_verification import finalize_changelog_repair as repair
from cw.stage_verification import finalize_conflict_resolve as resolve
from cw.stage_verification.finalize_changelog_repair import MergeabilityOutcome
from cw.stage_verification.finalize_result import (
    MERGE_CONFLICT_POST_PUSH,
    FinalizeBlock,
)
from tests._codex_review_helpers import install_home_prep_pr_state, merge_view
from tests._git_sandbox import run as git_run
from tests._shared.base_branch_builders import ROW_BASE, push_epic_base
from tests._shared.git_repos import commit_tracked_file, git_in, push_commit_to_origin
from tests._shared.scope_grant_builders import install_real_guard_script
from tests._tool_fakes import (
    GhReply,
    finalize_turn_result,
    stdin_contains,
    stub_prep_pr_finalize,
)
from tests.test_codex_finalize_run import (
    _CLEAN,
    BRANCH,
    FINALIZE_TURN,
    PR_NUMBER,
    PR_URL,
    Harness,
    _pr_list,
    _run_handing,
    _view,
    harness,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.codex_runner import CodexRunResult

__all__ = ["harness"]

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")

_DOC = "CHANGELOG-notes.md"
_GATE_LOG = "gate-runs.log"
_MERGE_FIELDS = "mergeable,mergeStateStatus"
_READ_BACK = "headRefName,headRefOid,baseRefName,state,autoMergeRequest"
_BIND = "number,url,headRefName,headRefOid"
_PR_INFO = {"number": PR_NUMBER, "url": PR_URL, "auto_merge": False, "base": "main"}


def _script_head(h: Harness, head: str) -> None:
    """Re-script every gh read that names the PR head."""
    h.head = head
    h.gh.reply("pr", "view", PR_URL, "--json", _BIND, stdout=_view(headRefOid=head))
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        _READ_BACK,
        stdout=json.dumps(
            {
                "headRefName": BRANCH,
                "headRefOid": head,
                "baseRefName": "main",
                "state": "OPEN",
                "autoMergeRequest": {"mergeMethod": "SQUASH"},
            }
        ),
    )


def _merge_view(h: Harness, mergeable: str, state: str) -> None:
    h.gh.reply(
        "pr",
        "view",
        PR_URL,
        "--json",
        _MERGE_FIELDS,
        stdout=merge_view(mergeable, state),
    )


def _branch_commit(h: Harness, name: str, content: str) -> None:
    """Commit *name* on the branch, push it and re-script gh for the new head."""
    commit_tracked_file(h.worktree, name, content)
    git_in(h.worktree, "push", "origin", BRANCH)
    _script_head(h, h.local_head())


def _racing_turn(h: Harness, tmp_path: Path, name: str, theirs: str) -> None:
    """The turn pushes a sibling commit to origin/main, then answers."""
    reply = finalize_turn_result(None)

    def _answer(_argv: list[str], _stdin: str | None) -> CodexRunResult:
        push_commit_to_origin(h.origin, "main", tmp_path / "side", name, theirs)
        return reply

    h.runner.add(stdin_contains(FINALIZE_TURN), _answer)


def _logging_gates(tmp_path: Path, command: str = "true") -> Path:
    log = tmp_path / _GATE_LOG
    install_home_prep_pr_state(
        tmp_path / "gate-state", ("lint", f"echo run >> {log}; {command}", 5.0)
    )
    return log


def _runs(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").split()) if log.exists() else 0


def _publish_rescripts(h: Harness, mergeable: str = "MERGEABLE") -> None:
    """After the resolver's push, GitHub reports the new head and *mergeable*."""
    real_push = resolve.push_ahead

    def _push(worktree: Path, *, branch: str, head_sha: str) -> object:
        block = real_push(worktree, branch=branch, head_sha=head_sha)
        if block is None:
            _script_head(h, head_sha)
            _merge_view(h, mergeable, "CLEAN" if mergeable == "MERGEABLE" else "DIRTY")
        return block

    h.monkeypatch.setattr(resolve, "push_ahead", _push)


def _mid_merge(h: Harness) -> bool:
    done = git_run(
        "rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=h.worktree, check=False
    )
    return done.returncode == 0


def test_a_conflicting_default_branch_merge_is_resolved_pushed_and_shipped(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    h = harness().trace()
    log = _logging_gates(tmp_path)
    _branch_commit(h, _DOC, "ours\n")
    pre_merge = h.local_head()
    _racing_turn(h, tmp_path, _DOC, "theirs\n")
    _merge_view(h, "CONFLICTING", "DIRTY")
    _publish_rescripts(h)

    result = h.run()

    assert result.status == "shipped"
    merged = h.local_head()
    parents = git_in(h.worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1] == pre_merge
    assert len(parents) == 3
    body = git_in(h.worktree, "log", "-1", "--format=%B")
    assert resolve.AUTO_RESOLVED_TRAILER in body
    text = (h.worktree / _DOC).read_text(encoding="utf-8")
    assert "ours" in text
    assert "theirs" in text
    assert h.origin_tip() == merged
    assert _runs(log) == 2
    arm_argv = h.arm.calls()[-1]
    assert arm_argv[0] == "arm-automerge"
    assert arm_argv[arm_argv.index("--head-sha") + 1] == merged
    assert resolve.SEMANTIC_RESOLVED in result.friction_highlights
    assert "resolve_path_gates: passed" in result.friction_highlights
    assert h.order.count("run_approval_fence") == 2


def test_an_unsafe_conflict_parks_with_pr_info_and_pushes_nothing(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    h = harness()
    _branch_commit(h, "clash.py", "mine = 1\n")
    head, tip = h.local_head(), h.origin_tip()
    _racing_turn(h, tmp_path, "clash.py", "theirs = 2\n")
    _merge_view(h, "CONFLICTING", "DIRTY")

    result, payload = _run_handing(h)

    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == MERGE_CONFLICT_POST_PUSH
    assert "semantic auto-resolve attempted — refused" in result.blocker.details
    assert payload["pr_info"] == _PR_INFO
    assert h.local_head() == head
    assert not _mid_merge(h)
    assert h.origin_tip() == tip
    assert [c[0] for c in h.arm.calls()] == []


def test_a_gate_red_only_after_the_merge_is_reverted(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    install_real_guard_script(resolve.CLASSIFIER_SCRIPT_NAME)
    h = harness()
    _logging_gates(tmp_path, f'test "$(wc -l < {_DOC})" -le 1')
    _branch_commit(h, _DOC, "ours\n")
    head, tip = h.local_head(), h.origin_tip()
    _racing_turn(h, tmp_path, _DOC, "theirs\n")
    _merge_view(h, "CONFLICTING", "DIRTY")

    result = h.run()

    assert result.blocker is not None
    assert result.blocker.reason == MERGE_CONFLICT_POST_PUSH
    assert "but gate lint failed" in result.blocker.details
    assert "; reverted" in result.blocker.details
    assert h.local_head() == head
    assert h.origin_tip() == tip
    assert h.arm.calls() == []


def test_a_union_repair_that_moved_head_is_never_resolved_again(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    still = FinalizeBlock(
        MERGE_CONFLICT_POST_PUSH,
        f"{PR_URL} is not mergeable after the push (mergeable=CONFLICTING, "
        "mergeStateStatus=DIRTY); still DIRTY after one CHANGELOG union merge",
        retry_eligible=True,
    )

    def _repair(*_a: Any, **_k: Any) -> MergeabilityOutcome:
        return MergeabilityOutcome(still, [], h.head)

    def _never(*_a: Any, **_k: Any) -> object:
        msg = "the resolver must not run after a union merge"
        raise AssertionError(msg)

    h.monkeypatch.setattr(
        "cw.codex_finalize._run.check_mergeability_repairing", _repair
    )
    h.monkeypatch.setattr("cw.codex_finalize._run.resolve_post_push_conflict", _never)
    head = h.local_head()

    result, payload = _run_handing(h)

    assert result.blocker is not None
    assert result.blocker.details == still.details
    assert payload["pr_info"] == _PR_INFO
    assert h.local_head() == head


def test_a_refused_changelog_repair_is_never_followed_by_a_merge(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness(task_updates={"base_branch": ROW_BASE})
    push_epic_base(h.worktree)
    log = _logging_gates(tmp_path)
    head, tip = h.local_head(), h.origin_tip()
    # The base moves while the turn runs, so a bare merge would have work to do.
    reply = finalize_turn_result(None)

    def _answer(_argv: list[str], _stdin: str | None) -> CodexRunResult:
        side = tmp_path / "side"
        push_commit_to_origin(h.origin, ROW_BASE, side, "sibling.py", "x = 1\n")
        return reply

    h.runner.add(stdin_contains(FINALIZE_TURN), _answer)
    _merge_view(h, "CONFLICTING", "DIRTY")

    def _refused(*_a: Any, **_k: Any) -> ChangelogSync:
        return ChangelogSync(
            SyncState.REFUSED,
            None,
            head,
            "changelog_released_section_touched",
            (),
            (),
            "released section(s) 1.0.0 would change",
        )

    h.monkeypatch.setattr(repair, "sync_changelog", _refused)

    result, payload = _run_handing(h)

    assert result.blocker is not None
    assert result.blocker.reason == MERGE_CONFLICT_POST_PUSH
    assert result.blocker.details.endswith(
        "changelog-sync refused (changelog_released_section_touched): released "
        "section(s) 1.0.0 would change; no merge attempted: the CHANGELOG repair "
        "refused to merge origin/epic/x"
    )
    assert payload["pr_info"] == {**_PR_INFO, "base": ROW_BASE}
    assert h.local_head() == head
    assert h.origin_tip() == tip
    assert _runs(log) == 1
    assert h.arm.calls() == []


@pytest.mark.parametrize(
    ("decision", "status"),
    [("clean", "shipped"), ("continue", "review_pending_approval")],
)
def test_clean_mode_runs_the_delta_confirm_round_before_the_arm(
    harness: Callable[..., Harness], decision: str, status: str
) -> None:
    h = harness()
    h.turn()
    eligible = GhReply(
        1,
        json.dumps(
            {
                "decision": "rereview_required",
                "reason": "commits_after_clean_round",
                "delta_confirm_eligible": True,
                "resolution_files": [_DOC],
            }
        ),
    )
    h.set_cw("review converge config", _CLEAN)
    h.set_cw(
        "review converge verify-tip", [GhReply(0, "{}"), eligible, GhReply(0, "{}")]
    )
    h.set_cw(
        "review converge confirm-job delta",
        [
            GhReply(75, "", "CONFIRM_JOB_ID=job-1\n"),
            GhReply(0, json.dumps({"decision": decision})),
        ],
    )

    result = h.run()

    assert result.status == status
    if status == "review_pending_approval":
        assert any(
            "delta confirm: findings" in entry for entry in result.friction_highlights
        )
        assert "arm-automerge" not in [c[0] for c in h.arm.calls()]
    else:
        assert [c[0] for c in h.arm.calls()][-1] == "arm-automerge"


def test_a_dirty_reused_pr_makes_no_merge_and_says_so(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    _merge_view(h, "CONFLICTING", "DIRTY")
    head = h.local_head()

    result, payload = _run_handing(h)

    assert result.blocker is not None
    assert result.blocker.reason == MERGE_CONFLICT_POST_PUSH
    assert result.blocker.details.endswith(
        "; no merge attempted: cw resolves only on a run that created the PR"
    )
    assert payload["pr_info"] == _PR_INFO
    assert h.local_head() == head


def test_an_adopted_row_with_a_red_reuse_gate_is_agent_block(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness(task_updates={"branch": BRANCH})
    install_home_prep_pr_state(
        tmp_path / "gate-state", ("lint", "echo nope; [ ! -f feature.py ]", 5.0)
    )
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))

    result = h.run()

    assert result.blocker is not None
    assert result.blocker.reason == "agent_block"
    assert "reuse-path quality gate `lint`" in result.blocker.details


def test_an_adopted_draft_is_marked_ready_before_the_arm(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates={"branch": BRANCH})
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))

    result = h.run()

    assert result.status == "shipped"
    # The reuse path releases any CI hold first.
    assert [c[0] for c in h.arm.calls()] == [
        "ci-hold",
        "check-automerge-allowed",
        "ready",
        "arm-automerge",
    ]


def test_an_adopted_draft_that_cannot_be_marked_ready_is_never_armed(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates={"branch": BRANCH})
    h.gh.reply("pr", "list", "--head", BRANCH, stdout=_pr_list("OPEN"))
    h.arm = stub_prep_pr_finalize(
        Path.home(),
        ready_exit=1,
        ready_stdout=json.dumps(
            {
                "pr_number": PR_NUMBER,
                "status": "failed",
                "flipped": True,
                "gh_exit_code": 1,
                "gh_stderr": "HTTP 403: forbidden",
                "detail": "gh pr ready exited 1; refusing to trust the read-back",
            }
        ),
    )

    result, payload = _run_handing(h)

    assert result.blocker is not None
    assert result.blocker.reason == "agent_block"
    assert result.stage_reached == "stage5_post_create"
    assert result.blocker.retry_eligible is True
    assert result.blocker.details == (
        f"adopted draft PR #{PR_NUMBER} could not be marked ready: HTTP 403: forbidden"
    )
    assert payload["pr_info"] == {"number": PR_NUMBER, "url": PR_URL}
    assert [c[0] for c in h.arm.calls()] == [
        "ci-hold",
        "check-automerge-allowed",
        "ready",
    ]


def test_an_unreadable_mergeability_view_carries_no_conflict_pr_info(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    h.turn()
    h.gh.reply("pr", "view", PR_URL, "--json", _MERGE_FIELDS, returncode=1)

    result, payload = _run_handing(h)

    assert result.blocker is not None
    assert result.blocker.reason == "operator_unavailable"
    assert "pr_info" not in payload
