"""The codex PLAN origin-sync pre-flight (#3465): decisions, bound and real git.

:func:`origin_sync_block` takes its fetch, read and sleep as arguments, so the
decision table runs with fakes and no clock. One case reads a real repository
and one checks the fetch's bound through the shared tick-fetch gate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

import cw.codex_plan._preflight as preflight
from cw.auto_dev_result import AutoDevResult
from cw.codex_plan._preflight import (
    DIVERGED_REASON,
    ORIGIN_FETCH_TIMEOUT_SECONDS,
    WAIT_ATTEMPTS,
    WAIT_INTERVAL_SECONDS,
    OriginSync,
    fetch_origin,
    origin_sync_block,
    read_origin_sync,
)
from cw.worktree import FetchOutcome, FetchResult, active_tick_fetch
from tests._doctor_helpers import make_client
from tests._shared.git_repos import add_bare_origin, commit_tracked_file, git_in
from tests._worktree_helpers import FakeClock, hanging_run
from tests.test_codex_plan_run import Harness, harness

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")
__all__ = ["harness"]

TICKET = "3465"
_SHA_LOCAL = "a" * 40
_SHA_ORIGIN = "b" * 40


class _Fetch:
    def __init__(self, outcome: FetchOutcome = FetchOutcome.FETCHED) -> None:
        self.outcome = outcome
        self.calls = 0

    def __call__(self, client: object) -> FetchResult:
        self.calls += 1
        return FetchResult(outcome=self.outcome, reason=None)


class _Reads:
    """A scripted sequence of origin-sync reads; the last one repeats."""

    def __init__(self, *states: OriginSync | None) -> None:
        self._states = list(states)
        self.calls = 0

    def __call__(self, worktree: Path, branch: str) -> OriginSync | None:
        self.calls += 1
        index = min(self.calls - 1, len(self._states) - 1)
        return self._states[index]


class _Sleeps:
    def __init__(self) -> None:
        self.seconds: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def _sync(ahead: int, behind: int) -> OriginSync:
    return OriginSync(
        local_sha=_SHA_LOCAL, origin_sha=_SHA_ORIGIN, ahead=ahead, behind=behind
    )


def _run(
    tmp_path: Path,
    fetch: _Fetch,
    read: _Reads,
    sleep: _Sleeps,
    *,
    branch: str = "main",
) -> AutoDevResult | None:
    client = make_client(name="acme", workspace_path=tmp_path).model_copy(
        update={"default_branch": branch}
    )
    return origin_sync_block(
        client=client,
        worktree=tmp_path,
        ticket_id=TICKET,
        fetch=fetch,
        read=read,
        sleep=sleep,
    )


class TestDecisions:
    def test_in_sync_passes_without_sleeping(self, tmp_path: Path) -> None:
        sleep = _Sleeps()
        assert _run(tmp_path, _Fetch(), _Reads(_sync(0, 0)), sleep) is None
        assert sleep.seconds == []

    def test_ahead_only_blocks_at_once(self, tmp_path: Path) -> None:
        sleep = _Sleeps()
        result = _run(tmp_path, _Fetch(), _Reads(_sync(1, 0)), sleep)
        assert result is not None
        assert sleep.seconds == []
        assert result.blocker is not None
        assert result.blocker.details == (
            f"local_main={_SHA_LOCAL}, origin_main={_SHA_ORIGIN}, ahead=1, behind=0"
        )

    def test_diverged_blocks_at_once(self, tmp_path: Path) -> None:
        sleep = _Sleeps()
        result = _run(tmp_path, _Fetch(), _Reads(_sync(2, 3)), sleep)
        assert result is not None
        assert result.blocker is not None
        assert sleep.seconds == []
        assert "ahead=2, behind=3" in result.blocker.details

    def test_behind_only_that_recovers_during_the_wait_passes(
        self, tmp_path: Path
    ) -> None:
        sleep = _Sleeps()
        read = _Reads(_sync(0, 2), _sync(0, 0))
        assert _run(tmp_path, _Fetch(), read, sleep) is None
        assert sleep.seconds == [WAIT_INTERVAL_SECONDS]

    def test_behind_only_then_an_unreadable_ref_fails_open_after_one_wait(
        self, tmp_path: Path
    ) -> None:
        sleep = _Sleeps()
        read = _Reads(_sync(0, 2), None)
        assert _run(tmp_path, _Fetch(), read, sleep) is None
        assert sleep.seconds == [WAIT_INTERVAL_SECONDS]
        assert read.calls == 2

    def test_behind_only_that_persists_waits_then_blocks(self, tmp_path: Path) -> None:
        sleep = _Sleeps()
        fetch = _Fetch()
        result = _run(tmp_path, fetch, _Reads(_sync(0, 2)), sleep)
        assert result is not None
        assert sleep.seconds == [WAIT_INTERVAL_SECONDS] * WAIT_ATTEMPTS
        assert fetch.calls == 1, "the wait never re-fetches"

    def test_a_non_fetched_outcome_fails_open(self, tmp_path: Path) -> None:
        for outcome in (FetchOutcome.FAILED, FetchOutcome.BRANCH_ABSENT):
            sleep = _Sleeps()
            read = _Reads(_sync(5, 5))
            assert _run(tmp_path, _Fetch(outcome), read, sleep) is None
            assert read.calls == 0

    def test_an_unreadable_ref_fails_open(self, tmp_path: Path) -> None:
        assert _run(tmp_path, _Fetch(), _Reads(None), _Sleeps()) is None

    def test_the_block_shape(self, tmp_path: Path) -> None:
        result = _run(tmp_path, _Fetch(), _Reads(_sync(1, 0)), _Sleeps())
        assert result is not None
        assert result.blocker is not None
        assert result.status == "blocked"
        assert result.stage_reached == "stage1_pre_flight"
        assert result.blocker.reason == DIVERGED_REASON
        assert result.blocker.retry_eligible is True
        assert result.blocker.retry_delay_seconds is None
        assert result.next_actions == ["sync_local_main"]

    def test_a_non_default_branch_is_the_one_compared(self, tmp_path: Path) -> None:
        seen: list[str] = []

        def read(worktree: Path, branch: str) -> OriginSync:
            seen.append(branch)
            return _sync(0, 0)

        client = make_client(name="acme", workspace_path=tmp_path).model_copy(
            update={"default_branch": "trunk"}
        )
        assert (
            origin_sync_block(
                client=client,
                worktree=tmp_path,
                ticket_id=TICKET,
                fetch=_Fetch(),
                read=read,
                sleep=_Sleeps(),
            )
            is None
        )
        assert seen == ["trunk"]


class TestFetchBound:
    def test_the_fetch_runs_under_a_sixty_second_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The literals, not the constant against itself: 60s per call and total."""
        seen: list[tuple[float, float]] = []

        def fake_fetch(client: object) -> FetchResult:
            gate = active_tick_fetch()
            assert gate is not None
            seen.append((gate.per_call_s, gate.budget_s))
            return FetchResult(outcome=FetchOutcome.FETCHED, reason=None)

        monkeypatch.setattr(preflight, "fetch_default_branch", fake_fetch)
        fetch_origin(make_client(name="acme", workspace_path=tmp_path))
        assert seen == [(60.0, 60.0)]
        assert ORIGIN_FETCH_TIMEOUT_SECONDS == 60.0

    def test_a_hanging_fetch_fails_open_and_never_reads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The real fetch seam times out: no block, a WARNING, one bounded child."""
        run = hanging_run(FakeClock())
        monkeypatch.setattr("cw.worktree._git.subprocess.run", run)
        read = _Reads(_sync(5, 5))
        sleep = _Sleeps()
        client = make_client(name="acme", workspace_path=tmp_path).model_copy(
            update={"default_branch": "main"}
        )
        with caplog.at_level(logging.WARNING):
            result = origin_sync_block(
                client=client,
                worktree=tmp_path,
                ticket_id=TICKET,
                fetch=fetch_origin,
                read=read,
                sleep=sleep,
            )
        assert result is None
        assert read.calls == 0
        assert sleep.seconds == []
        assert run.argv == [["git", "fetch", "origin", "main", "--quiet"]]
        assert run.timeouts == [60.0]
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("timed out" in m for m in warnings), warnings
        assert any("origin sync skipped for main" in m for m in warnings), warnings

    def test_the_wait_constants_match_the_tick_stale_window(self) -> None:
        assert WAIT_ATTEMPTS * WAIT_INTERVAL_SECONDS == 90


class TestRealGit:
    def test_read_origin_sync_counts_local_commits_against_origin(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("preflight-real")
        add_bare_origin(repo)
        branch = "main"
        in_sync = read_origin_sync(repo, branch)
        assert in_sync is not None
        assert (in_sync.ahead, in_sync.behind) == (0, 0)
        commit_tracked_file(repo, "local.py")
        ahead = read_origin_sync(repo, branch)
        assert ahead is not None
        assert (ahead.ahead, ahead.behind) == (1, 0)

    def test_read_origin_sync_is_none_without_an_origin(
        self, make_git_repo: Callable[..., Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo("preflight-no-origin")
        with caplog.at_level(logging.WARNING, logger=preflight.__name__):
            assert read_origin_sync(repo, "main") is None
        assert "origin sync not read for main" in caplog.text

    def test_read_origin_sync_is_none_for_a_branch_origin_lacks(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        repo, _origin = make_git_repo_with_origin("preflight-ghost")
        assert read_origin_sync(repo, "ghost") is None

    def test_read_origin_sync_counts_a_behind_only_branch_in_order(
        self, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
    ) -> None:
        """Origin ahead by two: ``ahead`` is the left count, ``behind`` the right."""
        repo, _origin = make_git_repo_with_origin("preflight-behind")
        git_in(repo, "checkout", "main")
        commit_tracked_file(repo, "one.py")
        commit_tracked_file(repo, "two.py")
        git_in(repo, "push", "origin", "main")
        origin_sha = git_in(repo, "rev-parse", "HEAD")
        git_in(repo, "reset", "--hard", "HEAD~2")
        local_sha = git_in(repo, "rev-parse", "HEAD")
        sync = read_origin_sync(repo, "main")
        assert sync == OriginSync(
            local_sha=local_sha, origin_sha=origin_sha, ahead=0, behind=2
        )
        assert not sync.in_sync


class TestRunWiring:
    def test_a_default_branch_ahead_of_origin_blocks_before_any_turn(
        self, harness: Callable[..., Harness]
    ) -> None:
        h = harness()
        add_bare_origin(h.worktree)
        commit_tracked_file(h.worktree, "local.py")
        result = h.run()
        assert result.blocker is not None
        assert result.blocker.reason == DIVERGED_REASON
        assert result.stage_reached == "stage1_pre_flight"
        assert h.plan_calls == []
