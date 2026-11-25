"""Tests for cw.stage_verification.impl_resume: the IMPL re-entry contract (#3529).

Real ``git`` repos through ``tests/_git_sandbox``; the staleness script is the
repo's real ``check_impl_guard_staleness.py`` installed under the per-test HOME.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from cw.local_runner import git_facts
from cw.models import Stage, TicketTask
from cw.models.review_feedback import ReviewFeedback
from cw.review_park_findings import write_park_findings
from cw.stage_verification.impl_block import IMPL_FAILED, ImplBlock
from cw.stage_verification.impl_resume import (
    Disposition,
    EntryKind,
    ReentryPlan,
    classify_entry,
    plan_reentry,
    resume_disposition,
)
from tests._codex_review_helpers import fresh_impl_repo
from tests._review_history_helpers import write_held_ledger
from tests._shared.git_repos import (
    diverge_origin_default,
    git_in,
    leave_conflicting_merge,
    push_commit_to_origin,
)
from tests._shared.review_builders import (
    _make_finding,
    _make_gap_finding,
    regressed_review_row,
    write_review_verdict_envelope,
)
from tests._shared.scope_grant_builders import install_real_guard_script
from tests._shared.stop_hook_context import _write_hook_context_file
from tests._shared.ticket_context import make_comment

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_BRANCH = "dev/T-1"
_TRAILER = "Auto-Dev-Stage: impl-complete"
_STALENESS_SCRIPT = "check_impl_guard_staleness.py"
_LONG_AGO = "2000-01-01T00:00:00Z"
_FAR_FUTURE = "2099-01-01T00:00:00Z"


def _feedback() -> ReviewFeedback:
    return ReviewFeedback(
        repo="o/r",
        pr_number=1,
        reviewed_sha="a" * 40,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        report="red",
    )


def _task(**updates: Any) -> TicketTask:
    return TicketTask(ticket_id="T-1", client="test", **updates)


def _commit(repo: Path, name: str, *, trailer: str | None = None) -> None:
    """Commit one new file, optionally with a trailer on the commit."""
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git_in(repo, "add", name)
    args = ["commit", "-m", f"add {name}"]
    if trailer is not None:
        args += ["--trailer", trailer]
    git_in(repo, *args)


def _seed_marker(repo: Path) -> None:
    """Write the per-arrival ``regressed_into_stage`` marker through the real writer."""
    task = _task(stage=Stage.IMPL, regressed_into_stage=Stage.IMPL)
    _write_hook_context_file(repo, task=task, ticket_id="T-1", client="test")


@pytest.fixture
def repo_origin(make_git_repo: Callable[..., Path]) -> tuple[Path, Path]:
    return fresh_impl_repo(make_git_repo, "resume-wt", _BRANCH)


@pytest.fixture
def repo(repo_origin: tuple[Path, Path]) -> Path:
    return repo_origin[0]


@pytest.fixture
def staleness_script() -> Path:
    return install_real_guard_script(_STALENESS_SCRIPT)


def _facts(repo: Path) -> Any:
    return git_facts(repo, "main")


def _plan(
    repo: Path,
    tmp_path: Path,
    task: TicketTask | None = None,
    *,
    comments: Any = (),
) -> ReentryPlan | ImplBlock | None:
    return plan_reentry(
        task=task or _task(),
        worktree=repo,
        base_branch="main",
        facts=_facts(repo),
        comments=comments,
        scratch=tmp_path / "scratch",
    )


def _reentry(result: ReentryPlan | ImplBlock | None) -> ReentryPlan:
    assert isinstance(result, ReentryPlan), result
    return result


class TestClassifyEntry:
    def test_no_commits_is_fresh(self, repo: Path) -> None:
        entry = classify_entry(_facts(repo), _task(), repo)
        assert entry is not None
        assert not isinstance(entry, ImplBlock)
        assert entry.kind is EntryKind.FRESH
        assert entry.commits == ()
        assert entry.friction == ()

    def test_commits_without_the_trailer_are_s2_implementing(self, repo: Path) -> None:
        _commit(repo, "a.py")
        entry = classify_entry(_facts(repo), _task(), repo)
        assert entry is not None
        assert not isinstance(entry, ImplBlock)
        assert entry.kind is EntryKind.S2_IMPLEMENTING
        assert entry.head_sha == git_in(repo, "rev-parse", "HEAD")
        assert len(entry.commits) == 1

    def test_the_trailer_on_head_is_past_s2(self, repo: Path) -> None:
        _commit(repo, "a.py", trailer=_TRAILER)
        entry = classify_entry(_facts(repo), _task(), repo)
        assert entry is not None
        assert not isinstance(entry, ImplBlock)
        assert entry.kind is EntryKind.PAST_S2

    def test_a_trailer_on_an_earlier_commit_is_not_past_s2(self, repo: Path) -> None:
        _commit(repo, "a.py", trailer=_TRAILER)
        _commit(repo, "b.py")
        entry = classify_entry(_facts(repo), _task(), repo)
        assert entry is not None
        assert not isinstance(entry, ImplBlock)
        assert entry.kind is EntryKind.S2_IMPLEMENTING
        assert len(entry.commits) == 2

    def test_a_fix_cycle_trailer_alone_is_s2_implementing(self, repo: Path) -> None:
        _commit(repo, "a.py", trailer="Auto-Dev-Fix-Cycle: 1")
        entry = classify_entry(_facts(repo), _task(), repo)
        assert entry is not None
        assert not isinstance(entry, ImplBlock)
        assert entry.kind is EntryKind.S2_IMPLEMENTING

    def test_a_repair_row_returns_none(self, repo: Path) -> None:
        _commit(repo, "a.py")
        task = _task(review_feedback=_feedback())
        assert classify_entry(_facts(repo), task, repo) is None

    def test_an_unresolvable_fork_point_blocks(self, repo: Path) -> None:
        facts = _facts(repo)
        facts["fork_point"] = ""
        block = classify_entry(facts, _task(), repo)
        assert isinstance(block, ImplBlock)
        assert block.reason == IMPL_FAILED
        assert "fork" in block.details

    def test_regressed_row_with_verdict_is_exempt(self, repo: Path) -> None:
        _commit(repo, "a.py")
        write_review_verdict_envelope(
            repo, ticket_id="T-1", reviewed_sha="a" * 40, must_fix=[_make_finding()]
        )
        task = _task(stage=Stage.IMPL, stage_high_water=Stage.REVIEW)
        assert classify_entry(_facts(repo), task, repo) is None

    def test_regressed_row_with_saved_park_findings_is_exempt(self, repo: Path) -> None:
        _commit(repo, "a.py")
        assert write_park_findings(
            repo, ticket_id="T-1", reviewed_sha="a" * 40, text="parked finding"
        )
        task = _task(stage=Stage.IMPL, stage_high_water=Stage.REVIEW)
        assert classify_entry(_facts(repo), task, repo) is None

    def test_pre_pr_gap_hold_row_is_exempt(self, repo: Path) -> None:
        _commit(repo, "a.py")
        write_review_verdict_envelope(
            repo,
            ticket_id="T-1",
            reviewed_sha="a" * 40,
            held=[_make_gap_finding(summary="B")],
        )
        task = regressed_review_row("T-1", blocked_reason="review_operator_actionable")
        assert classify_entry(_facts(repo), task, repo) is None

    def test_pre_pr_clean_mode_gap_hold_row_is_exempt(self, repo: Path) -> None:
        _commit(repo, "a.py")
        write_held_ledger(
            repo, [[_make_gap_finding(summary="B")], [], []], ticket_id="T-1"
        )
        task = regressed_review_row("T-1", blocked_reason="review_operator_actionable")
        assert classify_entry(_facts(repo), task, repo) is None

    def test_withheld_findings_add_the_friction_line(self, repo: Path) -> None:
        _commit(repo, "a.py")
        task = _task(stage=Stage.IMPL, stage_high_water=Stage.REVIEW)
        entry = classify_entry(_facts(repo), task, repo)
        assert entry is not None
        assert not isinstance(entry, ImplBlock)
        assert entry.friction == (
            "regress_review_findings: withheld (artifact_missing)",
        )

    def test_a_plain_row_has_no_regress_friction(self, repo: Path) -> None:
        _commit(repo, "a.py")
        entry = classify_entry(_facts(repo), _task(), repo)
        assert entry is not None
        assert not isinstance(entry, ImplBlock)
        assert entry.friction == ()


_KINDS = (None, EntryKind.FRESH, EntryKind.S2_IMPLEMENTING, EntryKind.PAST_S2)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("stale", [False, True])
def test_an_adopted_merge_always_resumes(
    kind: EntryKind | None, *, stale: bool
) -> None:
    assert (
        resume_disposition(kind, stale=stale, merge_adopted=True) is Disposition.RESUME
    )


@pytest.mark.parametrize("kind", [None, EntryKind.FRESH])
@pytest.mark.parametrize("stale", [False, True])
def test_no_entry_or_a_fresh_one_proceeds(
    kind: EntryKind | None, *, stale: bool
) -> None:
    assert (
        resume_disposition(kind, stale=stale, merge_adopted=False)
        is Disposition.PROCEED
    )


@pytest.mark.parametrize("stale", [False, True])
def test_s2_implementing_resumes(*, stale: bool) -> None:
    assert (
        resume_disposition(EntryKind.S2_IMPLEMENTING, stale=stale, merge_adopted=False)
        is Disposition.RESUME
    )


def test_past_s2_advances_only_when_not_stale() -> None:
    assert (
        resume_disposition(EntryKind.PAST_S2, stale=False, merge_adopted=False)
        is Disposition.ADVANCE
    )
    assert (
        resume_disposition(EntryKind.PAST_S2, stale=True, merge_adopted=False)
        is Disposition.RESUME
    )


class TestPlanReentry:
    def test_a_fresh_row_returns_none(self, repo: Path, tmp_path: Path) -> None:
        assert _plan(repo, tmp_path) is None

    def test_an_exempt_row_without_a_merge_returns_none(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _commit(repo, "a.py")
        assert _plan(repo, tmp_path, _task(review_feedback=_feedback())) is None

    def test_s2_not_stale_resumes(
        self, repo: Path, tmp_path: Path, staleness_script: Path
    ) -> None:
        _commit(repo, "a.py")
        plan = _reentry(_plan(repo, tmp_path))
        assert plan.disposition is Disposition.RESUME
        assert plan.merge is None
        assert plan.staleness is not None
        assert plan.staleness.stale is False
        head = git_in(repo, "rev-parse", "HEAD")
        assert plan.friction == (f"impl_resumed_from: {head}",)

    def test_s2_stale_by_marker_resumes_and_records_reasons(
        self, repo: Path, tmp_path: Path, staleness_script: Path
    ) -> None:
        _commit(repo, "a.py")
        _seed_marker(repo)
        plan = _reentry(_plan(repo, tmp_path))
        assert plan.disposition is Disposition.RESUME
        assert plan.staleness is not None
        assert plan.staleness.stale is True
        assert "impl_guard_stale: regressed_to_impl" in plan.friction

    def test_past_s2_with_nothing_stale_advances(
        self, repo: Path, tmp_path: Path, staleness_script: Path
    ) -> None:
        _commit(repo, "a.py", trailer=_TRAILER)
        plan = _reentry(_plan(repo, tmp_path))
        assert plan.disposition is Disposition.ADVANCE
        head = git_in(repo, "rev-parse", "HEAD")
        assert (
            f"impl_advanced_past_s2: HEAD {head[:12]} carries the impl-complete "
            "trailer; no turn ran"
        ) in plan.friction

    def test_past_s2_with_the_durable_regress_head_resumes(
        self, repo: Path, tmp_path: Path, staleness_script: Path
    ) -> None:
        _commit(repo, "a.py", trailer=_TRAILER)
        head = git_in(repo, "rev-parse", "HEAD")
        plan = _reentry(_plan(repo, tmp_path, _task(finalize_regress_branch_head=head)))
        assert plan.disposition is Disposition.RESUME
        assert plan.staleness is not None
        assert plan.staleness.reasons == ("regressed_to_impl",)

    def test_past_s2_regressed_with_no_script_installed_still_resumes(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _commit(repo, "a.py", trailer=_TRAILER)
        _seed_marker(repo)
        plan = _reentry(_plan(repo, tmp_path))
        assert plan.disposition is Disposition.RESUME
        assert plan.staleness is not None
        assert plan.staleness.stale is True
        assert plan.staleness.reasons == ("regressed_to_impl",)

    def test_past_s2_after_head_moved_off_the_durable_regress_head_advances(
        self, repo: Path, tmp_path: Path, staleness_script: Path
    ) -> None:
        old = git_in(repo, "rev-parse", "HEAD")
        _commit(repo, "a.py", trailer=_TRAILER)
        plan = _reentry(_plan(repo, tmp_path, _task(finalize_regress_branch_head=old)))
        assert plan.disposition is Disposition.ADVANCE

    def test_past_s2_with_an_operator_comment_after_head_resumes(
        self, repo: Path, tmp_path: Path, staleness_script: Path
    ) -> None:
        _commit(repo, "a.py", trailer=_TRAILER)
        newer = make_comment("c1", body="please also do X", created_at=_FAR_FUTURE)
        plan = _reentry(_plan(repo, tmp_path, comments=[newer]))
        assert plan.disposition is Disposition.RESUME
        assert plan.staleness is not None
        assert plan.staleness.operator_comments == (newer,)

    def test_past_s2_with_only_an_agent_authored_newer_comment_advances(
        self, repo: Path, tmp_path: Path, staleness_script: Path
    ) -> None:
        _commit(repo, "a.py", trailer=_TRAILER)
        newer = make_comment("c1", created_at=_FAR_FUTURE, agent_authored=True)
        plan = _reentry(_plan(repo, tmp_path, comments=[newer]))
        assert plan.disposition is Disposition.ADVANCE
        assert any("agent-authored" in line for line in plan.friction)

    def test_a_stale_script_marker_blocks(self, repo: Path, tmp_path: Path) -> None:
        _commit(repo, "a.py")
        script = repo / ".claude" / "scripts" / _STALENESS_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text("# cw-script-version: 1\n", encoding="utf-8")
        block = _plan(repo, tmp_path)
        assert isinstance(block, ImplBlock)
        assert block.details.startswith("Pre-Stage Detector Guard: HEADLESS BLOCK")

    def test_a_classify_block_is_returned(self, repo: Path, tmp_path: Path) -> None:
        facts = _facts(repo)
        facts["fork_point"] = ""
        block = plan_reentry(
            task=_task(),
            worktree=repo,
            base_branch="main",
            facts=facts,
            comments=[],
            scratch=tmp_path / "scratch",
        )
        assert isinstance(block, ImplBlock)
        assert "fork" in block.details

    def test_an_unmeasurable_merge_state_blocks(
        self, repo: Path, tmp_path: Path
    ) -> None:
        with patch(
            "cw.stage_verification.impl_resume.merge_in_progress", return_value=None
        ):
            block = _plan(repo, tmp_path)
        assert isinstance(block, ImplBlock)
        assert block.details.startswith("merge_in_progress:")


class TestUnconcludedMerge:
    """An arriving ``MERGE_HEAD`` is adopted only with a regress signal."""

    def _conflict(self, repo: Path, origin: Path, tmp_path: Path) -> None:
        diverge_origin_default(repo, origin, _BRANCH, tmp_path / "side")
        leave_conflicting_merge(repo)

    def test_a_merge_without_a_signal_blocks_for_a_fresh_row(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, origin = repo_origin
        push_commit_to_origin(origin, "main", tmp_path / "side", "other.py")
        git_in(repo, "fetch", "origin")
        git_in(repo, "merge", "--no-commit", "--no-ff", "origin/main")
        block = _plan(repo, tmp_path)
        assert isinstance(block, ImplBlock)
        assert block.details.startswith("merge_in_progress:")
        assert "no regress" in block.details

    def test_a_merge_without_a_signal_blocks_for_an_exempt_row(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, origin = repo_origin
        self._conflict(repo, origin, tmp_path)
        block = _plan(repo, tmp_path, _task(review_feedback=_feedback()))
        assert isinstance(block, ImplBlock)
        assert block.details.startswith("merge_in_progress:")

    def test_a_signalled_merge_on_a_fresh_row_is_adopted_and_resumes(
        self,
        repo_origin: tuple[Path, Path],
        tmp_path: Path,
        staleness_script: Path,
    ) -> None:
        repo, origin = repo_origin
        push_commit_to_origin(origin, "main", tmp_path / "side", "other.py")
        git_in(repo, "fetch", "origin")
        git_in(repo, "merge", "--no-commit", "--no-ff", "origin/main")
        _seed_marker(repo)
        plan = _reentry(_plan(repo, tmp_path))
        assert plan.disposition is Disposition.RESUME
        assert plan.entry is not None
        assert plan.entry.kind is EntryKind.FRESH
        assert plan.merge is not None
        assert any(
            line.startswith("impl_unconcluded_merge_adopted: ")
            for line in plan.friction
        )

    def test_a_signalled_merge_on_an_exempt_row_is_adopted_and_resumes(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, origin = repo_origin
        self._conflict(repo, origin, tmp_path)
        _seed_marker(repo)
        plan = _reentry(_plan(repo, tmp_path, _task(review_feedback=_feedback())))
        assert plan.disposition is Disposition.RESUME
        assert plan.entry is None
        assert plan.merge is not None
        assert plan.merge.outcome.conflicted == frozenset({"clash.py"})

    def test_a_signalled_merge_on_an_s2_row_resumes(
        self,
        repo_origin: tuple[Path, Path],
        tmp_path: Path,
        staleness_script: Path,
    ) -> None:
        repo, origin = repo_origin
        self._conflict(repo, origin, tmp_path)
        _seed_marker(repo)
        plan = _reentry(_plan(repo, tmp_path))
        assert plan.disposition is Disposition.RESUME
        assert plan.entry is not None
        assert plan.entry.kind is EntryKind.S2_IMPLEMENTING
        assert plan.merge is not None

    def test_a_signalled_merge_on_a_past_s2_row_resumes_never_advances(
        self,
        repo_origin: tuple[Path, Path],
        tmp_path: Path,
        staleness_script: Path,
    ) -> None:
        repo, origin = repo_origin
        diverge_origin_default(repo, origin, _BRANCH, tmp_path / "side")
        _commit(repo, "done.py", trailer=_TRAILER)
        leave_conflicting_merge(repo)
        head = git_in(repo, "rev-parse", "HEAD")
        plan = _reentry(_plan(repo, tmp_path, _task(finalize_regress_branch_head=head)))
        assert plan.entry is not None
        assert plan.entry.kind is EntryKind.PAST_S2
        assert plan.disposition is Disposition.RESUME
        assert plan.merge is not None

    def test_a_classify_block_wins_over_an_adopted_merge(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, origin = repo_origin
        self._conflict(repo, origin, tmp_path)
        _seed_marker(repo)
        facts = _facts(repo)
        facts["fork_point"] = ""
        block = plan_reentry(
            task=_task(),
            worktree=repo,
            base_branch="main",
            facts=facts,
            comments=[],
            scratch=tmp_path / "scratch",
        )
        assert isinstance(block, ImplBlock)
        assert "fork" in block.details

    def test_a_merge_whose_base_ref_is_missing_blocks(
        self, repo_origin: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, origin = repo_origin
        self._conflict(repo, origin, tmp_path)
        _seed_marker(repo)
        git_in(repo, "update-ref", "-d", "refs/remotes/origin/main")
        block = _plan(repo, tmp_path)
        assert isinstance(block, ImplBlock)
        assert block.details.startswith("merge_in_progress:")
