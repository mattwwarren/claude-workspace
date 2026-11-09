"""Tests for :func:`cw.review_debt_filing.sweep_debt_closes` (#3044, RFC 0017 III2).

Every ``gh`` call goes through :class:`tests._tool_fakes.FakeGh`; the repo-slug
check and the git toplevel anchor are patched at the ``_close`` seam.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from cw.gh import AGENT_COMMENT_MARKER, GhFailure
from cw.models import ClientConfig, OrchestratorEventType
from cw.review_debt_filing import (
    MAX_DEBT_CLOSES_PER_SWEEP,
    CloseContext,
    CloseOutcome,
    CloseResult,
    sweep_debt_closes,
)
from cw.review_debt_filing._events import emit_closed
from tests._review_debt_helpers import (
    DEBT_TICKET,
    FakeClock,
    _clock_stepper,
    _debt_body,
    _events_of,
    _sweep_state,
    _write_debt_config,
)
from tests._tool_fakes import FakeGh

if TYPE_CHECKING:
    from pathlib import Path

    from cw.review_debt_filing import DebtTracker

_KEY = "c0ffee" * 10 + "abcd"
_PR1 = "https://github.com/owner/repo/pull/11"
_PR2 = "https://github.com/owner/repo/pull/12"
_ISSUE = 42
_ISSUE_URL = f"https://github.com/owner/repo/issues/{_ISSUE}"


def _sentence(pr_url: str) -> str:
    return (
        "Closing as not planned: the pull request that carried this review debt,"
        f" {pr_url}, was closed without merging, so the deferred finding never"
        " reached the default branch. Reopen this issue if the work moves to"
        " another PR."
    )


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """Same-repo slug check and no git toplevel; records every slug check."""
    checks: list[tuple[str, Path]] = []

    def _no_mismatch(slug: str, git_dir: Path) -> str | None:
        checks.append((slug, git_dir))
        return None

    monkeypatch.setattr("cw.review_debt_filing._close.repo_slug_mismatch", _no_mismatch)
    monkeypatch.setattr("cw.review_debt_filing._close.git_toplevel", lambda _cwd: None)
    return checks


def _client(
    tmp_path: Path,
    name: str = "acme",
    *,
    tracker: str | None = "github-issues",
    auto: bool | None = True,
) -> ClientConfig:
    root = tmp_path / name
    root.mkdir()
    _write_debt_config(root, tracker=tracker, auto=auto)
    return ClientConfig(name=name, workspace_path=root)


def _sweep(
    gh: FakeGh, *clients: ClientConfig, clock: FakeClock | None = None
) -> list[CloseResult]:
    return sweep_debt_closes(
        {cfg.name: cfg for cfg in clients},
        _sweep_state(clock),
        tracker_for=lambda _cfg: gh,
    )


def _closed_issue(gh: FakeGh, *, pr_urls: tuple[str, ...] = (_PR1,)) -> None:
    gh.open_issues[_ISSUE] = _debt_body(_KEY, pr_urls=pr_urls)
    for url in pr_urls:
        gh.pr_states.setdefault(url, "CLOSED")


def _gh_calls(gh: FakeGh, prefix: str) -> list[str]:
    return [call for call in gh.calls if call.startswith(prefix)]


class TestClosing:
    def test_a_closed_unmerged_pr_closes_the_issue_once_as_not_planned(
        self, tmp_path: Path
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        results = _sweep(gh, _client(tmp_path))
        expected = (
            f"{_sentence(_PR1)}\n\ncw review-debt key `{_KEY[:12]}`"
            f"\n\n{AGENT_COMMENT_MARKER}"
        )
        assert gh.closed == [(_ISSUE, expected, "not planned")]
        assert results == [CloseResult("acme", _ISSUE, (_PR1,), CloseOutcome.CLOSED)]
        assert _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSED) == [
            {
                "ticket_id": DEBT_TICKET,
                "client": "acme",
                "key": _KEY,
                "tracker": "github-issues",
                "tracker_id": str(_ISSUE),
                "url": _ISSUE_URL,
                "pr_url": _PR1,
            }
        ]

    def test_an_unparseable_key_drops_the_key_line(self, tmp_path: Path) -> None:
        gh = FakeGh()
        gh.open_issues[_ISSUE] = _debt_body(None, queue_ticket=None, pr_urls=(_PR1,))
        gh.pr_states[_PR1] = "CLOSED"
        _sweep(gh, _client(tmp_path))
        ((_number, comment, _reason),) = gh.closed
        assert comment == f"{_sentence(_PR1)}\n\n{AGENT_COMMENT_MARKER}"
        assert "cw review-debt key" not in comment
        (event,) = _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSED)
        assert (event["key"], event["ticket_id"]) == (None, None)

    def test_sweeping_twice_closes_once(self, tmp_path: Path) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        cfg = _client(tmp_path)
        _sweep(gh, cfg)
        assert _sweep(gh, cfg) == []
        assert len(_gh_calls(gh, "close_issue")) == 1

    def test_auto_create_issues_false_still_closes(self, tmp_path: Path) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        _sweep(gh, _client(tmp_path, auto=False))
        assert len(gh.closed) == 1

    def test_every_pr_closed_closes_naming_the_newest(self, tmp_path: Path) -> None:
        gh = FakeGh()
        _closed_issue(gh, pr_urls=(_PR1, _PR2))
        results = _sweep(gh, _client(tmp_path))
        ((_number, comment, _reason),) = gh.closed
        assert comment.startswith(_sentence(_PR2))
        assert results[0].pr_urls == (_PR1, _PR2)
        (event,) = _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSED)
        assert event["pr_url"] == _PR2

    def test_an_issue_without_a_pr_marker_is_untouched(self, tmp_path: Path) -> None:
        gh = FakeGh()
        gh.open_issues[_ISSUE] = _debt_body(_KEY)
        assert _sweep(gh, _client(tmp_path)) == []
        assert gh.calls == ["list_open_issues_by_label:review-debt"]

    def test_issues_are_swept_in_number_order(self, tmp_path: Path) -> None:
        gh = FakeGh()
        gh.open_issues[9] = _debt_body(_KEY, pr_urls=(_PR2,))
        gh.open_issues[3] = _debt_body(_KEY, pr_urls=(_PR1,))
        gh.pr_states.update({_PR1: "CLOSED", _PR2: "CLOSED"})
        results = _sweep(gh, _client(tmp_path))
        assert [r.issue for r in results] == [3, 9]


class TestSkips:
    @pytest.mark.parametrize(
        ("state", "outcome"),
        [
            ("OPEN", CloseOutcome.SKIPPED_PR_OPEN),
            ("MERGED", CloseOutcome.SKIPPED_PR_MERGED),
            (None, CloseOutcome.SKIPPED_PR_UNKNOWN),
            ("DRAFT", CloseOutcome.SKIPPED_PR_UNKNOWN),
        ],
    )
    def test_a_pr_that_is_not_closed_leaves_the_issue_open(
        self, tmp_path: Path, state: str | None, outcome: CloseOutcome
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        gh.pr_states[_PR1] = state
        results = _sweep(gh, _client(tmp_path))
        assert results == [CloseResult("acme", _ISSUE, (_PR1,), outcome)]
        assert gh.closed == []
        assert _ISSUE in gh.open_issues

    def test_a_merged_pr_is_memoized_across_sweeps(self, tmp_path: Path) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        gh.pr_states[_PR1] = "MERGED"
        cfg = _client(tmp_path)
        state = _sweep_state()
        for _ in range(2):
            results = sweep_debt_closes(
                {cfg.name: cfg}, state, tracker_for=lambda _cfg: gh
            )
            assert results[0].outcome is CloseOutcome.SKIPPED_PR_MERGED
        assert _gh_calls(gh, "fetch_pr_state") == [f"fetch_pr_state:{_PR1}"]
        assert state.merged_prs == {_PR1}

    def test_one_open_pr_among_several_keeps_the_issue_open(
        self, tmp_path: Path
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh, pr_urls=(_PR1, _PR2))
        gh.pr_states[_PR2] = "OPEN"
        results = _sweep(gh, _client(tmp_path))
        assert results[0].outcome is CloseOutcome.SKIPPED_PR_OPEN
        assert gh.closed == []

    def test_a_merged_pr_first_stops_before_the_next_read(self, tmp_path: Path) -> None:
        gh = FakeGh()
        _closed_issue(gh, pr_urls=(_PR1, _PR2))
        gh.pr_states[_PR1] = "MERGED"
        results = _sweep(gh, _client(tmp_path))
        assert results[0].outcome is CloseOutcome.SKIPPED_PR_MERGED
        assert _gh_calls(gh, "fetch_pr_state") == [f"fetch_pr_state:{_PR1}"]

    def test_a_pr_shared_by_issues_is_read_once_per_sweep(self, tmp_path: Path) -> None:
        gh = FakeGh()
        gh.open_issues[1] = _debt_body(_KEY, pr_urls=(_PR1,))
        gh.open_issues[2] = _debt_body("d" * 64, pr_urls=(_PR1,))
        gh.pr_states[_PR1] = "OPEN"
        results = _sweep(gh, _client(tmp_path))
        assert [r.outcome for r in results] == [CloseOutcome.SKIPPED_PR_OPEN] * 2
        assert len(_gh_calls(gh, "fetch_pr_state")) == 1

    def test_a_foreign_repo_pr_is_skipped_without_a_pr_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, Path]] = []

        def _mismatch(slug: str, git_dir: Path) -> str | None:
            seen.append((slug, git_dir))
            return "PR repo differs from origin"

        monkeypatch.setattr(
            "cw.review_debt_filing._close.repo_slug_mismatch", _mismatch
        )
        gh = FakeGh()
        _closed_issue(gh, pr_urls=(_PR1, _PR2))
        gh.open_issues[50] = _debt_body("d" * 64, pr_urls=(_PR1,))
        cfg = _client(tmp_path)
        results = _sweep(gh, cfg)
        assert [r.outcome for r in results] == [CloseOutcome.SKIPPED_REPO_MISMATCH] * 2
        assert _gh_calls(gh, "fetch_pr_state") == []
        assert gh.closed == []
        assert seen == [("owner/repo", cfg.workspace_path)]

    @pytest.mark.parametrize(
        "url",
        [
            "--repo=evil/x",
            "-https://github.com/owner/repo/pull/1",
            "https://github.com/owner/repo/pull/1 --web",
            "http://github.com/owner/repo/pull/1",
            "https://github.com/owner/repo/issues/1",
            "https://github.com/owner/repo/pull/0",
            "https://github.example.com/owner/repo/pull/1",
            "https://github.com/owner/pull/1",
        ],
    )
    def test_an_unparseable_or_non_github_url_is_unknown_and_never_reaches_gh(
        self, tmp_path: Path, url: str
    ) -> None:
        gh = FakeGh()
        gh.open_issues[_ISSUE] = _debt_body(_KEY, pr_urls=(_PR1, url))
        gh.pr_states[_PR1] = "CLOSED"
        results = _sweep(gh, _client(tmp_path))
        assert results == [
            CloseResult("acme", _ISSUE, (_PR1, url), CloseOutcome.SKIPPED_PR_UNKNOWN)
        ]
        assert gh.calls == ["list_open_issues_by_label:review-debt"]


class TestListing:
    def test_a_truncated_listing_skips_the_client_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gh = FakeGh()
        gh.list_truncated = True
        _closed_issue(gh)
        with caplog.at_level(logging.WARNING):
            assert _sweep(gh, _client(tmp_path)) == []
        assert gh.calls == ["list_open_issues_by_label:review-debt"]
        assert "acme" in caplog.text
        assert "listing cap" in caplog.text

    def test_a_failed_listing_skips_the_client_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gh = FakeGh()
        gh.fail_list = GhFailure.AUTH
        with caplog.at_level(logging.WARNING):
            assert _sweep(gh, _client(tmp_path)) == []
        assert gh.calls == ["list_open_issues_by_label:review-debt"]
        assert "auth" in caplog.text

    @pytest.mark.parametrize("tracker", ["linear", None, "GitHub-Issues"])
    def test_a_non_github_tracker_makes_no_gh_call(
        self, tmp_path: Path, tracker: str | None
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        assert _sweep(gh, _client(tmp_path, tracker=tracker)) == []
        assert gh.calls == []

    def test_a_client_with_no_project_config_makes_no_gh_call(
        self, tmp_path: Path
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        cfg = ClientConfig(name="bare", workspace_path=tmp_path)
        assert _sweep(gh, cfg) == []
        assert gh.calls == []

    def test_each_github_client_gets_its_own_tracker(self, tmp_path: Path) -> None:
        trackers = {"a": FakeGh(), "b": FakeGh(), "c": FakeGh()}
        for gh in (trackers["a"], trackers["c"]):
            _closed_issue(gh)
        clients = {
            "a": _client(tmp_path, "a"),
            "b": _client(tmp_path, "b", tracker="linear"),
            "c": _client(tmp_path, "c"),
        }

        def _tracker_for(cfg: ClientConfig) -> DebtTracker:
            return trackers[cfg.name]

        results = sweep_debt_closes(clients, _sweep_state(), tracker_for=_tracker_for)
        assert [r.client for r in results] == ["a", "c"]
        assert trackers["b"].calls == []


class TestFailures:
    def test_a_failed_close_is_reread_once_then_retried_next_sweep(
        self, tmp_path: Path
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        gh.fail_close = [GhFailure.NETWORK]
        cfg = _client(tmp_path)
        state = _sweep_state()
        first = sweep_debt_closes({cfg.name: cfg}, state, tracker_for=lambda _c: gh)
        assert first == [
            CloseResult("acme", _ISSUE, (_PR1,), CloseOutcome.FAILED, GhFailure.NETWORK)
        ]
        assert _gh_calls(gh, "fetch_issue_state") == [f"fetch_issue_state:{_ISSUE}"]
        assert _ISSUE in gh.open_issues
        second = sweep_debt_closes({cfg.name: cfg}, state, tracker_for=lambda _c: gh)
        assert second[0].outcome is CloseOutcome.CLOSED
        assert len(gh.closed) == 1
        (failed,) = _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSE_FAILED)
        assert failed["kind"] == "network"
        assert failed["ticket_id"] == DEBT_TICKET

    def test_the_failed_event_is_rate_limited_per_issue_and_kind(
        self, tmp_path: Path
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        gh.fail_close = [GhFailure.AUTH, GhFailure.AUTH, GhFailure.OTHER]
        cfg = _client(tmp_path)
        state = _sweep_state()
        for _ in range(3):
            sweep_debt_closes({cfg.name: cfg}, state, tracker_for=lambda _c: gh)
        failed = _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSE_FAILED)
        assert [event["kind"] for event in failed] == ["auth", "other"]
        assert state.reported == {("acme", _ISSUE, "auth"), ("acme", _ISSUE, "other")}

    def test_an_already_closed_race_counts_as_closed_without_an_event(
        self, tmp_path: Path
    ) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        gh.fail_close = [GhFailure.OTHER]
        gh.issue_states[_ISSUE] = "CLOSED"
        results = _sweep(gh, _client(tmp_path))
        assert results == [CloseResult("acme", _ISSUE, (_PR1,), CloseOutcome.CLOSED)]
        assert _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSED) == []
        assert _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSE_FAILED) == []

    def test_an_unknown_reread_counts_as_failed(self, tmp_path: Path) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        gh.fail_close = [GhFailure.NETWORK]
        gh.fail_issue_state = {str(_ISSUE)}
        results = _sweep(gh, _client(tmp_path))
        assert results[0].outcome is CloseOutcome.FAILED

    def test_a_failed_event_write_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            msg = "inbox gone"
            raise OSError(msg)

        monkeypatch.setattr("cw.review_debt_filing._events.record_event", _boom)
        gh = FakeGh()
        _closed_issue(gh)
        results = _sweep(gh, _client(tmp_path))
        assert results[0].outcome is CloseOutcome.CLOSED


class TestBounds:
    def test_the_close_cap_leaves_the_rest_for_the_next_sweep(
        self, tmp_path: Path
    ) -> None:
        gh = FakeGh()
        total = MAX_DEBT_CLOSES_PER_SWEEP + 1
        for number in range(1, total + 1):
            gh.open_issues[number] = _debt_body(_KEY, pr_urls=(_PR1,))
        gh.pr_states[_PR1] = "CLOSED"
        cfg = _client(tmp_path)
        state = _sweep_state()
        first = sweep_debt_closes({cfg.name: cfg}, state, tracker_for=lambda _c: gh)
        assert len(first) == MAX_DEBT_CLOSES_PER_SWEEP
        assert list(gh.open_issues) == [total]
        second = sweep_debt_closes({cfg.name: cfg}, state, tracker_for=lambda _c: gh)
        assert [r.issue for r in second] == [total]

    def test_the_cap_also_stops_later_clients(self, tmp_path: Path) -> None:
        first_gh, second_gh = FakeGh(), FakeGh()
        for number in range(1, MAX_DEBT_CLOSES_PER_SWEEP + 1):
            first_gh.open_issues[number] = _debt_body(_KEY, pr_urls=(_PR1,))
        first_gh.pr_states[_PR1] = "CLOSED"
        _closed_issue(second_gh)
        trackers = {"a": first_gh, "b": second_gh}
        clients = {"a": _client(tmp_path, "a"), "b": _client(tmp_path, "b")}
        sweep_debt_closes(
            clients, _sweep_state(), tracker_for=lambda cfg: trackers[cfg.name]
        )
        assert second_gh.calls == []

    def test_a_budget_spent_mid_sweep_stops_cleanly(self, tmp_path: Path) -> None:
        clock = FakeClock()
        gh = FakeGh()
        gh.on_call = _clock_stepper(clock, 25.0)
        gh.fail_close = [GhFailure.NETWORK]
        for number in (1, 2):
            gh.open_issues[number] = _debt_body(_KEY, pr_urls=(_PR1,))
        gh.pr_states[_PR1] = "CLOSED"
        results = _sweep(gh, _client(tmp_path), clock=clock)
        # list (0->25), PR read (25->50), close with 10s left (50->60), then
        # no budget for the re-read: issue 1 stays FAILED, issue 2 waits.
        assert gh.calls == [
            "list_open_issues_by_label:review-debt",
            f"fetch_pr_state:{_PR1}",
            "close_issue:1",
        ]
        assert gh.timeouts[-1] == ("close_issue", 10)
        assert results == [
            CloseResult("acme", 1, (_PR1,), CloseOutcome.FAILED, GhFailure.NETWORK)
        ]

    def test_a_budget_spent_after_the_listing_reads_nothing(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        gh = FakeGh()
        gh.on_call = _clock_stepper(clock, 61.0)
        _closed_issue(gh)
        assert _sweep(gh, _client(tmp_path), clock=clock) == []
        assert gh.calls == ["list_open_issues_by_label:review-debt"]

    def test_a_budget_spent_before_the_close_leaves_the_issue(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        gh = FakeGh()
        gh.on_call = _clock_stepper(clock, 30.0)
        _closed_issue(gh)
        assert _sweep(gh, _client(tmp_path), clock=clock) == []
        assert gh.closed == []
        assert _gh_calls(gh, "close_issue") == []

    def test_a_spent_budget_before_the_listing_makes_no_call(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        gh = FakeGh()
        _closed_issue(gh)
        state = _sweep_state(clock)
        cfg = _client(tmp_path)

        def _late(_cfg: ClientConfig) -> FakeGh:
            clock.now += 120.0
            return gh

        assert sweep_debt_closes({cfg.name: cfg}, state, tracker_for=_late) == []
        assert gh.calls == []

    def test_each_call_gets_min_of_its_cap_and_the_remaining_budget(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock()
        gh = FakeGh()
        gh.on_call = _clock_stepper(clock, 20.0)
        _closed_issue(gh)
        _sweep(gh, _client(tmp_path), clock=clock)
        assert gh.timeouts == [
            ("list_open_issues_by_label", 30),
            ("fetch_pr_state", 20),
            ("close_issue", 20),
        ]

    def test_no_gh_call_runs_under_a_lock(self, tmp_path: Path) -> None:
        gh = FakeGh()
        _closed_issue(gh)
        gh.fail_close = [GhFailure.OTHER]
        _sweep(gh, _client(tmp_path))
        assert len(gh.lock_snapshots) == 4
        assert all(snapshot == () for snapshot in gh.lock_snapshots)


class TestRedaction:
    def test_a_home_path_in_the_key_is_redacted(self, tmp_path: Path) -> None:
        gh = FakeGh()
        key = "/home/alice/" + "x" * 52
        gh.open_issues[_ISSUE] = _debt_body(key, pr_urls=(_PR1,))
        gh.pr_states[_PR1] = "CLOSED"
        _sweep(gh, _client(tmp_path))
        ((_number, comment, _reason),) = gh.closed
        assert "/home/alice" not in comment
        assert "cw review-debt key `<home>/`" in comment

    def test_a_worktree_anchor_in_the_key_is_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []

        def _toplevel(cwd: Path) -> str:
            seen.append(cwd)
            return "/srv/wt"

        monkeypatch.setattr("cw.review_debt_filing._close.git_toplevel", _toplevel)
        gh = FakeGh()
        gh.open_issues[1] = _debt_body("/srv/wt/abcd" + "e" * 52, pr_urls=(_PR1,))
        gh.open_issues[2] = _debt_body("/srv/wt/abcd" + "f" * 52, pr_urls=(_PR1,))
        gh.pr_states[_PR1] = "CLOSED"
        cfg = _client(tmp_path)
        _sweep(gh, cfg)
        assert [comment for _n, comment, _r in gh.closed] == [
            f"{_sentence(_PR1)}\n\ncw review-debt key `<worktree>/abcd`"
            f"\n\n{AGENT_COMMENT_MARKER}"
        ] * 2
        assert seen == [cfg.workspace_path]

    def test_no_anchor_is_resolved_when_nothing_closes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _must_not_run(_cwd: Path) -> str:
            msg = "git_toplevel must not run when nothing closes"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.review_debt_filing._close.git_toplevel", _must_not_run)
        gh = FakeGh()
        _closed_issue(gh)
        gh.pr_states[_PR1] = "OPEN"
        _sweep(gh, _client(tmp_path))


def test_a_hand_built_close_context_is_accepted() -> None:
    ctx = CloseContext(client="acme")
    assert (ctx.home, ctx.worktrees) == ("", ())
    assert emit_closed(ctx, 1, "u", _PR1, None, None) is None
    (event,) = _events_of(OrchestratorEventType.REVIEW_DEBT_CLOSED)
    assert event["client"] == "acme"
