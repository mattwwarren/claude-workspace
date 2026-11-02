"""Codex-lane cycle-0 prompt-size handling (#2867).

``run_review_with_fix_loop`` forwards the lane's ``max_prompt_chars`` to cycle
0's ``run_review``: a diff over the limit is reviewed shard by shard (#2864's
planner), a diff that cannot be sharded parks as a named, non-retryable
``agent_block`` before any codex call, and a diff within the limit takes the
unsharded path with its prompts unchanged. Every test drives the real loop and
the real review pass over a real git repo with a scripted codex runner; none
makes a live codex call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.codex_fix_loop import _driver, run_review_with_fix_loop
from cw.codex_review import (
    CODEX_BUDGET_EXHAUSTED,
    CODEX_INPUT_TOO_LARGE,
    CODEX_REVIEW_UNPARSEABLE,
    ReviewInputTooLargeError,
    _prepare_review_pass,
)
from cw.codex_review._capability import _PROBE_ARGV
from cw.codex_runner import CodexRunResult, FakeCodexRunner
from tests._codex_review_helpers import (
    _all_ok,
    _large_diff_repo,
    _must_fix_in,
    _ok_result,
    _prompt_files,
    _SequencedRunner,
    _shard_limit_inputs,
    _sharded_setup,
    _stem_content,
    _task,
)
from tests._review_convergence_helpers import (
    codex_audit_fixture,
    warm_codex_capability,
)
from tests.conftest import commit_tracked_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.review_findings import ReviewVerdict

_DRIVER_LOGGER = "cw.codex_fix_loop._driver"
_HUGE_FILE = "huge/blob.py"
_TOO_LARGE_WARNING = "cycle 0 review prompt too large"
#: In (30, 60): above the per-role floor so cycle 0's roles run, below the
#: fix-cycle floor so the loop parks on budget instead of starting a fix.
_BUDGET_BELOW_FIX_FLOOR = 59


def _run_lane(
    repo: Path,
    runner: _SequencedRunner,
    *,
    limit: int | None,
    session_id: str,
    fix_loop_enabled: bool = False,
    budget: int | None = None,
) -> tuple[AutoDevResult, ReviewVerdict | None]:
    return run_review_with_fix_loop(
        runner=runner,
        task=_task(),
        worktree=repo,
        default_branch="main",
        model=None,
        reasoning_effort=None,
        wall_clock_budget_seconds=budget,
        session_id=session_id,
        fix_loop_enabled=fix_loop_enabled,
        max_prompt_chars=limit,
    )


def _reviewer_calls(runner: _SequencedRunner) -> list[dict[str, object]]:
    return [call for call in runner.calls if call["argv"] != list(_PROBE_ARGV)]


class TestShardedCycleZero:
    def test_fix_loop_disabled_reviews_every_shard_within_the_limit(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-lane-shard")
        setup = _sharded_setup(repo, "s-lane-shard")
        roles, shards = setup.prepared.roles, len(setup.plans)
        runner = _SequencedRunner(_all_ok(roles, shards))

        result, verdict = _run_lane(
            repo, runner, limit=setup.limit, session_id="s-lane-shard"
        )

        calls = _reviewer_calls(runner)
        assert len(calls) == len(roles) * shards
        prompts = [str(call["stdin"]) for call in calls]
        assert all(len(prompt) <= setup.limit for prompt in prompts)
        seen = {path for prompt in prompts for path in _prompt_files(prompt)}
        assert seen == set(setup.prepared.changed_files)
        assert verdict is not None
        assert len(verdict.shards) == shards
        assert all(shard.reviewed for shard in verdict.shards)
        assert result.blocker is None

    def test_fix_loop_enabled_must_fix_in_second_shard_enters_the_loop(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-lane-shard-dirty")
        setup = _sharded_setup(repo, "s-lane-shard-dirty")
        roles, shards = setup.prepared.roles, len(setup.plans)
        target = setup.plans[1].files[0]
        results = _all_ok(roles, shards)
        results[len(roles)] = _ok_result(roles[0], findings=_must_fix_in(target))
        runner = _SequencedRunner(results)

        result, verdict = _run_lane(
            repo,
            runner,
            limit=setup.limit,
            session_id="s-lane-shard-dirty",
            fix_loop_enabled=True,
            budget=_BUDGET_BELOW_FIX_FLOOR,
        )

        # Cycle 0 read every shard; the loop then parked on budget, carrying
        # the shard-2 finding, without starting a fix invocation.
        assert len(_reviewer_calls(runner)) == len(roles) * shards
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_BUDGET_EXHAUSTED
        assert verdict is not None
        assert [f.file for f in verdict.must_fix] == [target]


class TestUnshardableCycleZero:
    def test_a_file_too_large_for_any_prompt_parks_as_agent_block(
        self, make_git_repo: Callable[[str], Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-lane-huge")
        commit_tracked_file(repo, _HUGE_FILE, _stem_content(_HUGE_FILE, 400))
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-lane-huge"
        )
        overhead, costs = _shard_limit_inputs(prepared)
        small = max(cost for path, cost in costs.items() if path != _HUGE_FILE)
        limit = overhead + small + 1
        assert costs[_HUGE_FILE] > limit - overhead
        runner = _SequencedRunner([])

        with caplog.at_level(logging.WARNING, logger=_DRIVER_LOGGER):
            result, verdict = _run_lane(
                repo, runner, limit=limit, session_id="s-lane-huge"
            )

        assert verdict is None
        assert result.status == "blocked"
        assert result.blocker is not None
        assert result.blocker.reason == "agent_block"
        assert result.blocker.retry_eligible is False
        for fragment in (_HUGE_FILE, "file_too_large", "actual_chars=", "max_chars="):
            assert fragment in result.blocker.details
        assert runner.calls == []
        (warning,) = [
            r
            for r in caplog.records
            if r.name == _DRIVER_LOGGER and _TOO_LARGE_WARNING in r.getMessage()
        ]
        assert warning.levelno == logging.WARNING
        assert "ticket=T-1" in warning.getMessage()
        assert "cause=file_too_large" in warning.getMessage()

    def test_prompt_overhead_over_the_limit_parks_as_agent_block(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-lane-overhead")
        warm_codex_capability(repo, _task())
        runner = _SequencedRunner([])

        result, verdict = _run_lane(repo, runner, limit=1, session_id="s-lane-over")

        assert verdict is None
        assert result.blocker is not None
        assert result.blocker.reason == "agent_block"
        assert result.blocker.retry_eligible is False
        assert "input_too_large" in result.blocker.details
        assert "file_too_large" not in result.blocker.details
        assert "max_chars=1" in result.blocker.details
        assert runner.calls == []

    def test_cycle_zero_agent_block_text_is_unchanged(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """#2877 adds a re-review variant; cycle 0's text stays byte-identical."""
        repo = make_git_repo("wt-lane-text")
        exc = ReviewInputTooLargeError(actual_chars=10, max_chars=5, file="a.py")

        result = _driver._input_too_large_block(_task(), repo, exc)

        assert result.blocker is not None
        assert result.blocker.reason == "agent_block"
        assert result.blocker.retry_eligible is False
        assert result.blocker.details == (
            "codex review prompt too large (file_too_large): a.py: one changed "
            "file does not fit a review prompt: actual_chars=10 max_chars=5; "
            "nothing was reviewed, truncated or retried; split or shrink the "
            "named file, or fix the oversize input, then requeue"
        )


