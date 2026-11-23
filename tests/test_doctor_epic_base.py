"""``cw doctor`` ``epic-base/<client>/<branch>`` advisory (RFC 0020 B4, #3786).

Real git throughout: the check reads local ``origin/*`` refs, so it needs a
real remote-tracking layout. Git calls are recorded through
``patch_git_bounded``, which still runs the real function, so no raw git
appears here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import cw.doctor as doctor_pkg
import cw.doctor.epic_base as doctor_epic_base
from cw import merge_probe
from cw.doctor import _deps
from cw.doctor.core import _epic_base_checks
from cw.doctor.epic_base import _check_epic_base
from cw.merge_probe import PROBE_TIMEOUT_SECONDS
from cw.models import QueueItemStatus
from cw.models.events import PrState
from tests._shared.base_branch_builders import (
    base_row,
    client_for_repo,
    epic_repo_with_origin,
    patch_git_bounded,
)
from tests._shared.git_repos import commit_tracked_file, commit_tracked_file_at, git_in
from tests._worktree_helpers import FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cw.doctor._shared import CheckResult
    from cw.models import ClientConfig, TicketTask

BASE = "epic/x"
FILES = (("big.py", 50), ("uv.lock", 40))
# base_work.py (one line) plus the two sized files: the three-dot measure.
FULL = 1 + sum(n for _, n in FILES)
T_OLD = 1_700_000_000
T_NEW = T_OLD + 3600
DAY = 86_400
HOUR = 3_600
REQUIRED = PrState(state="OPEN", review_decision="REVIEW_REQUIRED")


def _clients(repo: Path, **extra: object) -> dict[str, ClientConfig]:
    return {"acme": client_for_repo(repo, **extra)}


def _row(base: str | None = BASE, **overrides: object) -> TicketTask:
    return base_row(base, client="acme", **overrides)


def _check(
    clients: dict[str, ClientConfig],
    tasks: Sequence[TicketTask],
    *,
    now: float = float(T_OLD),
    clock: FakeClock | None = None,
    max_bases: int = 8,
    budget_s: float = 15.0,
) -> list[CheckResult]:
    return _check_epic_base(
        clients,
        tasks,
        now=now,
        max_bases=max_bases,
        budget_s=budget_s,
        monotonic=FakeClock() if clock is None else clock,
    )


def _only(results: list[CheckResult]) -> CheckResult:
    assert len(results) == 1, results
    return results[0]


@pytest.fixture
def epic(make_git_repo: Callable[..., Path]) -> Path:
    repo, _ = epic_repo_with_origin(make_git_repo, BASE, files=FILES)
    return repo


# -- grouping ---------------------------------------------------------------


class TestGrouping:
    def test_one_result_per_live_base_with_the_slash_intact(self, epic: Path) -> None:
        tasks = [_row(ticket_id="T-1"), _row(ticket_id="T-2")]

        results = _check(_clients(epic), tasks)

        assert [r.name for r in results] == ["epic-base/acme/epic/x"]

    def test_pending_and_running_on_one_base_give_one_result(self, epic: Path) -> None:
        tasks = [
            _row(ticket_id="T-1", status=QueueItemStatus.PENDING),
            _row(ticket_id="T-2", status=QueueItemStatus.RUNNING),
        ]

        assert len(_check(_clients(epic), tasks)) == 1

    def test_null_base_unknown_client_and_terminal_only_yield_nothing(
        self, epic: Path
    ) -> None:
        tasks = [
            _row(None),
            base_row(BASE, client="ghost"),
            _row(status=QueueItemStatus.COMPLETED),
        ]

        assert _check(_clients(epic), tasks) == []

    def test_results_sort_by_client_then_base(self, epic: Path) -> None:
        tasks = [_row("zz"), _row("aa"), _row("mm")]

        names = [r.name for r in _check(_clients(epic), tasks)]

        assert names == [
            "epic-base/acme/aa",
            "epic-base/acme/mm",
            "epic-base/acme/zz",
        ]


# -- measurements -----------------------------------------------------------


class TestMeasurements:
    def test_three_dot_line_count_counts_the_epic_only(self, epic: Path) -> None:
        detail = _only(_check(_clients(epic), [_row()])).detail

        assert f"diff {FULL} lines" in detail

    def test_ignored_paths_are_excluded_from_the_count(self, epic: Path) -> None:
        clients = _clients(epic, merge_gate_ignore_paths=["uv.lock"])

        detail = _only(_check(clients, [_row()])).detail

        assert f"diff {FULL - 40} lines" in detail

    def test_age_is_the_oldest_unlanded_committer_time(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, _ = epic_repo_with_origin(make_git_repo, BASE, files=FILES)
        git_in(repo, "checkout", BASE)
        commit_tracked_file_at(repo, "dated_a.py", T_OLD)
        commit_tracked_file_at(repo, "dated_b.py", T_NEW)
        git_in(repo, "push", "origin", BASE)
        git_in(repo, "checkout", "main")

        now = T_OLD + 3 * DAY + 5 * HOUR
        detail = _only(_check(_clients(repo), [_row()], now=float(now))).detail

        assert "oldest unlanded commit 3 days 5 hours" in detail

    def test_clean_probe_for_a_disjoint_change(self, epic: Path) -> None:
        result = _only(_check(_clients(epic), [_row()]))

        assert "merge probe (main into epic/x): clean" in result.detail
        assert result.warn is False

    def test_conflict_names_the_files_and_warns(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, _ = epic_repo_with_origin(make_git_repo, BASE, files=FILES)
        git_in(repo, "checkout", "main")
        commit_tracked_file(repo, "shared.py", "main = 1\n")
        git_in(repo, "push", "origin", "main")
        git_in(repo, "checkout", BASE)
        commit_tracked_file(repo, "shared.py", "epic = 1\n")
        git_in(repo, "push", "origin", BASE)
        git_in(repo, "checkout", "main")

        result = _only(_check(_clients(repo), [_row()]))

        assert "merge probe (main into epic/x): conflict: shared.py" in result.detail
        assert result.warn is True

    def test_up_to_date_when_the_epic_already_has_main(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo, _ = epic_repo_with_origin(make_git_repo, BASE, files=FILES)
        git_in(repo, "checkout", BASE)
        git_in(repo, "merge", "--no-edit", "origin/main")
        git_in(repo, "push", "origin", BASE)
        git_in(repo, "checkout", "main")

        result = _only(_check(_clients(repo), [_row()]))

        assert "merge probe (main into epic/x): up to date" in result.detail
        assert result.warn is False


# -- ceiling ----------------------------------------------------------------


class TestCeiling:
    def test_unset_ceiling_says_none_configured_and_does_not_warn(
        self, epic: Path
    ) -> None:
        result = _only(_check(_clients(epic), [_row()]))

        assert "no epic ceiling configured" in result.detail
        assert result.warn is False

    def test_over_the_ceiling_says_over_and_warns(self, epic: Path) -> None:
        clients = _clients(epic, epic={"max_diff_lines": FULL - 1})

        result = _only(_check(clients, [_row()]))

        assert f"ceiling {FULL - 1}: OVER" in result.detail
        assert "land the epic now" in result.detail
        assert result.warn is True

    def test_equal_to_the_ceiling_is_not_over(self, epic: Path) -> None:
        clients = _clients(epic, epic={"max_diff_lines": FULL})

        result = _only(_check(clients, [_row()]))

        assert f"ceiling {FULL}: within" in result.detail
        assert result.warn is False


# -- fail open --------------------------------------------------------------


class TestFailOpen:
    def test_missing_repo_path_renders_unknown_and_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        result = _only(_check(_clients(tmp_path / "gone"), [_row()]))

        assert result.ok is True
        assert "diff unknown (" in result.detail
        assert "merge probe (main into epic/x): unknown (" in result.detail
        assert result.warn is False

    def test_absent_base_is_unknown_and_skips_the_remaining_calls(
        self, epic: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = patch_git_bounded(monkeypatch, doctor_epic_base, merge_probe)

        result = _only(_check(_clients(epic), [_row("epic/missing")]))

        assert "unknown (origin/epic/missing does not resolve)" in result.detail
        assert [argv[0] for argv, _ in calls] == ["rev-parse"]
        assert result.warn is False

    def test_a_git_failure_on_log_renders_only_that_field_unknown(
        self, epic: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_git_bounded(monkeypatch, doctor_epic_base, fail_verbs=("log",))

        result = _only(_check(_clients(epic), [_row()]))

        assert "oldest unlanded commit unknown (git log timed out" in result.detail
        assert f"diff {FULL} lines" in result.detail


# -- cap and budget ---------------------------------------------------------


class TestCapAndBudget:
    def test_cap_measures_the_first_bases_and_advises_the_rest(
        self, epic: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = patch_git_bounded(monkeypatch, doctor_epic_base, merge_probe)
        tasks = [_row("epic/a"), _row("epic/b"), _row("epic/c")]

        results = _check(_clients(epic), tasks, max_bases=2)

        capped = results[2]
        assert capped.name == "epic-base/acme/epic/c"
        assert "not measured (per-run cap of 2 bases)" in capped.detail
        assert not any("epic/c" in " ".join(argv) for argv, _ in calls)

    def test_budget_exhaustion_stops_later_bases(
        self, epic: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        calls = patch_git_bounded(
            monkeypatch,
            doctor_epic_base,
            merge_probe,
            clock=clock,
            step=20.0,
        )
        tasks = [_row(BASE), _row("epic/y")]

        results = _check(_clients(epic), tasks, clock=clock, budget_s=15.0)

        assert "not measured (time budget exhausted)" in results[1].detail
        assert calls
        for _, timeout in calls:
            assert timeout <= PROBE_TIMEOUT_SECONDS
            assert timeout <= 15.0


# -- no mutation, no fetch --------------------------------------------------


class TestNoMutation:
    def test_repo_state_is_unchanged_and_only_read_verbs_run(
        self, epic: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _snapshot() -> tuple[str, str, str, str]:
            return (
                git_in(epic, "status", "--porcelain"),
                git_in(epic, "for-each-ref"),
                git_in(epic, "ls-files", "-s"),
                git_in(epic, "rev-parse", "HEAD"),
            )

        before = _snapshot()
        calls = patch_git_bounded(monkeypatch, doctor_epic_base, merge_probe)

        _check(_clients(epic), [_row()])

        assert _snapshot() == before
        verbs = {argv[0] for argv, _ in calls}
        assert verbs <= {"rev-parse", "diff", "log", "merge-base", "merge-tree"}


# -- misconfiguration -------------------------------------------------------


class TestMisconfiguration:
    def test_a_completed_child_awaiting_review_on_the_base_warns(
        self, epic: Path
    ) -> None:
        tasks = [
            _row(ticket_id="T-1", status=QueueItemStatus.COMPLETED, pr_state=REQUIRED),
            _row(ticket_id="T-2", status=QueueItemStatus.RUNNING),
        ]

        result = _only(_check(_clients(epic), tasks))

        assert result.warn is True
        assert "1 child PR(s) (T-1)" in result.detail
        assert "branch protection on epic/x likely requires a human review" in (
            result.detail
        )
        assert "RFC 0020 D-12" in result.detail

    @pytest.mark.parametrize(
        "pr_state",
        [
            PrState(state="OPEN", review_decision="REVIEW_REQUIRED", is_draft=True),
            PrState(state="MERGED", review_decision="REVIEW_REQUIRED"),
            PrState(state="OPEN", review_decision=""),
            PrState(state="OPEN", review_decision="APPROVED"),
            None,
        ],
    )
    def test_other_states_are_not_flagged(
        self, epic: Path, pr_state: PrState | None
    ) -> None:
        tasks = [_row(ticket_id="T-1", pr_state=pr_state)]

        result = _only(_check(_clients(epic), tasks))

        assert "branch protection" not in result.detail
        assert result.warn is False

    def test_a_flagged_row_on_another_base_is_not_counted(self, epic: Path) -> None:
        tasks = [
            _row(ticket_id="T-1"),
            _row(
                "epic/other",
                ticket_id="T-2",
                status=QueueItemStatus.COMPLETED,
                pr_state=REQUIRED,
            ),
        ]

        result = _only(_check(_clients(epic), tasks))

        assert "branch protection" not in result.detail
        assert result.warn is False


# -- wiring -----------------------------------------------------------------


class TestWiring:
    @pytest.mark.parametrize(
        "error",
        [
            OSError("no queue"),
            json.JSONDecodeError("bad", "{", 0),
            ValidationError.from_exception_data("DevQueue", []),
        ],
    )
    def test_checks_degrade_to_nothing_when_the_queue_will_not_load(
        self,
        epic: Path,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        def _raise() -> object:
            raise error

        monkeypatch.setattr(_deps, "load_dev_queue", _raise)

        assert _epic_base_checks(_clients(epic)) == []

    def test_the_package_re_exports_the_check(self) -> None:
        assert doctor_pkg._check_epic_base is _check_epic_base
