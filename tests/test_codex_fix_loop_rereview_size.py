"""Codex fix-loop re-review delta prompt-size handling (#2877).

A fix cycle's re-review embeds only the delta since the previous reviewed head,
but a cycle can still add enough code to push that delta over the lane's
``max_prompt_chars``. The re-review now shards an over-limit delta with the
same planner cycle 0 uses (#2864), parks a delta the planner cannot shard as a
named, non-retryable ``agent_block`` before any codex call for that cycle, and
carries a sharded delta's coverage gap (a failed role or an unreviewed shard)
to the clean exit, which then parks as ``codex_review_partial``. Every test
drives the real loop and the real review pass over a real git repo (with a
bare origin, since every real fix commit is pushed, #2354) and a scripted
codex runner; none makes a live codex call.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from cw.claude_review import ClaudeRunResult
from cw.codex_background import _ConfirmLoop
from cw.codex_fix_loop import _driver, run_review_with_fix_loop
from cw.codex_fix_loop.snapshot import _verdict_snapshot_filename
from cw.codex_review import CODEX_REVIEW_PARTIAL, _prepare_review_pass
from cw.codex_review._shard import plan_prompt_shards
from cw.codex_runner import FakeCodexRunner
from cw.models import ClientConfig
from cw.models.client import ReviewConfig
from tests._claude_review_helpers import RoleAwareClaudeRunner, review_envelope
from tests._codex_review_helpers import (
    _FAILED_RUN,
    _all_ok,
    _doc_json,
    _EditingRunner,
    _large_diff_repo,
    _must_fix_in,
    _ok_result,
    _prompt_files,
    _stem_content,
    _task,
)
from tests.conftest import add_bare_origin, commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.codex_review import _ReviewPassInputs
    from cw.codex_runner import CodexRunResult
    from cw.review_findings import ReviewVerdict

_DRIVER_LOGGER = "cw.codex_fix_loop._driver"
_PARK_LOGGER = "cw.codex_fix_loop.park"
_DELTA_WARNING = "codex_delta_coverage_gap"
_CYCLE0_WARNING = "codex_cycle0_coverage_gap"
_DELTA_UNREAD = "left part of its delta unread"
_RESTART_CLAUSE = (
    "fix commits through cycle 0 are already pushed and stay on the branch, "
    "and a requeue restarts at cycle 0 over the whole diff, which is "
    "re-planned from scratch"
)
_PRIOR_FINDINGS = "## Unresolved Prior Findings"
_LINES = 100
_CYCLE0_FILE = "pkg_a/a1.py"
_DELTA_FILES = (
    "pkg_d/d1.py",
    "pkg_d/d2.py",
    "pkg_e/e1.py",
    "pkg_e/e2.py",
    "pkg_f/f1.py",
    "pkg_f/f2.py",
    "pkg_g/g1.py",
    "pkg_g/g2.py",
)
_MORE_DELTA_FILES = (
    "pkg_h/h1.py",
    "pkg_h/h2.py",
    "pkg_i/i1.py",
    "pkg_i/i2.py",
    "pkg_j/j1.py",
    "pkg_j/j2.py",
    "pkg_k/k1.py",
    "pkg_k/k2.py",
)
_SMALL_FILE = "pkg_d/small.py"
_HUGE_FILE = "huge/blob.py"
_FILLER_SHARDS = 12


def _add_files(paths: Sequence[str], lines: int = _LINES) -> Callable[[Path], None]:
    """A fix-cycle edit that writes *paths* (``_stem_content`` of *lines*)."""

    def _fix(worktree: Path) -> None:
        for relpath in paths:
            target = worktree / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_stem_content(relpath, lines), encoding="utf-8")

    return _fix


class _Lane(NamedTuple):
    """A pushed feature-branch repo whose cycle-0 prompt is exactly the limit."""

    repo: Path
    roles: list[str]
    limit: int
    cycle0_sha: str


def _lane(make_git_repo: Callable[[str], Path], name: str) -> _Lane:
    """A four-file, 100-line repo; the limit is its longest cycle-0 prompt.

    The planner returns no shards when the longest prompt is within the limit,
    so cycle 0 runs unsharded. This also warms the capability cache.
    """
    repo = _large_diff_repo(make_git_repo, name, lines=_LINES)
    add_bare_origin(repo)
    prepared = _prepare_review_pass(
        _task(), repo, "main", runner=FakeCodexRunner(), session_id=name
    )
    limit = max(len(p) for p in prepared.prompts_by_role.values())
    return _Lane(repo, prepared.roles, limit, prepared.reviewed_sha)


class _DeltaSpy:
    """Records every re-review's prepared inputs (calls with ``delta_from_sha``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.prepared: list[_ReviewPassInputs] = []
        real = _driver._prepare_review_pass

        def _spy(*args: Any, **kwargs: Any) -> _ReviewPassInputs:
            prepared = real(*args, **kwargs)
            if kwargs.get("delta_from_sha") is not None:
                self.prepared.append(prepared)
            return prepared

        monkeypatch.setattr(_driver, "_prepare_review_pass", _spy)


