"""Tests for cw.codex_impl._resume and the re-entry flow in _run (#3529).

Run-level tests drive ``run_codex_impl_stage`` through the run tests' own
harness against a ``ScriptedCodexRunner``: a branch that already carries work
resumes, advances or adopts an unconcluded merge instead of blocking. The
disposition table itself is pinned in
``tests/test_stage_verification_impl_resume.py``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.branch_ahead import merge_in_progress
from cw.codex_impl._resume import (
    advance_payload,
    bound_operator_comments,
    resume_note,
)
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.dispatch.impl_gates import _should_gate_for_unconcluded_finalize_regress_merge
from cw.local_runner import GitFacts
from cw.merge_probe import MergeProbe
from cw.models import Stage, TicketTask
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.impl_feedback import MergeOutcome
from cw.stage_verification.impl_gates import run_scope_conformance
from cw.stage_verification.impl_merge_adopt import AdoptedMerge
from cw.stage_verification.impl_resume import Disposition, ReentryPlan
from cw.stage_verification.impl_staleness import Staleness
from cw.ticket_snapshot import SnapshotContext, normalize_ticket_id
from cw.tracker_io._prompt import REFRESH_TURN_MARKER
from tests._backend_mcp_helpers import stage_mcp_config
from tests._codex_review_helpers import assert_clean_after_refusal, exclude_cw_dir
from tests._shared.git_repos import (
    commit_tracked_file,
    diverge_origin_default,
    git_in,
    leave_conflicting_merge,
    push_commit_to_origin,
)
from tests._shared.scope_grant_builders import install_real_guard_script
from tests._shared.stop_hook_context import _write_hook_context_file
from tests._shared.ticket_context import make_comment, make_snapshot_from
from tests._tool_fakes import stdin_contains
from tests.test_codex_impl_run import (
    BRANCH,
    TICKET,
    WIDGET,
    Harness,
    _assert_block,
    _courier_client,
    _refresh_reply,
    _repin,
    _tracked_client,
    harness,
)
from tests.test_codex_plan_run import stage_events

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.models import ClientConfig

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")
__all__ = ["harness"]

_TRAILER = "Auto-Dev-Stage: impl-complete"
_RESUME_HEADER = "=== BEGIN CW-INJECTED: Resume (existing branch work) ==="
_SECTION_END = "=== END CW-INJECTED: Resume (existing branch work) ==="
_FAR_FUTURE = "2099-01-01T00:00:00Z"
# The real writer also drops .claude/settings.local.json beside the context.
_CONTEXT_PATTERN = (".claude/cw-context.json*", ".claude/settings.local.json")


def _commit(h: Harness, name: str, *, trailer: str | None = None) -> str:
    """Commit one new file on the branch (optionally trailered); return HEAD."""
    (h.worktree / name).write_text(f"{name}\n", encoding="utf-8")
    git_in(h.worktree, "add", name)
    args = ["commit", "-m", f"add {name}"]
    if trailer is not None:
        args += ["--trailer", trailer]
    git_in(h.worktree, *args)
    return h.head()


def _seed_marker(h: Harness) -> None:
    """Write the per-arrival regress marker through the real hook-context writer."""
    task = TicketTask(
        ticket_id=TICKET,
        client="test",
        stage=Stage.IMPL,
        regressed_into_stage=Stage.IMPL,
    )
    _write_hook_context_file(h.worktree, task=task, ticket_id=TICKET, client="test")
    exclude_cw_dir(h.worktree, _CONTEXT_PATTERN)


def _record_regress_head(sha: str) -> None:
    """Stamp the row's durable ``finalize_regress_branch_head``."""
    store = load_dev_queue()
    store.tasks[0].finalize_regress_branch_head = sha
    save_dev_queue(store)


def _staleness_script() -> None:
    install_real_guard_script("check_impl_guard_staleness.py")