class TestWithinTheLimit:
    @pytest.mark.parametrize("over_longest", [1, None], ids=["limit", "no-limit"])
    def test_prompts_are_unchanged_and_unsharded(
        self, make_git_repo: Callable[[str], Path], over_longest: int | None
    ) -> None:
        repo = _large_diff_repo(make_git_repo, "wt-lane-fits")
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-lane-fits"
        )
        longest = max(len(p) for p in prepared.prompts_by_role.values())
        limit = None if over_longest is None else longest + over_longest
        runner = _SequencedRunner([_ok_result(role) for role in prepared.roles])

        result, verdict = _run_lane(repo, runner, limit=limit, session_id="s-fits")

        assert [call["stdin"] for call in runner.calls] == [
            prepared.prompts_by_role[role] for role in prepared.roles
        ]
        assert verdict is not None
        assert verdict.shards == []
        assert result.blocker is None


class TestBackendRejection:
    def test_per_role_size_rejection_is_a_named_non_retryable_park(
        self, make_git_repo: Callable[[str], Path]
    ) -> None:
        """A backend size rejection on the lane stays non-retryable and named.

        The audit stream is the hand-authored #2864 fixture; no live call.
        """
        repo = _large_diff_repo(make_git_repo, "wt-lane-rejected")
        prepared = _prepare_review_pass(
            _task(), repo, "main", runner=FakeCodexRunner(), session_id="s-lane-rej"
        )
        rejected = CodexRunResult(
            returncode=1,
            stdout=codex_audit_fixture("input_too_large_turn_failed.jsonl"),
            stderr="",
        )
        runner = _SequencedRunner([rejected for _role in prepared.roles])

        result, verdict = _run_lane(
            repo, runner, limit=None, session_id="s-lane-rej", fix_loop_enabled=True
        )

        assert verdict is None
        assert result.blocker is not None
        assert result.blocker.reason == CODEX_REVIEW_UNPARSEABLE
        assert not result.blocker.retry_eligible
        assert CODEX_INPUT_TOO_LARGE in result.blocker.details
        assert "actual_chars=1379349 max_chars=1048576" in result.blocker.details
