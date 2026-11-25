"""The codex PLAN origin-sync pre-flight (#3465): decisions, bound and real git.

:func:`origin_sync_block` takes its fetch, read and sleep as arguments, so the
decision table runs with fakes and no clock. One case reads a real repository
and one checks the fetch's bound through the shared tick-fetch gate.
"""

from __future__ import annotations

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
from tests._shared.git_repos import add_bare_origin, commit_tracked_file
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
    def test_the_fetch_runs_under_the_origin_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[float] = []

        def fake_fetch(client: object) -> FetchResult:
            gate = active_tick_fetch()
            assert gate is not None
            seen.append(gate.per_call_s)
            return FetchResult(outcome=FetchOutcome.FETCHED, reason=None)

        monkeypatch.setattr(preflight, "fetch_default_branch", fake_fetch)
        fetch_origin(make_client(name="acme", workspace_path=tmp_path))
        assert seen == [ORIGIN_FETCH_TIMEOUT_SECONDS]

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
