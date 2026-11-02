"""Cycle-0 coverage gap carried to the codex fix loop's clean exit (#2880).

A cycle 0 that left part of the diff unread (a failed reviewer role, or an
unreviewed shard) and then blocked enters the fix loop, whose re-reviews cover
only the delta since the last cycle. A clean delta must not stand in for the
unread part: the loop parks as ``codex_review_partial`` instead of returning a
clean ``stage_complete``. Every loop test drives the real loop and the real
review pass over a real git repo with a scripted codex runner; none makes a
live codex call.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

import pytest

from cw.claude_review import ClaudeRunResult
from cw.codex_background import _ConfirmLoop
from cw.codex_fix_loop import run_review_with_fix_loop
from cw.codex_fix_loop.coverage import NO_DELTA_GAP, DeltaGap
from cw.codex_fix_loop.park import _refuse_unread_coverage
from cw.codex_review import (
    CODEX_MUST_FIX_MECHANICALLY_REJECTED,
    CODEX_REVIEW_PARTIAL,
    _prepare_review_pass,
    make_codex_blocked,
    synthesize_codex_review_result,
)
from cw.codex_runner import CodexRunResult, FakeCodexRunner
from cw.models import ClientConfig
from cw.models.client import ReviewConfig
from cw.review_convergence import UnreadFindings, unread_from_verdict
from cw.review_findings import ReviewerRunRecord, ShardRecord
from tests._claude_review_helpers import RoleAwareClaudeRunner, review_envelope
from tests._codex_review_helpers import (
    _FAILED_RUN,
    _FIX_NOOP,
    _doc_json,
    _large_diff_repo,
    _loop_verdict,
    _must_fix_in,
    _ok_result,
    _SequencedRunner,
    _sharded_setup,
    _stem_content,
    _task,
)
from tests._review_convergence_helpers import warm_codex_capability
from tests.conftest import (
    _make_diff,
    _make_reviewer_doc,
    commit_tracked_file,
    git_in,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.review_findings import ReviewVerdict

_PARK_LOGGER = "cw.codex_fix_loop.park"
_GAP_WARNING = "codex_cycle0_coverage_gap"
_DELTA_GAP_WARNING = "codex_delta_coverage_gap"
_UNREAD_TEXT = "left part of the diff unread"
_FIRST_FILE = "pkg_a/a1.py"


def _failed_doc(role: str) -> CodexRunResult:
    return CodexRunResult(
        returncode=0,
        stdout="",
        stderr="",
        output_file_content=_doc_json(role=role, status="failed"),
    )


def _roles(repo: Path, session_id: str) -> list[str]:
    prepared = _prepare_review_pass(
        _task(), repo, "main", runner=FakeCodexRunner(), session_id=session_id
    )
    return prepared.roles


def _cycle_zero_roles(
    roles: list[str], *, failed: CodexRunResult | None
) -> list[CodexRunResult]:
    """One shard's cycle-0 results: role 0 raises a MUST_FIX, role 1 is *failed*."""
    results = [_ok_result(role) for role in roles]
    results[0] = _ok_result(roles[0], findings=_must_fix_in(_FIRST_FILE))
    if failed is not None:
        results[1] = failed
    return results


def _clean_rereview(roles: list[str]) -> list[CodexRunResult]:
    return [_ok_result(role) for role in roles]


def _loop_kwargs(repo: Path, runner: object, session_id: str) -> dict[str, object]:
    return {
        "runner": runner,
        "task": _task(),
        "worktree": repo,
        "default_branch": "main",
        "model": None,
        "reasoning_effort": None,
        "session_id": session_id,
        "fix_loop_enabled": True,
    }


