"""Tests for ``cw.dispatch.branch_freshness``: the REVIEW conflict probe (#1823, #2979).

Real ``git`` repos built in ``tmp_path`` via the shared ``make_git_repo``
fixture, with ``origin`` pointed at the repo itself so ``origin/<default>`` is a
resolvable ref without a network. Only a merge the probe reports as a conflict
is returned; every other state, and every failure, is fail open (no park, never
a raise), because a false positive parks a healthy ticket for an operator.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from cw import merge_probe as probe_mod
from cw.dispatch import branch_freshness
from cw.dispatch.branch_freshness import (
    _MAX_PROBES_PER_TICK,
    _SWEEP_BUDGET_SECONDS,
    has_conflicting_branch,
    precomputed_branch_conflicts,
    resolve_conflicting_branches,
)
from cw.merge_probe import MergeProbe
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from tests.conftest import _make_ticket_task, git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask

_BRANCH = "dev/1823"
_SHARED_FILE = "shared.txt"
_BRANCH_ONLY_FILE = "branch_only.txt"
_CLIENT = "acme"
_TICKET = "1823"


def _write_commit(repo: Path, name: str, body: str, message: str) -> None:
    """Write *body* to *name* under *repo* and commit it."""
    (repo / name).write_text(body, encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-m", message)


def _seed_repo(make_git_repo: Callable[..., Path], name: str) -> Path:
    """Self-origin repo carrying both fixture files on ``main``/``origin/main``."""
    repo = make_git_repo(name)
    git_in(repo, "remote", "add", "origin", str(repo))
    _write_commit(repo, _SHARED_FILE, "one\ntwo\nthree\nfour\nfive\n", "seed shared")
    _write_commit(repo, _BRANCH_ONLY_FILE, "base\n", "seed branch-only")
    git_in(repo, "fetch", "origin", "main")
    return repo


def _behind_repo(
    make_git_repo: Callable[..., Path],
    name: str,
    *,
    branch_file: str,
    branch_body: str,
    main_body: str,
) -> Path:
    """Repo whose ``dev/1823`` is behind an ``origin/main`` that moved."""
    repo = _seed_repo(make_git_repo, name)
    git_in(repo, "checkout", "-b", _BRANCH)
    _write_commit(repo, branch_file, branch_body, "branch work")
    git_in(repo, "checkout", "main")
    _write_commit(repo, _SHARED_FILE, main_body, "main churn")
    git_in(repo, "fetch", "origin", "main")
    git_in(repo, "checkout", _BRANCH)
    return repo


def _conflicting(make_git_repo: Callable[..., Path], name: str = "bf") -> Path:
    return _behind_repo(
        make_git_repo,
        name,
        branch_file=_SHARED_FILE,
        branch_body="BRANCH\ntwo\nthree\nfour\nfive\n",
        main_body="MAIN\ntwo\nthree\nfour\nfive\n",
    )


def _event(ticket_id: str = _TICKET) -> OrchestratorEvent:
    return OrchestratorEvent(
        type=OrchestratorEventType.SESSION_COMPLETED,
        payload={"ticket_id": ticket_id},
    )


def _row(
    worktree: Path | None,
    *,
    ticket_id: str = _TICKET,
    status: QueueItemStatus = QueueItemStatus.RUNNING,
    stage: Stage = Stage.REVIEW,
) -> TicketTask:
    return _make_ticket_task(
        ticket_id=ticket_id,
        client=_CLIENT,
        status=status,
        stage=stage,
        worktree_path=worktree,
    )


def _clients(tmp_path: Path) -> dict[str, ClientConfig]:
    return {
        _CLIENT: ClientConfig(
            name=_CLIENT, workspace_path=tmp_path, default_branch="main"
        )
    }


def _sweep(
    tmp_path: Path, *rows: TicketTask, events: list[OrchestratorEvent] | None = None
) -> frozenset[tuple[str, str]]:
    return resolve_conflicting_branches(
        events if events is not None else [_event()],
        DevQueueStore(tasks=list(rows)),
        _clients(tmp_path),
    )


class TestResolveConflictingBranches:
    def test_a_conflicting_branch_is_reported(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        repo = _conflicting(make_git_repo)

        assert _sweep(tmp_path, _row(repo)) == {(_CLIENT, _TICKET)}

    def test_a_behind_branch_with_a_clean_overlapping_merge_is_not(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        """The inversion of #1823's overlap rule: same file, textually clean."""
        repo = _behind_repo(
            make_git_repo,
            "bf-clean-overlap",
            branch_file=_SHARED_FILE,
            branch_body="ONE\ntwo\nthree\nfour\nfive\n",
            main_body="one\ntwo\nthree\nfour\nFIVE\n",
        )

        assert _sweep(tmp_path, _row(repo)) == frozenset()

    def test_a_behind_branch_with_disjoint_churn_is_not(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        repo = _behind_repo(
            make_git_repo,
            "bf-disjoint",
            branch_file=_BRANCH_ONLY_FILE,
            branch_body="branch work\n",
            main_body="main churn\n",
        )

        assert _sweep(tmp_path, _row(repo)) == frozenset()

    def test_an_up_to_date_branch_is_not(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        repo = _conflicting(make_git_repo)
        git_in(repo, "reset", "--hard", "origin/main")

        assert _sweep(tmp_path, _row(repo)) == frozenset()

    def test_a_branch_ahead_only_is_not(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        repo = _seed_repo(make_git_repo, "bf-ahead")
        git_in(repo, "checkout", "-b", _BRANCH)
        _write_commit(repo, _BRANCH_ONLY_FILE, "ahead\n", "ahead")

        assert _sweep(tmp_path, _row(repo)) == frozenset()


class TestFailOpen:
    @pytest.mark.parametrize("missing", ["none", "nonexistent"])
    def test_a_missing_worktree_is_skipped_without_a_git_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        missing: str,
    ) -> None:
        """Pins Discovery 3: a ``None`` ``worktree_path`` (every dispatch row)."""
        calls: list[object] = []
        monkeypatch.setattr(
            branch_freshness, "probe_default_merge", lambda *a: calls.append(a)
        )
        path = None if missing == "none" else tmp_path / "absent"

        assert _sweep(tmp_path, _row(path)) == frozenset()
        assert calls == []

    def test_a_missing_default_ref_is_not_a_conflict(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        repo = make_git_repo("bf-no-origin")

        assert _sweep(tmp_path, _row(repo)) == frozenset()

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(OSError("git vanished"), id="oserror"),
            pytest.param(subprocess.TimeoutExpired(["git"], 10.0), id="timeout"),
        ],
    )
    def test_a_git_failure_is_not_a_conflict(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
    ) -> None:
        repo = _conflicting(make_git_repo)

        def _raise(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise failure

        monkeypatch.setattr("cw.merge_probe.run_git", _raise)

        assert _sweep(tmp_path, _row(repo)) == frozenset()

    def test_an_error_exit_is_not_a_conflict(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _conflicting(make_git_repo)
        real = probe_mod.run_git

        def _rc2(
            argv: list[str],
            *,
            cwd: Path | None = None,
            timeout: float | None = None,
            **_rest: object,
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "merge-tree":
                return subprocess.CompletedProcess(argv, 2, "", "boom")
            return real(argv, cwd=cwd, capture_output=True, timeout=timeout)

        monkeypatch.setattr(probe_mod, "run_git", _rc2)

        assert _sweep(tmp_path, _row(repo)) == frozenset()

    def test_an_unavailable_probe_is_logged_and_does_not_park(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = _conflicting(make_git_repo)
        unavailable = MergeProbe("unavailable", None, (), "git is too old")
        monkeypatch.setattr(
            branch_freshness, "probe_default_merge", lambda *_a: unavailable
        )

        with caplog.at_level("INFO", logger="cw.dispatch"):
            found = _sweep(tmp_path, _row(repo))

        assert found == frozenset()
        lines = [r.getMessage() for r in caplog.records if "unavailable" in r.message]
        assert lines == [
            f"branch_freshness: probe unavailable for {_CLIENT}/{_TICKET} "
            "(git is too old); it does not park"
        ]

    def test_an_unknown_client_is_skipped(
        self, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = _conflicting(make_git_repo)

        found = resolve_conflicting_branches(
            [_event()], DevQueueStore(tasks=[_row(repo)]), {}
        )

        assert found == frozenset()


class TestCandidates:
    @pytest.mark.parametrize(
        ("status", "stage", "ticket_id"),
        [
            pytest.param(QueueItemStatus.PENDING, Stage.REVIEW, _TICKET, id="pending"),
            pytest.param(QueueItemStatus.RUNNING, Stage.IMPL, _TICKET, id="impl-stage"),
            pytest.param(QueueItemStatus.RUNNING, Stage.REVIEW, "999", id="no-event"),
        ],
    )
    def test_only_running_review_rows_named_by_an_event_are_probed(
        self,
        make_git_repo: Callable[..., Path],
        tmp_path: Path,
        status: QueueItemStatus,
        stage: Stage,
        ticket_id: str,
    ) -> None:
        repo = _conflicting(make_git_repo)

        found = _sweep(
            tmp_path, _row(repo, ticket_id=ticket_id, status=status, stage=stage)
        )

        assert found == frozenset()

    def test_an_event_without_a_ticket_id_names_nothing(
        self, make_git_repo: Callable[..., Path], tmp_path: Path
    ) -> None:
        repo = _conflicting(make_git_repo)
        blank = OrchestratorEvent(
            type=OrchestratorEventType.SESSION_COMPLETED, payload={}
        )

        assert _sweep(tmp_path, _row(repo), events=[blank]) == frozenset()


class TestBounds:
    @staticmethod
    def _rows(tmp_path: Path, count: int) -> list[TicketTask]:
        rows = []
        for index in range(count):
            path = tmp_path / f"wt-{index}"
            path.mkdir()
            rows.append(_row(path, ticket_id=str(index)))
        return rows

    def test_the_probe_cap_is_enforced_and_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        rows = self._rows(tmp_path, _MAX_PROBES_PER_TICK + 5)
        probed: list[Path] = []

        def _conflict(worktree: Path, _base: str) -> MergeProbe:
            probed.append(worktree)
            return MergeProbe("conflict", "a" * 40, ("x",), "")

        monkeypatch.setattr(branch_freshness, "probe_default_merge", _conflict)
        events = [_event(row.ticket_id) for row in rows]

        with caplog.at_level("INFO", logger="cw.dispatch"):
            found = _sweep(tmp_path, *rows, events=events)

        assert len(probed) == _MAX_PROBES_PER_TICK
        assert len(found) == _MAX_PROBES_PER_TICK
        assert any(
            "5 REVIEW branch probe(s) skipped" in r.message for r in caplog.records
        )

    def test_the_wall_clock_budget_skips_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = self._rows(tmp_path, 3)
        ticks = iter(
            [0.0, 0.0, _SWEEP_BUDGET_SECONDS + 1.0, _SWEEP_BUDGET_SECONDS + 2.0]
        )
        probed: list[Path] = []

        def _conflict(worktree: Path, _base: str) -> MergeProbe:
            probed.append(worktree)
            return MergeProbe("conflict", "a" * 40, ("x",), "")

        monkeypatch.setattr(branch_freshness, "probe_default_merge", _conflict)

        found = resolve_conflicting_branches(
            [_event(row.ticket_id) for row in rows],
            DevQueueStore(tasks=rows),
            _clients(tmp_path),
            clock=lambda: next(ticks),
        )

        assert probed == [rows[0].worktree_path]
        assert found == {(_CLIENT, rows[0].ticket_id)}


class TestPrecomputedVerdicts:
    def test_no_verdict_outside_the_scope(self) -> None:
        assert has_conflicting_branch(_CLIENT, _TICKET) is False

    def test_a_listed_pair_is_a_conflict_only_inside_the_scope(self) -> None:
        with precomputed_branch_conflicts(frozenset({(_CLIENT, _TICKET)})):
            assert has_conflicting_branch(_CLIENT, _TICKET) is True
            assert has_conflicting_branch(_CLIENT, "other") is False
            assert has_conflicting_branch("other", _TICKET) is False
        assert has_conflicting_branch(_CLIENT, _TICKET) is False

    def test_the_scope_resets_on_an_exception(self) -> None:
        def _boom() -> None:
            with precomputed_branch_conflicts(frozenset({(_CLIENT, _TICKET)})):
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            _boom()
        assert has_conflicting_branch(_CLIENT, _TICKET) is False

    def test_nesting_restores_the_outer_verdicts(self) -> None:
        with precomputed_branch_conflicts(frozenset({(_CLIENT, "outer")})):
            with precomputed_branch_conflicts(frozenset({(_CLIENT, "inner")})):
                assert has_conflicting_branch(_CLIENT, "inner") is True
                assert has_conflicting_branch(_CLIENT, "outer") is False
            assert has_conflicting_branch(_CLIENT, "outer") is True