def _github_client(tmp_path: Path) -> ClientConfig:
    """A GitHub-issues client: any non-agent commenter binds as operator text."""
    return _tracked_client(tmp_path, "github-issues")


def _resume_section(h: Harness) -> str:
    (call,) = h.impl_calls
    stdin = str(call["stdin"])
    assert _RESUME_HEADER in stdin
    return stdin.split(_RESUME_HEADER, 1)[1].split(_SECTION_END, 1)[0]


def _parents(h: Harness) -> list[str]:
    return git_in(h.worktree, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]


def _arrive_mid_merge(
    h: Harness, tmp_path: Path, name: str = "clash.py", **contents: str
) -> None:
    """Leave the worktree mid-merge, conflicted in *name* (a FINALIZE-regress)."""
    diverge_origin_default(
        h.worktree, h.origin, BRANCH, tmp_path / "side", name, **contents
    )
    leave_conflicting_merge(h.worktree)


def _backstop_task(h: Harness, recorded_head: str) -> TicketTask:
    return TicketTask(
        ticket_id=TICKET,
        client="test",
        stage=Stage.IMPL,
        worktree_path=h.worktree,
        finalize_regress_branch_head=recorded_head,
        finalize_regress_merge_conflict_detected=True,
    )


# --- s2_implementing: resume on top of the existing commits ------------------