def _cycle_zero(roles: list[str], *, fail_second: bool = False) -> list[CodexRunResult]:
    """Cycle 0 (unsharded): role 0 raises a MUST_FIX, others ok or role 1 failed."""
    results = [_ok_result(role) for role in roles]
    results[0] = _ok_result(roles[0], findings=_must_fix_in(_CYCLE0_FILE))
    if fail_second:
        results[1] = _FAILED_RUN
    return results


def _gapped_delta(roles: list[str]) -> list[CodexRunResult]:
    """A sharded cycle 1: shard 0 role 0 a MUST_FIX, shard 1 role 0 failed.

    ``pkg_d/d1.py`` is the first sorted delta file, so it is always in shard 0.
    Shard-major, role-minor, padded with ok fillers for the later cycles.
    """
    results = _all_ok(roles, _FILLER_SHARDS)
    results[0] = _ok_result(roles[0], findings=_must_fix_in(_DELTA_FILES[0]))
    results[len(roles)] = _FAILED_RUN
    return results


def _run(
    lane: _Lane, runner: _EditingRunner, *, limit: int | None
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    return run_review_with_fix_loop(
        runner=runner,
        task=_task(),
        worktree=lane.repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=None,
        session_id=f"s-{lane.repo.name}",
        fix_loop_enabled=True,
        max_prompt_chars=limit,
        stall_cycles=10,
    )


def _assert_sharded_delta(
    lane: _Lane, prepared: _ReviewPassInputs, files: Sequence[str]
) -> int:
    """Precondition: *prepared* is the delta of *files* and is over the limit."""
    assert prepared.roles == lane.roles
    assert set(prepared.changed_files) == set(files)
    assert max(len(p) for p in prepared.prompts_by_role.values()) > lane.limit
    shards = len(plan_prompt_shards(prepared, lane.limit))
    assert shards >= 2
    return shards


def _partial_details(result: AutoDevResult, verdict: ReviewVerdict | None) -> str:
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_REVIEW_PARTIAL
    assert not result.blocker.retry_eligible
    assert verdict is None
    return result.blocker.details


class TestShardedRereviewDelta:
    def test_oversize_delta_reviews_every_delta_file_within_the_limit(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lane = _lane(make_git_repo, "wt-rr-shard")
        spy = _DeltaSpy(monkeypatch)
        runner = _EditingRunner(
            [*_cycle_zero(lane.roles), *_all_ok(lane.roles, _FILLER_SHARDS)],
            [_add_files(_DELTA_FILES)],
        )

        result, verdict = _run(lane, runner, limit=lane.limit)

        (delta,) = spy.prepared
        shards = _assert_sharded_delta(lane, delta, _DELTA_FILES)
        rereview = runner.review_calls[len(lane.roles) :]
        assert len(rereview) == len(lane.roles) * shards
        prompts = [str(call["stdin"]) for call in rereview]
        assert all(len(prompt) <= lane.limit for prompt in prompts)
        assert all(_PRIOR_FINDINGS in prompt for prompt in prompts)
        seen = {path for prompt in prompts for path in _prompt_files(prompt)}
        assert seen == set(_DELTA_FILES)
        assert result.blocker is None
        assert result.status == "stage_complete"
        assert verdict is not None
        assert len(verdict.shards) == shards
        assert all(shard.reviewed for shard in verdict.shards)
        assert verdict.previous_reviewed_sha == lane.cycle0_sha

    def test_fully_reviewed_sharded_delta_with_a_blocker_then_clean_exits_clean(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lane = _lane(make_git_repo, "wt-rr-shard-dirty")
        spy = _DeltaSpy(monkeypatch)
        cycle1 = _all_ok(lane.roles, _FILLER_SHARDS)
        cycle1[0] = _ok_result(lane.roles[0], findings=_must_fix_in(_DELTA_FILES[0]))
        runner = _EditingRunner(
            [*_cycle_zero(lane.roles), *cycle1],
            [_add_files(_DELTA_FILES), _add_files([_SMALL_FILE], 5)],
        )

        result, verdict = _run(lane, runner, limit=lane.limit)

        first, second = spy.prepared
        _assert_sharded_delta(lane, first, _DELTA_FILES)
        assert second.roles == lane.roles
        assert result.blocker is None, result.blocker
        assert result.status == "stage_complete"
        assert verdict is not None
        assert result.review is not None
        assert result.review.fix_cycles_used == 2


class TestUnderLimitDelta:
    @pytest.mark.parametrize("use_limit", [True, False], ids=["limit", "no-limit"])
    def test_under_limit_delta_takes_the_unsharded_path_byte_for_byte(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
        use_limit: bool,
    ) -> None:
        lane = _lane(make_git_repo, "wt-rr-fits")
        spy = _DeltaSpy(monkeypatch)

        def _never(**_kwargs: object) -> object:
            pytest.fail("an under-limit delta must not be sharded")

        monkeypatch.setattr("cw.codex_review.core.run_sharded_roles", _never)
        runner = _EditingRunner(
            [*_cycle_zero(lane.roles), *_all_ok(lane.roles, 1)],
            [_add_files([_SMALL_FILE], 5)],
        )

        result, verdict = _run(lane, runner, limit=lane.limit if use_limit else None)

        (delta,) = spy.prepared
        rereview = runner.review_calls[len(lane.roles) :]
        assert [call["stdin"] for call in rereview] == [
            delta.prompts_by_role[role] for role in delta.roles
        ]
        assert result.blocker is None
        assert verdict is not None
        assert verdict.shards == []
        assert verdict.previous_reviewed_sha == lane.cycle0_sha


class TestUnshardableDelta:
    def test_delta_file_too_large_parks_as_agent_block_without_a_codex_call(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        lane = _lane(make_git_repo, "wt-rr-huge")
        runner = _EditingRunner(
            _cycle_zero(lane.roles), [_add_files([_HUGE_FILE], 1000)]
        )

        with caplog.at_level(logging.WARNING, logger=_DRIVER_LOGGER):
            result, verdict = _run(lane, runner, limit=lane.limit)

        assert verdict is None
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == "agent_block"
        assert result.blocker.retry_eligible is False
        details = result.blocker.details
        for fragment in (
            "file_too_large",
            _HUGE_FILE,
            "actual_chars=",
            "max_chars=",
            "cycle 1",
            _RESTART_CLAUSE,
        ):
            assert fragment in details, details
        assert len(runner.review_calls) == len(lane.roles)
        assert runner.fix_calls == 1
        assert any(
            "[diagnostics:" in line and _verdict_snapshot_filename(0) in line
            for line in result.friction_highlights
        ), result.friction_highlights
        (warning,) = [
            r
            for r in caplog.records
            if r.name == _DRIVER_LOGGER and "cycle 1 re-review" in r.getMessage()
        ]
        assert warning.levelno == logging.WARNING
        assert "ticket=T-1" in warning.getMessage()
        assert "cause=file_too_large" in warning.getMessage()

    def test_delta_prompt_overhead_over_the_limit_parks_input_too_large(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = make_git_repo("wt-rr-overhead")
        git_in(repo, "checkout", "-b", "feature")
        commit_tracked_file(repo, "tiny/t1.py", _stem_content("tiny/t1.py", 3))
        add_bare_origin(repo)
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-rr-over"
        )
        lane = _Lane(
            repo,
            prepared.roles,
            max(len(p) for p in prepared.prompts_by_role.values()),
            prepared.reviewed_sha,
        )
        cycle_zero = [_ok_result(role) for role in lane.roles]
        cycle_zero[0] = _ok_result(lane.roles[0], findings=_must_fix_in("tiny/t1.py"))
        runner = _EditingRunner(cycle_zero, [_add_files(["tiny/t2.py"], 3)])

        result, verdict = _run(lane, runner, limit=lane.limit)

        assert verdict is None
        assert result.blocker is not None
        assert result.blocker.reason == "agent_block"
        assert result.blocker.retry_eligible is False
        assert "input_too_large" in result.blocker.details
        assert "file_too_large" not in result.blocker.details
        assert "cycle 1" in result.blocker.details
        assert _RESTART_CLAUSE in result.blocker.details
        assert len(runner.review_calls) == len(lane.roles)


class TestDeltaGapCarried:
    def test_unreviewed_delta_shard_with_a_blocker_then_clean_cycle_parks_as_partial(
        self,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lane = _lane(make_git_repo, "wt-rr-gap")
        spy = _DeltaSpy(monkeypatch)
        runner = _EditingRunner(
            [*_cycle_zero(lane.roles), *_gapped_delta(lane.roles)],
            [_add_files(_DELTA_FILES), _add_files([_SMALL_FILE], 5)],
        )

        with caplog.at_level(logging.WARNING, logger=_PARK_LOGGER):
            result, verdict = _run(lane, runner, limit=lane.limit)

        first, second = spy.prepared
        _assert_sharded_delta(lane, first, _DELTA_FILES)
        assert second.roles == lane.roles
        details = _partial_details(result, verdict)
        assert _DELTA_UNREAD in details
        assert "cycle(s) 1 " in details
        assert "unreviewed shards: 1" in details
        assert f"failed reviewer roles: {lane.roles[0]}" in details
        assert "cycle 0 left part" not in details
        assert result.review is not None
        assert result.review.fix_cycles_used == 2
        messages = [r.getMessage() for r in caplog.records]
        (warning,) = [m for m in messages if _DELTA_WARNING in m]
        assert "cycle=2" in warning
        assert "delta_cycles=1 " in warning
        assert not [m for m in messages if _CYCLE0_WARNING in m]

    def test_cycle_zero_gap_and_delta_gap_are_both_reported(
        self,
        make_git_repo: Callable[[str], Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        lane = _lane(make_git_repo, "wt-rr-both")
        assert len(lane.roles) > 1
        runner = _EditingRunner(
            [
                *_cycle_zero(lane.roles, fail_second=True),
                *_gapped_delta(lane.roles),
            ],
            [_add_files(_DELTA_FILES), _add_files([_SMALL_FILE], 5)],
        )

        with caplog.at_level(logging.WARNING, logger=_PARK_LOGGER):
            result, verdict = _run(lane, runner, limit=lane.limit)

        details = _partial_details(result, verdict)
        assert "cycle 0 left part of the diff unread" in details
        assert _DELTA_UNREAD in details
        assert details.index("cycle 0 left part") < details.index(_DELTA_UNREAD)
        messages = [r.getMessage() for r in caplog.records]
        assert len([m for m in messages if _CYCLE0_WARNING in m]) == 1
        assert len([m for m in messages if _DELTA_WARNING in m]) == 1

    def test_delta_gap_is_never_cleared_by_a_later_fully_reviewed_sharded_delta(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lane = _lane(make_git_repo, "wt-rr-gap-kept")
        spy = _DeltaSpy(monkeypatch)
        runner = _EditingRunner(
            [*_cycle_zero(lane.roles), *_gapped_delta(lane.roles)],
            [_add_files(_DELTA_FILES), _add_files(_MORE_DELTA_FILES)],
        )

        result, verdict = _run(lane, runner, limit=lane.limit)

        first, second = spy.prepared
        _assert_sharded_delta(lane, first, _DELTA_FILES)
        _assert_sharded_delta(lane, second, _MORE_DELTA_FILES)
        details = _partial_details(result, verdict)
        assert "cycle(s) 1 " in details
        assert "unreviewed shards: 1" in details
        assert result.review is not None
        assert result.review.fix_cycles_used == 2


class TestCleanMode:
    def test_a_sharded_delta_gap_never_exits_clean_in_clean_mode(
        self, make_git_repo: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The DeltaGap park at cycle 2 fires; the run never exits clean.

        The ledger's unread guard does not apply: it only fires for a
        would-be-clean round (no new accepted findings), and the dirty cycle
        carries a new finding, so it is recorded as an ordinary ledger round.
        The later clean cycle then parks as ``codex_review_partial`` with the
        delta-unread text.
        """
        lane = _lane(make_git_repo, "wt-rr-clean")
        # The clean-mode ledger lives under .cw/; a real worktree ignores it.
        exclude = lane.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text(".cw/\n", encoding="utf-8")
        claude = RoleAwareClaudeRunner(
            lambda role: ClaudeRunResult(0, review_envelope(_doc_json(role=role)), "")
        )
        monkeypatch.setattr("cw.codex_clean_confirm._which_claude", lambda: "/x/claude")
        monkeypatch.setattr("cw.codex_background.RealClaudeRunner", lambda: claude)
        runner = _EditingRunner(
            [*_cycle_zero(lane.roles), *_gapped_delta(lane.roles)],
            [_add_files(_DELTA_FILES), _add_files([_SMALL_FILE], 5)],
        )
        loop = partial(
            run_review_with_fix_loop,
            runner=runner,
            task=_task(),
            worktree=lane.repo,
            default_branch="main",
            model=None,
            reasoning_effort=None,
            session_id="s-rr-clean",
            fix_loop_enabled=True,
            max_prompt_chars=lane.limit,
            stall_cycles=10,
        )
        client = ClientConfig(
            name="test",
            workspace_path=lane.repo,
            default_branch="main",
            review=ReviewConfig.model_validate(
                {"convergence": "clean", "confirm_backend": "none"}
            ),
        )
        run = _ConfirmLoop(
            task=_task(),
            worktree=lane.repo,
            client=client,
            sid="s-rr-clean",
            deadline=None,
            fix_loop_enabled=True,
            loop=loop,
        )

        outcome = run.iterate(seeds=(), budget=None)

        assert outcome.result.status == "blocked"
        assert outcome.result.blocker is not None
        assert outcome.result.blocker.reason == CODEX_REVIEW_PARTIAL
        assert _DELTA_UNREAD in outcome.result.blocker.details
        assert outcome.result.review is not None
        assert outcome.result.review.converged is not True