def _run_loop(
    repo: Path,
    runner: _SequencedRunner,
    session_id: str,
    *,
    max_prompt_chars: int | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    return run_review_with_fix_loop(
        runner=runner,
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id=session_id,
        fix_loop_enabled=True,
        max_prompt_chars=max_prompt_chars,
    )


def _without_timings(value: object) -> object:
    """Drop wall-clock ``duration_seconds``, the only field two runs differ in."""
    if isinstance(value, dict):
        return {
            key: _without_timings(item)
            for key, item in value.items()
            if key != "duration_seconds"
        }
    if isinstance(value, list):
        return [_without_timings(item) for item in value]
    return value


def _assert_partial_park(result: AutoDevResult, verdict: ReviewVerdict | None) -> str:
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_PARTIAL
    assert not result.blocker.retry_eligible
    assert verdict is None
    assert _UNREAD_TEXT in result.blocker.details
    return result.blocker.details


class TestUnshardedFailedRoleCycleZero:
    def test_failed_role_then_clean_rereview_parks_as_partial(
        self,
        make_git_repo: Callable[[str], Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-gap-failed")
        roles = _roles(repo, "s-gap-failed")
        assert len(roles) > 1
        runner = _SequencedRunner(
            [
                *_cycle_zero_roles(roles, failed=_FAILED_RUN),
                _FIX_NOOP,
                *_clean_rereview(roles),
            ]
        )

        with caplog.at_level(logging.WARNING, logger=_PARK_LOGGER):
            result, verdict = _run_loop(repo, runner, "s-gap-failed")

        details = _assert_partial_park(result, verdict)
        assert f"failed reviewer roles: {roles[1]}" in details
        assert "unreviewed shards: 0" in details
        assert result.review is not None
        assert result.review.fix_cycles_used == 1
        (warning,) = [r for r in caplog.records if _GAP_WARNING in r.getMessage()]
        assert warning.levelno == logging.WARNING
        assert "ticket=T-1" in warning.getMessage()
        assert "cycle=1" in warning.getMessage()
        assert roles[1] in warning.getMessage()

    def test_failed_status_document_then_clean_rereview_parks_as_partial(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-gap-doc")
        roles = _roles(repo, "s-gap-doc")
        runner = _SequencedRunner(
            [
                *_cycle_zero_roles(roles, failed=_failed_doc(roles[1])),
                _FIX_NOOP,
                *_clean_rereview(roles),
            ]
        )

        result, verdict = _run_loop(repo, runner, "s-gap-doc")

        details = _assert_partial_park(result, verdict)
        assert f"failed reviewer roles: {roles[1]}" in details
        assert result.review is not None
        assert result.review.fix_cycles_used == 1


class TestShardedUnreviewedShardCycleZero:
    def test_unreviewed_shard_then_clean_rereview_parks_as_partial(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-gap-shard")
        setup = _sharded_setup(repo, "s-gap-shard")
        roles = setup.prepared.roles
        cycle_zero: list[CodexRunResult] = []
        for index in range(len(setup.plans)):
            shard = [_ok_result(role) for role in roles]
            if index == 0:
                shard[0] = _ok_result(
                    roles[0], findings=_must_fix_in(setup.plans[0].files[0])
                )
            if index == 1:
                shard[0] = _FAILED_RUN
            cycle_zero.extend(shard)
        runner = _SequencedRunner([*cycle_zero, _FIX_NOOP, *_clean_rereview(roles)])

        result, verdict = _run_loop(
            repo, runner, "s-gap-shard", max_prompt_chars=setup.limit
        )

        details = _assert_partial_park(result, verdict)
        assert "unreviewed shards: 1" in details
        assert f"failed reviewer roles: {roles[0]}" in details
        assert result.review is not None
        assert result.review.fix_cycles_used == 1


class TestCycleZeroGapCarriedAcrossCycles:
    def test_gap_survives_a_dirty_intermediate_cycle(
        self,
        make_git_repo: Callable[[str], Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-gap-carry")
        roles = _roles(repo, "s-gap-carry")
        # A delta re-review after a no-op fix runs only the first two roles, so
        # each re-review is scripted with exactly two results to keep the
        # positional script aligned across both cycles.
        rereview_roles = roles[:2]
        dirty_rereview = _clean_rereview(rereview_roles)
        dirty_rereview[0] = _ok_result(
            rereview_roles[0], findings=_must_fix_in(_FIRST_FILE)
        )
        runner = _SequencedRunner(
            [
                *_cycle_zero_roles(roles, failed=_FAILED_RUN),
                _FIX_NOOP,
                *dirty_rereview,
                _FIX_NOOP,
                *_clean_rereview(rereview_roles),
            ]
        )

        with caplog.at_level(logging.WARNING, logger=_PARK_LOGGER):
            result, verdict = _run_loop(repo, runner, "s-gap-carry")

        details = _assert_partial_park(result, verdict)
        assert f"failed reviewer roles: {roles[1]}" in details
        assert result.review is not None
        assert result.review.fix_cycles_used == 2
        (warning,) = [r for r in caplog.records if _GAP_WARNING in r.getMessage()]
        assert "cycle=2" in warning.getMessage()


def _review_client(repo: Path, config: dict[str, object]) -> ClientConfig:
    return ClientConfig(
        name="test",
        workspace_path=repo,
        default_branch="main",
        review=ReviewConfig.model_validate(config),
    )


_MODES = {
    "blocking": {},
    "blocking-no-confirm": {"confirm_backend": "none"},
    "clean-default-confirm": {"convergence": "clean"},
    "clean-no-confirm": {"convergence": "clean", "confirm_backend": "none"},
    "clean-codex-confirm": {"convergence": "clean", "confirm_backend": "codex"},
}


class TestModesPinned:
    @pytest.mark.parametrize("config", list(_MODES.values()), ids=list(_MODES))
    def test_gap_parks_in_every_mode_without_a_confirm_round(
        self,
        make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
        monkeypatch: pytest.MonkeyPatch,
        config: dict[str, object],
    ) -> None:
        repo, _origin = make_git_repo_with_origin("wt-gap-modes")
        for relpath in ("pkg_a/a1.py", "pkg_a/a2.py"):
            commit_tracked_file(repo, relpath, _stem_content(relpath))
        git_in(repo, "push", "origin", "feature")
        # The clean-mode ledger lives in the worktree under .cw/; a real
        # worktree ignores it, and an untracked ledger would park the fix
        # cycle on a dirty start before the refusal under test is reached.
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text(".cw/\n", encoding="utf-8")
        warm_codex_capability(repo, _task())
        roles = _roles(repo, "s-gap-modes")
        claude = RoleAwareClaudeRunner(
            lambda role: ClaudeRunResult(0, review_envelope(_doc_json(role=role)), "")
        )
        monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: "/x/claude")
        monkeypatch.setattr("cw.codex_background.RealClaudeRunner", lambda: claude)
        runner = _SequencedRunner(
            [
                *_cycle_zero_roles(roles, failed=_FAILED_RUN),
                _FIX_NOOP,
                *_clean_rereview(roles),
            ]
        )
        loop = partial(
            run_review_with_fix_loop,
            **_loop_kwargs(repo, runner, "s-gap-modes"),
        )
        client = _review_client(repo, config)
        run = _ConfirmLoop(
            task=_task(),
            worktree=repo,
            client=client,
            sid="s-gap-modes",
            deadline=None,
            fix_loop_enabled=True,
            loop=loop,
        )

        outcome = run.iterate(seeds=(), budget=None)

        assert outcome.result.status == "blocked"
        assert outcome.result.blocker is not None
        assert outcome.result.blocker.reason == CODEX_REVIEW_PARTIAL, (
            outcome.result.blocker.details
        )
        assert outcome.result.review is not None
        assert outcome.result.review.converged is not True
        assert claude.roles == []


class TestNoGapIsByteIdentical:
    def test_all_roles_ok_then_clean_rereview_is_unchanged(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-gap-none")
        roles = _roles(repo, "s-gap-none")

        def _script() -> _SequencedRunner:
            return _SequencedRunner(
                [
                    *_cycle_zero_roles(roles, failed=None),
                    _FIX_NOOP,
                    *_clean_rereview(roles),
                ]
            )

        # Baseline first: the same run with the refusal replaced by an identity.
        with monkeypatch.context() as patch:
            patch.setattr(
                "cw.codex_fix_loop._driver._refuse_unread_coverage",
                lambda exit_result, *_args, **_kwargs: exit_result,
            )
            baseline, baseline_verdict = _run_loop(repo, _script(), "s-gap-none")
        assert baseline_verdict is not None

        result, verdict = _run_loop(repo, _script(), "s-gap-none")

        assert result.status == "stage_complete"
        assert result.blocker is None
        assert verdict is not None
        assert result.review is not None
        assert result.review.fix_cycles_used == 1
        assert _without_timings(result.model_dump()) == _without_timings(
            baseline.model_dump()
        )
        assert _without_timings(verdict.model_dump()) == _without_timings(
            baseline_verdict.model_dump()
        )

    def test_refuse_helper_returns_input_unchanged_without_gap(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-gap-helper-none")
        exit_result = _clean_pair(repo)

        returned = _refuse_unread_coverage(
            exit_result, UnreadFindings(), task=_task(), worktree=repo, cycle=1
        )

        assert returned is exit_result

    def test_rejected_only_unread_is_not_a_coverage_gap(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-gap-helper-rejected")
        exit_result = _clean_pair(repo)

        returned = _refuse_unread_coverage(
            exit_result,
            UnreadFindings(rejected_must_fix=2, discarded_run_failures=1),
            task=_task(),
            worktree=repo,
            cycle=1,
        )

        assert returned is exit_result

    def test_already_blocked_clean_exit_keeps_its_blocker(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-gap-helper-blocked")
        _clean, verdict = _clean_pair(repo)
        blocked = make_codex_blocked(
            ticket_id="T-1",
            worktree=repo,
            reason=CODEX_MUST_FIX_MECHANICALLY_REJECTED,
        )
        exit_result = (blocked, verdict)

        returned = _refuse_unread_coverage(
            exit_result,
            UnreadFindings(failed_roles=("Code Quality Reviewer",)),
            task=_task(),
            worktree=repo,
            cycle=1,
        )

        assert returned is exit_result
        assert returned[0].blocker is not None
        assert returned[0].blocker.reason == CODEX_MUST_FIX_MECHANICALLY_REJECTED


class TestRefuseHelperDetails:
    def test_gap_replaces_a_clean_result_and_escalates_late_cycles(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-gap-helper-refuse")
        clean, verdict = _clean_pair(repo)

        refused, refused_verdict = _refuse_unread_coverage(
            (clean, verdict),
            UnreadFindings(unreviewed_shards=2),
            task=_task(),
            worktree=repo,
            cycle=3,
        )

        assert refused_verdict is None
        assert refused.blocker is not None
        assert refused.blocker.reason == CODEX_REVIEW_PARTIAL
        assert refused.blocker.retry_eligible is None
        assert "failed reviewer roles: none" in refused.blocker.details
        assert "unreviewed shards: 2" in refused.blocker.details
        assert refused.review == clean.review
        assert refused.friction_highlights == clean.friction_highlights
        assert refused.health.fix_loop_escalated is True


def _shard(index: int, *, reviewed: bool) -> ShardRecord:
    return ShardRecord(
        index=index, files=[f"pkg/f{index}.py"], prompt_chars=10, reviewed=reviewed
    )


def _run_record(role: str, status: str) -> ReviewerRunRecord:
    return ReviewerRunRecord.model_validate(
        {"reviewer_role": role, "status": status, "finding_count": 0}
    )


class TestDeltaGapFold:
    """``DeltaGap.fold`` folds only a sharded re-review's coverage gap (#2877)."""

    def test_unsharded_verdict_with_a_failed_role_is_not_folded(self) -> None:
        verdict = _loop_verdict(agents_run=[_run_record("Role A", "failed")])
        assert unread_from_verdict(verdict).coverage_gap

        assert NO_DELTA_GAP.fold(verdict, 1) is NO_DELTA_GAP

    def test_fully_reviewed_sharded_verdict_is_not_folded(self) -> None:
        verdict = _loop_verdict(
            shards=[_shard(1, reviewed=True), _shard(2, reviewed=True)],
            agents_run=[_run_record("Role A", "ok")],
        )
        gap = DeltaGap()

        assert gap.fold(verdict, 1) is gap
        assert not gap.coverage_gap

    def test_sharded_gap_is_folded_with_its_cycle(self) -> None:
        verdict = _loop_verdict(
            shards=[_shard(1, reviewed=True), _shard(2, reviewed=False)],
            agents_run=[_run_record("Role A", "ok"), _run_record("Role A", "failed")],
        )

        gap = NO_DELTA_GAP.fold(verdict, 1)

        assert gap.coverage_gap
        assert gap.cycles == (1,)
        assert gap.unread == UnreadFindings(
            failed_roles=("Role A",), unreviewed_shards=1
        )

    def test_a_second_fold_merges_roles_shards_and_cycles(self) -> None:
        first = _loop_verdict(
            shards=[_shard(1, reviewed=False), _shard(2, reviewed=True)],
            agents_run=[_run_record("Role B", "failed")],
        )
        second = _loop_verdict(
            shards=[_shard(1, reviewed=False), _shard(2, reviewed=False)],
            agents_run=[
                _run_record("Role A", "failed"),
                _run_record("Role B", "failed"),
            ],
        )

        gap = NO_DELTA_GAP.fold(first, 1).fold(second, 3)

        assert gap.cycles == (1, 3)
        assert gap.unread.failed_roles == ("Role B", "Role A")
        assert gap.unread.unreviewed_shards == 3
        assert gap.unread.rejected_must_fix == 0
        assert gap.unread.discarded_run_failures == 0


_CYCLE0_TEXT = (
    "codex review parked: cycle 0 left part of the diff unread "
    "(failed reviewer roles: Role A; unreviewed shards: 0). The fix loop's "
    "re-review covered only the delta since the last cycle and cannot stand in "
    "for it, so a clean re-review is not a clean review. Requeue REVIEW to "
    "re-review the whole diff."
)
_DELTA_GAP = DeltaGap(
    UnreadFindings(failed_roles=("Role B",), unreviewed_shards=2), (1, 3)
)


class TestRefuseHelperDeltaGap:
    """The refusal also covers a fix-loop re-review delta's gap (#2877)."""

    def test_delta_gap_only(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo("wt-gap-helper-delta")
        clean, verdict = _clean_pair(repo)

        with caplog.at_level(logging.WARNING, logger=_PARK_LOGGER):
            refused, refused_verdict = _refuse_unread_coverage(
                (clean, verdict),
                UnreadFindings(),
                task=_task(),
                worktree=repo,
                cycle=4,
                delta_gap=_DELTA_GAP,
            )

        assert refused_verdict is None
        assert refused.blocker is not None
        assert refused.blocker.reason == CODEX_REVIEW_PARTIAL
        assert refused.blocker.retry_eligible is None
        assert refused.blocker.details == (
            "codex review parked: the fix loop's re-review of cycle(s) 1, 3 left "
            "part of its delta unread (failed reviewer roles: Role B; unreviewed "
            "shards: 2). A later cycle's re-review covers only the changes made "
            "after it and cannot stand in for it, so a clean re-review is not a "
            "clean review. Requeue REVIEW to re-review the whole diff."
        )
        assert refused.review == clean.review
        assert refused.health.fix_loop_escalated is True
        messages = [r.getMessage() for r in caplog.records]
        assert not [m for m in messages if _GAP_WARNING in m]
        (warning,) = [m for m in messages if _DELTA_GAP_WARNING in m]
        assert warning == (
            "codex_delta_coverage_gap ticket=T-1 cycle=4 delta_cycles=1,3 "
            "failed_roles=Role B unreviewed_shards=2"
        )

    def test_both_gaps_put_the_cycle_zero_text_first(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo("wt-gap-helper-both")
        exit_result = _clean_pair(repo)

        with caplog.at_level(logging.WARNING, logger=_PARK_LOGGER):
            refused, _ = _refuse_unread_coverage(
                exit_result,
                UnreadFindings(failed_roles=("Role A",)),
                task=_task(),
                worktree=repo,
                cycle=4,
                delta_gap=_DELTA_GAP,
            )

        assert refused.blocker is not None
        assert refused.blocker.details == (
            f"{_CYCLE0_TEXT} Also, the re-review of cycle(s) 1, 3 left part of "
            "its delta unread (failed reviewer roles: Role B; unreviewed "
            "shards: 2)."
        )
        messages = [r.getMessage() for r in caplog.records]
        assert len([m for m in messages if _GAP_WARNING in m]) == 1
        assert len([m for m in messages if _DELTA_GAP_WARNING in m]) == 1

    def test_without_a_delta_gap_the_cycle_zero_text_is_unchanged(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-gap-helper-cycle0")
        exit_result = _clean_pair(repo)

        refused, _ = _refuse_unread_coverage(
            exit_result,
            UnreadFindings(failed_roles=("Role A",)),
            task=_task(),
            worktree=repo,
            cycle=1,
        )

        assert refused.blocker is not None
        assert refused.blocker.details == _CYCLE0_TEXT

    def test_already_blocked_exit_ignores_a_delta_gap(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-gap-helper-delta-blocked")
        _clean, verdict = _clean_pair(repo)
        blocked = make_codex_blocked(
            ticket_id="T-1",
            worktree=repo,
            reason=CODEX_MUST_FIX_MECHANICALLY_REJECTED,
        )
        exit_result = (blocked, verdict)

        returned = _refuse_unread_coverage(
            exit_result,
            UnreadFindings(),
            task=_task(),
            worktree=repo,
            cycle=2,
            delta_gap=_DELTA_GAP,
        )

        assert returned is exit_result


def _clean_pair(repo: Path) -> tuple[AutoDevResult, ReviewVerdict]:
    result, verdict = synthesize_codex_review_result(
        task=_task(),
        worktree=repo,
        documents=[_make_reviewer_doc(reviewer_role="Code Quality Reviewer")],
        failures=[],
        diff=_make_diff("x = 1", files={"feature.py": [1]}),
        reviewed_sha=git_in(repo, "rev-parse", "HEAD"),
        session_id="s-gap-helper",
        default_branch="main",
        fix_loop_enabled=True,
    )
    assert result.blocker is None
    assert verdict is not None
    return result, verdict