def test_an_s2_implementing_branch_resumes_on_top_of_its_commits(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    commit_tracked_file(h.worktree, "prior.py")
    prior = h.head()
    h.turn()
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert h.commit_count() == 2
    assert len(result.commits) == 2
    assert prior in result.commits
    assert git_in(h.worktree, "rev-parse", "HEAD~1") == prior
    trailer = git_in(
        h.worktree, "log", "-1", "--format=%(trailers:key=Auto-Dev-Stage,valueonly)"
    )
    assert trailer == "impl-complete"
    assert h.origin_tip() == h.head()
    section = _resume_section(h)
    assert prior in section
    assert "prior.py" in section
    assert f"impl_resumed_from: {prior}" in result.friction_highlights
    entered = [e[1]["stage"] for e in stage_events() if e[0] == "stage.entered"]
    assert entered == ["s2_impl_started", "s2_impl_complete"]


def test_a_resumed_run_scope_checks_the_earlier_commits_too(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    commit_tracked_file(h.worktree, "prior.py")
    h.turn()
    with patch(
        "cw.codex_impl._run.run_scope_conformance", wraps=run_scope_conformance
    ) as spy:
        assert h.run().status == "stage_complete"
    assert {"prior.py", WIDGET} <= set(spy.call_args.kwargs["touched"])


def test_a_resumed_turn_that_changes_nothing_blocks(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    commit_tracked_file(h.worktree, "prior.py")
    start = h.head()
    h.turn(files={})
    result = h.run()
    _assert_block(result, IMPL_FAILED)
    assert result.blocker is not None
    assert result.blocker.details.startswith("resume_turn_made_no_change")
    assert h.head() == start
    assert git_in(h.worktree, "status", "--porcelain") == ""


# --- past_s2: advance with no turn --------------------------------------------


def test_a_past_s2_branch_advances_with_no_turn(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _staleness_script()
    head = _commit(h, "done.py", trailer=_TRAILER)
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert h.runner.calls == []
    assert h.origin_tip() == head
    assert result.commits == [head]
    assert (
        f"impl_advanced_past_s2: HEAD {head[:12]} carries the impl-complete "
        "trailer; no turn ran"
    ) in result.friction_highlights
    assert result.health.lowest_agent_confidence == "MEDIUM"
    assert result.health.any_incomplete_risk is True
    assert result.health.recommendation == "EXIT_FOR_HUMAN_REVIEW"
    entered = [e[1]["stage"] for e in stage_events() if e[0] == "stage.entered"]
    assert entered == ["s2_impl_started", "s2_impl_complete"]


def test_an_advance_whose_push_fails_blocks_impl_not_pushed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _commit(h, "done.py", trailer=_TRAILER)
    refused = subprocess.CalledProcessError(1, "push", stderr="denied")
    with patch("cw.codex_impl._run.push_and_verify_head", side_effect=refused):
        result = h.run()
    _assert_block(result, "impl_not_pushed")
    assert h.runner.calls == []


def test_a_trailer_on_an_earlier_commit_resumes_with_gates(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _commit(h, "done.py", trailer=_TRAILER)
    _commit(h, "later.py")
    h.turn()
    with patch(
        "cw.codex_impl._run.run_scope_conformance", wraps=run_scope_conformance
    ) as spy:
        result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert len(h.impl_calls) == 1
    spy.assert_called_once()
    trailer = git_in(
        h.worktree, "log", "-1", "--format=%(trailers:key=Auto-Dev-Stage,valueonly)"
    )
    assert trailer == "impl-complete"


def test_the_durable_regress_field_after_head_moved_advances(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates={"finalize_regress_branch_head": "a" * 40})
    _staleness_script()
    _commit(h, "done.py", trailer=_TRAILER)
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert h.runner.calls == []
    assert not any("resume_turn" in line for line in result.friction_highlights)


def test_the_durable_regress_field_equal_to_head_runs_a_turn(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _staleness_script()
    _record_regress_head(_commit(h, "done.py", trailer=_TRAILER))
    h.turn()
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert len(h.impl_calls) == 1
    assert "impl_guard_stale: regressed_to_impl" in result.friction_highlights


def test_a_regress_marker_runs_a_turn_and_delivers_only_operator_comments(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    operator = make_comment(
        "c1", body="please also handle the edge", created_at=_FAR_FUTURE
    )
    agent = make_comment(
        "c2", body="AGENT NOTE", created_at=_FAR_FUTURE, agent_authored=True
    )
    snapshot = make_snapshot_from(
        [operator, agent], ticket_id=normalize_ticket_id(TICKET), client="test"
    )
    h = harness(snapshot=snapshot)
    h.client = _github_client(tmp_path)
    _staleness_script()
    _commit(h, "done.py", trailer=_TRAILER)
    _seed_marker(h)
    h.turn()
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    section = _resume_section(h)
    assert "please also handle the edge" in section
    assert f"posted {_FAR_FUTURE} by operator" in section
    assert "AGENT NOTE" not in section
    assert "regressed_to_impl" in section


def test_an_operator_comment_after_head_runs_a_turn(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    operator = make_comment("c1", body="one more thing", created_at=_FAR_FUTURE)
    snapshot = make_snapshot_from(
        [operator], ticket_id=normalize_ticket_id(TICKET), client="test"
    )
    h = harness(snapshot=snapshot)
    h.client = _github_client(tmp_path)
    _staleness_script()
    _commit(h, "done.py", trailer=_TRAILER)
    h.turn()
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert "one more thing" in _resume_section(h)
    assert "impl_guard_stale: stale_comment_after_head" in result.friction_highlights


def test_a_past_s2_branch_without_a_snapshot_advances_and_records_the_load_failure(
    harness: Callable[..., Harness],
) -> None:
    h = harness(seed_snapshot=False)
    _staleness_script()
    _commit(h, "done.py", trailer=_TRAILER)
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert h.runner.calls == []
    assert "impl_guard_comments_load_failed" in result.friction_highlights


def test_an_agent_authored_newer_comment_alone_still_advances(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    agent = make_comment("c1", created_at=_FAR_FUTURE, agent_authored=True)
    snapshot = make_snapshot_from(
        [agent], ticket_id=normalize_ticket_id(TICKET), client="test"
    )
    h = harness(snapshot=snapshot)
    h.client = _github_client(tmp_path)
    _staleness_script()
    _commit(h, "done.py", trailer=_TRAILER)
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert h.runner.calls == []


def _mcp_comment_run(
    h: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    author_id: str,
) -> AutoDevResult:
    """A past_s2 MCP-bound run whose courier refresh returns one newer comment."""
    stage_mcp_config(monkeypatch, tmp_path, "codex", "enabled")
    _staleness_script()
    _commit(h, "done.py", trailer=_TRAILER)
    h.runner.add(
        stdin_contains(REFRESH_TURN_MARKER),
        _refresh_reply(
            comments=[
                {
                    "id": "c1",
                    "author_id": author_id,
                    "body": "a newer comment",
                    "created_at": _FAR_FUTURE,
                }
            ]
        ),
    )
    h.client = _courier_client(tmp_path)
    return h.run()


@pytest.mark.usefixtures("project_walk_stops_at_state_dir")
def test_a_newer_comment_from_a_non_operator_on_an_mcp_client_still_advances(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    result = _mcp_comment_run(h, tmp_path, monkeypatch, author_id="stranger")
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert h.impl_calls == []


@pytest.mark.usefixtures("project_walk_stops_at_state_dir")
def test_a_newer_comment_from_a_bound_operator_on_an_mcp_client_resumes(
    harness: Callable[..., Harness],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = harness()
    h.turn()
    result = _mcp_comment_run(h, tmp_path, monkeypatch, author_id="operator-1")
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert "a newer comment" in _resume_section(h)


def test_bound_operator_comments_filters_by_binding_or_returns_none() -> None:
    mine = make_comment("c1", author_id="operator-1")
    theirs = make_comment("c2", author_id="stranger")
    snapshot = make_snapshot_from([mine, theirs])
    ctx = SnapshotContext(github=False, operator_ids=("operator-1",))
    assert bound_operator_comments(snapshot, ctx) == [mine]
    assert bound_operator_comments(None, ctx) is None


# --- an unconcluded merge arriving with a regress ----------------------------


def test_an_unconcluded_merge_is_adopted_resolved_and_concluded(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    _arrive_mid_merge(h, tmp_path)
    _seed_marker(h)
    before = h.head()
    backstop = _backstop_task(h, before)
    assert _should_gate_for_unconcluded_finalize_regress_merge(backstop, {}) is True
    h.turn(files={"clash.py": "merged\n"})
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert len(_parents(h)) == 2
    assert merge_in_progress(h.worktree) is False
    assert h.origin_tip() == h.head()
    assert "- clash.py" in _resume_section(h)
    assert any(
        line.startswith("impl_unconcluded_merge_adopted: ")
        for line in result.friction_highlights
    )
    assert _should_gate_for_unconcluded_finalize_regress_merge(backstop, {}) is False


def test_an_unconcluded_merge_on_a_repair_row_is_adopted(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness(repair=True)
    _arrive_mid_merge(h, tmp_path)
    _repin(h)
    _seed_marker(h)
    h.turn(files={"clash.py": "merged\n"})
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert len(_parents(h)) == 2
    assert merge_in_progress(h.worktree) is False


def test_an_unconcluded_merge_without_a_regress_signal_blocks(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    _arrive_mid_merge(h, tmp_path)
    result = h.run()
    _assert_block(result, IMPL_FAILED)
    assert result.blocker is not None
    assert result.blocker.details.startswith("merge_in_progress:")
    assert h.runner.calls == []


def test_an_unconcluded_merge_on_a_repair_row_without_a_signal_blocks(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness(repair=True)
    _arrive_mid_merge(h, tmp_path)
    _repin(h)
    result = h.run()
    _assert_block(result, IMPL_FAILED)
    assert result.blocker is not None
    assert result.blocker.details.startswith("merge_in_progress:")
    assert h.runner.calls == []


def test_conflict_markers_left_in_a_conflicted_file_block_before_staging(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    _arrive_mid_merge(h, tmp_path)
    _seed_marker(h)
    start = h.head()
    origin_before = git_in(h.origin, "rev-parse", BRANCH)
    backstop = _backstop_task(h, start)
    h.turn(files={"clash.py": "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> x\n"})
    result = h.run()
    _assert_block(result, IMPL_FAILED)
    assert result.blocker is not None
    assert result.blocker.details.startswith("merge_in_progress:")
    assert "conflict markers remain in clash.py" in result.blocker.details
    assert git_in(h.origin, "rev-parse", BRANCH) == origin_before
    assert_clean_after_refusal(h.worktree, start)
    assert merge_in_progress(h.worktree) is False
    assert _should_gate_for_unconcluded_finalize_regress_merge(backstop, {}) is True


def _spy_staging(h: Harness, calls: list[str], index: list[str]) -> Any:
    """Patches recording the order of the merge check and every staging step."""
    from cw.codex_impl import _run as impl_run

    real_check = impl_run.merge_unresolved_block
    real_touched = impl_run.cycle_touched_paths
    real_stage = impl_run.stage_cycle

    def _check(*args: Any, **kwargs: Any) -> Any:
        calls.append("merge_unresolved_block")
        index.append(git_in(h.worktree, "ls-files", "-u"))
        return real_check(*args, **kwargs)

    def _touched(*args: Any, **kwargs: Any) -> Any:
        calls.append("cycle_touched_paths")
        return real_touched(*args, **kwargs)

    def _stage(*args: Any, **kwargs: Any) -> Any:
        calls.append("stage_cycle")
        return real_stage(*args, **kwargs)

    return (
        patch("cw.codex_impl._run.merge_unresolved_block", _check),
        patch("cw.codex_impl._run.cycle_touched_paths", _touched),
        patch("cw.codex_impl._run.stage_cycle", _stage),
    )


def test_an_untouched_binary_conflict_blocks_before_anything_is_staged(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    _arrive_mid_merge(h, tmp_path, "blob.bin", mine="a\x00\n", theirs="b\x00\n")
    _seed_marker(h)
    start = h.head()
    backstop = _backstop_task(h, start)
    origin_before = git_in(h.origin, "rev-parse", BRANCH)
    calls: list[str] = []
    index: list[str] = []
    h.turn()
    check, touched, stage = _spy_staging(h, calls, index)
    with check, touched, stage:
        result = h.run()
    _assert_block(result, IMPL_FAILED)
    assert result.blocker is not None
    assert result.blocker.details.startswith("merge_in_progress:")
    assert "blob.bin is still unmerged" in result.blocker.details
    # The check ran first: the conflicted path was still unmerged in the index,
    # cycle_touched_paths never ran, and the one stage_cycle is run.refused's own.
    assert "blob.bin" in index[0]
    assert "cycle_touched_paths" not in calls
    assert calls == ["merge_unresolved_block", "stage_cycle"]
    assert git_in(h.origin, "rev-parse", BRANCH) == origin_before
    assert_clean_after_refusal(h.worktree, start)
    assert merge_in_progress(h.worktree) is False
    assert _should_gate_for_unconcluded_finalize_regress_merge(backstop, {}) is True


def test_a_binary_conflict_the_turn_changed_is_concluded(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    _arrive_mid_merge(h, tmp_path, "blob.bin", mine="a\x00\n", theirs="b\x00\n")
    _seed_marker(h)
    h.turn(files={"blob.bin": "resolved\n"})
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert len(_parents(h)) == 2
    assert git_in(h.worktree, "ls-files", "-u") == ""


def test_a_merge_still_in_progress_after_the_commit_blocks_before_the_push(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    commit_tracked_file(h.worktree, "prior.py")
    h.turn()
    still = ImplBlock(IMPL_FAILED, "merge_in_progress: forced")
    with patch("cw.codex_impl._run.merge_unconcluded_block", return_value=still):
        result = h.run()
    _assert_block(result, IMPL_FAILED)
    assert result.blocker is not None
    assert result.blocker.details == "merge_in_progress: forced"
    assert h.commit_count() == 2
    assert h.origin_tip() != h.head()


# --- branch freshness on a resumed row ---------------------------------------


def test_a_resumed_row_with_a_clean_base_that_moved_makes_no_merge(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    commit_tracked_file(h.worktree, "prior.py")
    push_commit_to_origin(h.origin, "main", tmp_path / "side", "other.py")
    h.turn()
    result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert len(_parents(h)) == 1
    assert not any(line.startswith("merge_") for line in result.friction_highlights)


def test_a_resumed_row_whose_base_conflicts_blocks(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    commit_tracked_file(h.worktree, "prior.py")
    conflict = MergeProbe("conflict", "a" * 40, ("clash.py",), "")
    with patch(
        "cw.stage_verification.impl_feedback.probe_default_merge", return_value=conflict
    ):
        result = h.run()
    _assert_block(result, IMPL_FAILED)
    assert result.blocker is not None
    assert result.blocker.details.startswith("merge_conflict_with_default_branch")
    assert h.runner.calls == []


def test_a_failed_fetch_on_a_resumed_row_is_friction(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    commit_tracked_file(h.worktree, "prior.py")
    h.turn()
    with patch(
        "cw.stage_verification.impl_feedback.fetch_default_branch",
        return_value="no network",
    ):
        result = h.run()
    assert result.status == "stage_complete", result.blocker and result.blocker.details
    assert "merge_probe_unavailable: no network" in result.friction_highlights


# --- the resume note and the advance sentinel --------------------------------


def _facts(count: int) -> GitFacts:
    commits = [f"{index:040x}" for index in range(count, 0, -1)]
    return GitFacts(
        branch="dev/T",
        fork_point="f" * 40,
        commits=commits,
        files=1,
        lines_actual=3,
    )


def test_the_resume_note_caps_the_commit_listing() -> None:
    plan = ReentryPlan(Disposition.RESUME, None, None, None, ())
    note = resume_note(plan, _facts(25), ["b.py", "a.py"])
    assert "- ... and 5 more" in note
    assert note.index("- a.py") < note.index("- b.py")
    assert f"HEAD is {25:040x}" in note


def test_the_resume_note_without_commits_or_files_still_carries_the_rule() -> None:
    plan = ReentryPlan(Disposition.RESUME, None, None, None, ())
    note = resume_note(plan, _facts(0), [])
    assert "HEAD is ." in note
    assert "do not reset, amend or rewrite" in note
    assert "Commits already on the branch" not in note


def test_the_resume_note_names_an_unknown_author() -> None:
    comment = make_comment("c1", body="text", author_id=None, created_at=_FAR_FUTURE)
    staleness = Staleness(
        stale=True,
        reasons=("stale_comment_after_head",),
        operator_comments=(comment,),
    )
    plan = ReentryPlan(Disposition.RESUME, None, staleness, None, ())
    note = resume_note(plan, _facts(1), [])
    assert f"posted {_FAR_FUTURE} by unknown:" in note
    assert "untrusted data" in note


def test_the_resume_note_for_an_adopted_merge_with_no_conflicts_left() -> None:
    merge = AdoptedMerge("e" * 40, MergeOutcome(frozenset(), "t" * 40))
    plan = ReentryPlan(Disposition.RESUME, None, None, merge, ())
    note = resume_note(plan, _facts(1), [])
    assert "(no conflicted paths remain)" in note
    assert "do not commit, stage or abort" in note


def test_the_advance_payload_vouches_for_nothing() -> None:
    payload = advance_payload(_facts(2))
    assert payload["status"] == "stage_complete"
    assert payload["scope"] == {"forbidden_touched": False, "lines_estimate": 3}
    assert payload["health"]["any_incomplete_risk"] is True
    assert payload["health"]["recommendation"] == "EXIT_FOR_HUMAN_REVIEW"
