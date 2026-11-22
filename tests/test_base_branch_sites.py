"""Every S2 merge-target reader resolves the row's base (RFC 0020 S2, #3780).

Each site that measures, fetches, cuts, probes or merges against "the default
branch" on behalf of a queue row now asks :func:`cw.models.resolve_base_branch`
with that row in scope. Every site is pinned twice: a row carrying a base
(``epic/x``) must use it, and a null-base row (or no row) must resolve
byte-identically to ``client.default_branch``. The last group is the grep
acceptance as an AST guard: every remaining ``.default_branch`` read in the S2
packages is annotated as retained or is in the S3-deferred allowlist.
"""

from __future__ import annotations

import ast
import io
import logging
import subprocess
import tokenize
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from cw import native_daemon
from cw._git import run_git
from cw.dev_queue import add_ticket, load_dev_queue
from cw.dispatch import branch_freshness
from cw.dispatch import review_gates as rg_mod
from cw.exceptions import CwError, StaleWorktreeError, WorktreeError
from cw.merge_probe import MergeProbe
from cw.models import ClientConfig, CwState, LocalLivenessHandle, TicketTask
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import cancelled_row_probes, fix_dispatch
from cw.reconcile import local as local_mod
from cw.reconcile.cancelled_row_probes import (
    CancelledRowKey,
    cancelled_row_targets,
    capture_cancelled_row_probes,
    new_cancelled_row_probes,
)
from cw.reconcile.concierge.cancelled_row import _detect_cancelled_row_candidates
from cw.reconcile.harvest_synthesis import (
    HarvestFacts,
    HarvestFactsUnavailableError,
)
from cw.reconcile.review_recipes import changelog_repair, fix_agent, repair_engine
from cw.reconcile.review_recipes import mechanical_repair as mech_mod
from cw.reconcile.review_recipes._shared import RepoSlugs, ReviewRecipeCandidate
from cw.reconcile.review_recipes.changelog_repair import CHANGELOG_SPEC
from cw.reconcile.review_recipes.repair_engine import Staged
from cw.reconcile.review_recipes.repair_git import RepairStopError
from cw.repair_disposition import RepairDisposition
from cw.worktree import (
    FetchOutcome,
    FetchResult,
    RefreshOutcome,
    ReuseRefreshOptions,
    ReuseRefreshReport,
    TickFetchGate,
    _lifecycle,
    _refresh,
    create_worktree,
    unsaved_work_reason,
)
from cw.worktree._freshness import fetch_base_branch
from cw.worktree._occupancy import _Occupancy, _reuse_occupancy
from cw.worktree._refresh import _refresh_reused_worktree
from cw.worktree._scope import compute_branch_diff_scope
from cw.worktree._tick_fetch import BACKOFF_SKIP_REASON, BUDGET_SKIP_REASON
from tests._changelog_repair_helpers import (
    EXPECTED_SHA,
    make_client,
    make_head,
    make_prepared,
    make_repair_job,
    repair_task,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._reconcile_helpers import (
    _FIX_LOOP_CLIENT,
    _FIX_LOOP_TICKET,
    _local_git_worktree,
    _make_pending_fix_dispatch,
    _seed_task,
    install_stub_dispatch,
    make_fix_loop_client,
)
from tests._shared.dispatch_fixtures import (
    SWEEP_CLIENT,
    SWEEP_TICKET,
    sweep_event,
    sweep_resolve,
    sweep_row,
)
from tests._shared.git_repos import (
    add_bare_origin,
    add_diverged_base_origin,
    commit_tracked_file,
    git_in,
    push_commit_to_origin,
)
from tests._shared.session_seeding import (
    _make_daemon_session,
    _make_ticket_task,
    make_cancelled_row,
)
from tests._shared.workflow_text import _SRC_ROOT, _iter_src_files
from tests._worktree_helpers import FakeClock, hanging_run, patch_worktree
from tests._worktree_refresh_helpers import _seed_reuse

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorConfig
    from tests._changelog_repair_helpers import FakeGhHead

_BASE = "epic/x"
_BRANCH = "dev/3780"
_MARKER = "# stays on client.default_branch:"


# --- fetch_base_branch (R5) ------------------------------------------------


class TestFetchBaseBranch:
    def test_fetch_base_branch_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv: list[list[str]] = []

        def _run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            argv.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("cw.worktree._git.subprocess.run", _run)

        result = fetch_base_branch(
            ClientConfig(
                name="acme",
                workspace_path=tmp_path,
                worktree_base=tmp_path / "wt",
                default_branch="main",
            ),
            _BASE,
        )

        assert [cmd[1:] for cmd in argv] == [["fetch", "origin", _BASE, "--quiet"]]
        assert result.outcome is FetchOutcome.FETCHED

    def test_fetch_base_branch_timeout_is_failed_with_pinned_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        run = hanging_run(clock)
        monkeypatch.setattr("cw.worktree._git.subprocess.run", run)
        gate = TickFetchGate(warned=set(), clock=clock)

        with gate.tick():
            result = fetch_base_branch(
                ClientConfig(
                    name="acme",
                    workspace_path=tmp_path,
                    worktree_base=tmp_path / "wt",
                    default_branch="main",
                ),
                _BASE,
            )

        assert result.outcome is FetchOutcome.FAILED
        assert result.reason == f"git fetch origin {_BASE} --quiet timed out"
        assert run.timeouts == [gate.per_call_s]

    def test_fetch_base_branch_budget_spent_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        run = hanging_run(clock)
        monkeypatch.setattr("cw.worktree._git.subprocess.run", run)
        gate = TickFetchGate(warned=set(), clock=clock)
        gate.budget_s = 0.0

        with gate.tick():
            result = fetch_base_branch(
                ClientConfig(
                    name="acme",
                    workspace_path=tmp_path,
                    worktree_base=tmp_path / "wt",
                    default_branch="main",
                ),
                _BASE,
            )

        assert result.outcome is FetchOutcome.FAILED
        assert result.reason == BUDGET_SKIP_REASON
        assert run.argv == []

    def test_fetch_base_branch_backoff_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock()
        run = hanging_run(clock)
        monkeypatch.setattr("cw.worktree._git.subprocess.run", run)
        gate = TickFetchGate(warned=set(), clock=clock)
        client = ClientConfig(
            name="acme",
            workspace_path=tmp_path,
            worktree_base=tmp_path / "wt",
            default_branch="main",
        )

        with gate.tick():
            fetch_base_branch(client, _BASE)
            second = fetch_base_branch(client, _BASE)

        assert second.outcome is FetchOutcome.FAILED
        assert second.reason == BACKOFF_SKIP_REASON
        assert len(run.argv) == 1

    def test_fetch_base_branch_failure_warns_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _rc128(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: unreachable\n")

        monkeypatch.setattr("cw.worktree._git.subprocess.run", _rc128)
        gate = TickFetchGate(warned=set(), clock=FakeClock())
        client = ClientConfig(
            name="acme",
            workspace_path=tmp_path,
            worktree_base=tmp_path / "wt",
            default_branch="main",
        )

        with caplog.at_level(logging.WARNING), gate.tick():
            first = fetch_base_branch(client, _BASE)
            fetch_base_branch(client, _BASE)

        warnings = [r for r in caplog.records if "fatal: unreachable" in r.message]
        assert len(warnings) == 1
        assert gate.warned == {(client.name, FetchOutcome.FAILED, first.reason)}

    def test_fetch_base_branch_missing_ref_warns_and_is_branch_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stderr = f"fatal: couldn't find remote ref {_BASE}\n"

        def _absent(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 128, "", stderr)

        monkeypatch.setattr("cw.worktree._git.subprocess.run", _absent)
        gate = TickFetchGate(warned=set(), clock=FakeClock())
        client = ClientConfig(
            name="acme",
            workspace_path=tmp_path,
            worktree_base=tmp_path / "wt",
            default_branch="main",
        )

        with caplog.at_level(logging.WARNING), gate.tick():
            result = fetch_base_branch(client, _BASE)

        assert result.outcome is FetchOutcome.BRANCH_ABSENT
        assert any(
            r.levelno == logging.WARNING and "couldn't find remote ref" in r.message
            for r in caplog.records
        )
        assert gate.warned == {(client.name, FetchOutcome.BRANCH_ABSENT, result.reason)}


# --- worktree start point (R4, R8) ------------------------------------------


def _spy_git_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every argv ``cw.worktree._git`` runs, delegating to the real run."""
    real = subprocess.run
    argv: list[list[str]] = []

    def _spy(cmd: list[str], **kwargs: object) -> object:
        argv.append(list(cmd))
        return real(cmd, **kwargs)

    monkeypatch.setattr("cw.worktree._git.subprocess.run", _spy)
    return argv


def _fetches(argv: list[list[str]]) -> list[list[str]]:
    """The fetches among *argv*, without the leading ``git``."""
    return [cmd[1:] for cmd in argv if cmd[1:2] == ["fetch"]]


def _worktree_add(argv: list[list[str]]) -> list[str]:
    (cmd,) = [cmd for cmd in argv if cmd[1:3] == ["worktree", "add"]]
    return cmd


def _base_row(base: str | None = _BASE) -> TicketTask:
    return _make_ticket_task(ticket_id="3780", client="acme", base_branch=base)


class TestWorktreeStartPoint:
    def test_new_branch_cut_from_origin_base(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = make_git_repo("ws")
        add_diverged_base_origin(workspace, _BASE)
        base_tip = git_in(workspace, "rev-parse", f"origin/{_BASE}")
        argv = _spy_git_argv(monkeypatch)

        wt = create_worktree(
            ClientConfig(
                name="acme",
                workspace_path=workspace,
                worktree_base=tmp_path / "wt",
                default_branch="main",
            ),
            _BRANCH,
            task=_base_row(),
        )

        assert git_in(wt, "rev-parse", "HEAD") == base_tip
        assert _worktree_add(argv)[-4:] == ["-b", _BRANCH, str(wt), f"origin/{_BASE}"]
        assert _fetches(argv) == [
            ["fetch", "origin", _BRANCH, "--quiet"],
            ["fetch", "origin", _BASE, "--quiet"],
        ]

    @pytest.mark.parametrize("row", [None, _base_row(None)], ids=["no-row", "null"])
    def test_new_branch_null_base_argv_unchanged(
        self,
        row: TicketTask | None,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = make_git_repo("ws")
        add_diverged_base_origin(workspace, _BASE)
        argv = _spy_git_argv(monkeypatch)

        wt = create_worktree(
            ClientConfig(
                name="acme",
                workspace_path=workspace,
                worktree_base=tmp_path / "wt",
                default_branch="main",
            ),
            _BRANCH,
            task=row,
        )

        assert _worktree_add(argv)[-1] == "origin/main"
        assert _fetches(argv) == [["fetch", "origin", _BRANCH, "--quiet"]]
        assert git_in(wt, "rev-parse", "HEAD") == git_in(
            workspace, "rev-parse", "origin/main"
        )

    def test_missing_remote_base_raises_worktree_error(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        workspace = make_git_repo("ws")
        add_bare_origin(workspace)
        client = ClientConfig(
            name="acme",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
            default_branch="main",
        )

        with pytest.raises(WorktreeError, match=r"base branch 'epic/x' does not exist"):
            create_worktree(client, _BRANCH, task=_base_row())

        assert not (tmp_path / "wt" / "dev" / "3780").exists()
        assert not any((tmp_path / "wt").rglob("*.git"))

    def test_base_fetch_failure_fails_open_to_stale_ref(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = make_git_repo("ws")
        add_diverged_base_origin(workspace, _BASE)
        stale_tip = git_in(workspace, "rev-parse", f"origin/{_BASE}")
        monkeypatch.setattr(
            _lifecycle,
            "fetch_base_branch",
            lambda *_a: FetchResult(FetchOutcome.FAILED, "rc=128: fatal: offline"),
        )

        wt = create_worktree(
            ClientConfig(
                name="acme",
                workspace_path=workspace,
                worktree_base=tmp_path / "wt",
                default_branch="main",
            ),
            _BRANCH,
            task=_base_row(),
        )

        assert git_in(wt, "rev-parse", "HEAD") == stale_tip

    def test_base_fetch_failure_without_ref_raises(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = make_git_repo("ws")
        add_bare_origin(workspace)
        monkeypatch.setattr(
            _lifecycle,
            "fetch_base_branch",
            lambda *_a: FetchResult(FetchOutcome.FAILED, "rc=128: fatal: offline"),
        )

        with pytest.raises(WorktreeError, match=r"neither 'origin/epic/x' nor"):
            create_worktree(
                ClientConfig(
                    name="acme",
                    workspace_path=workspace,
                    worktree_base=tmp_path / "wt",
                    default_branch="main",
                ),
                _BRANCH,
                task=_base_row(),
            )

    @pytest.mark.parametrize(
        ("row", "expected"),
        [(_base_row(), _BASE), (_base_row(None), "main")],
        ids=["base", "null"],
    )
    def test_start_point_falls_back_to_local_base_ref(
        self,
        row: TicketTask,
        expected: str,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = make_git_repo("ws")
        add_diverged_base_origin(workspace, _BASE)
        git_in(workspace, "update-ref", "-d", f"refs/remotes/origin/{_BASE}")
        git_in(workspace, "update-ref", "-d", "refs/remotes/origin/main")
        monkeypatch.setattr(
            _lifecycle,
            "fetch_base_branch",
            lambda *_a: FetchResult(FetchOutcome.FAILED, "rc=128: fatal: offline"),
        )
        argv = _spy_git_argv(monkeypatch)

        create_worktree(
            ClientConfig(
                name="acme",
                workspace_path=workspace,
                worktree_base=tmp_path / "wt",
                default_branch="main",
            ),
            _BRANCH,
            task=row,
        )

        assert _worktree_add(argv)[-1] == expected

    def test_create_worktree_without_task_ignores_base(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = make_git_repo("ws")
        add_diverged_base_origin(workspace, _BASE)
        base_fetches: list[str] = []
        monkeypatch.setattr(
            _lifecycle,
            "fetch_base_branch",
            lambda _c, base: base_fetches.append(base),
        )
        argv = _spy_git_argv(monkeypatch)

        create_worktree(
            ClientConfig(
                name="acme",
                workspace_path=workspace,
                worktree_base=tmp_path / "wt",
                default_branch="main",
            ),
            _BRANCH,
        )

        assert _worktree_add(argv)[-1] == "origin/main"
        assert base_fetches == []


# --- reuse refresh, _handle_branch_absent (R4, R5) ---------------------------


def _reuse_with_base(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> tuple[ClientConfig, Path, Path]:
    client, _wt, origin, workspace = _seed_reuse(tmp_path, make_git_repo)
    git_in(workspace, "push", "origin", f"main:{_BASE}")
    git_in(workspace, "fetch", "origin")
    return client, origin, workspace


def _record_fetch(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    result: FetchResult | None = None,
) -> list[tuple[object, ...]]:
    real = getattr(_refresh, name)
    calls: list[tuple[object, ...]] = []

    def _rec(*args: object) -> FetchResult:
        calls.append(args)
        return result if result is not None else real(*args)

    monkeypatch.setattr(_refresh, name, _rec)
    return calls


class TestBranchAbsentRefresh:
    def test_branch_absent_base_row_fetches_base_not_default(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, origin, _ws = _reuse_with_base(tmp_path, make_git_repo)
        row = _base_row()
        wt = create_worktree(client, "dev/never", task=row)
        new_tip = push_commit_to_origin(origin, _BASE, tmp_path / "side", "adv.txt")
        base_calls = _record_fetch(monkeypatch, "fetch_base_branch")
        default_calls = _record_fetch(monkeypatch, "fetch_default_branch")
        report = ReuseRefreshReport()

        create_worktree(
            client,
            "dev/never",
            allow_dirty_reuse=True,
            refresh=ReuseRefreshOptions(report=report),
            task=row,
        )

        assert base_calls == [(client, _BASE)]
        assert default_calls == []
        assert report.outcome is RefreshOutcome.REFRESHED
        assert git_in(wt, "rev-parse", "HEAD") == new_tip

    def test_branch_absent_null_base_still_uses_fetch_default_branch(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _origin, _ws = _reuse_with_base(tmp_path, make_git_repo)
        row = _base_row(None)
        create_worktree(client, "dev/never", task=row)
        base_calls = _record_fetch(monkeypatch, "fetch_base_branch")
        default_calls = _record_fetch(monkeypatch, "fetch_default_branch")

        create_worktree(
            client,
            "dev/never",
            allow_dirty_reuse=True,
            refresh=ReuseRefreshOptions(),
            task=row,
        )

        assert default_calls == [(client,)]
        assert base_calls == []

    def test_branch_absent_base_fetch_failure_notes_base_and_does_not_raise(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _origin, _ws = _reuse_with_base(tmp_path, make_git_repo)
        row = _base_row()
        create_worktree(client, "dev/never", task=row)
        _record_fetch(
            monkeypatch,
            "fetch_base_branch",
            FetchResult(FetchOutcome.FAILED, "rc=128: fatal: simulated"),
        )
        report = ReuseRefreshReport()

        create_worktree(
            client,
            "dev/never",
            allow_dirty_reuse=True,
            refresh=ReuseRefreshOptions(report=report),
            task=row,
        )

        assert report.outcome is RefreshOutcome.NOT_REFRESHED
        assert len(report.notes) == 1
        assert f"fetching origin/{_BASE} to check" in report.notes[0]
        assert "fatal: simulated" in report.notes[0]

    def test_branch_absent_commits_of_own_note_names_base(
        self,
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _origin, _ws = _reuse_with_base(tmp_path, make_git_repo)
        row = _base_row()
        wt = create_worktree(client, "dev/never", task=row)
        git_in(wt, "commit", "--allow-empty", "-m", "own unpushed work")
        monkeypatch.setattr(
            "cw.worktree._occupancy._reuse_occupancy",
            lambda *_a, **_kw: _Occupancy(live=None, branch_mismatch=None, local=None),
        )
        report = ReuseRefreshReport()

        result = _refresh_reused_worktree(
            client,
            "dev/never",
            wt,
            report,
            ticket_id=None,
            daemon=native_daemon.get_native_daemon_client(),
            task=row,
        )

        assert result.outcome is RefreshOutcome.NOT_REFRESHED
        assert f"commits of its own beyond origin/{_BASE}" in report.notes[0]

    def test_occupancy_unsaved_check_uses_base(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        workspace = make_git_repo("ws")
        add_diverged_base_origin(workspace, _BASE)
        client = ClientConfig(
            name="acme",
            workspace_path=workspace,
            worktree_base=tmp_path / "wt",
            default_branch="main",
        )
        wt = create_worktree(client, _BRANCH, task=_base_row())
        git_in(wt, "branch", "--unset-upstream")
        daemon = FakeNativeDaemonClient()

        base = _reuse_occupancy(client, _BRANCH, wt, daemon=daemon, task=_base_row())
        null = _reuse_occupancy(client, _BRANCH, wt, daemon=daemon)

        assert base.local is None
        assert null.local is not None
        assert "ahead of origin/main" in null.local


# --- unsaved work (R4) ------------------------------------------------------


def _base_cut_worktree(
    tmp_path: Path, make_git_repo: Callable[..., Path]
) -> tuple[ClientConfig, Path, Path]:
    workspace = make_git_repo("ws")
    add_diverged_base_origin(workspace, _BASE)
    client = ClientConfig(
        name="acme",
        workspace_path=workspace,
        worktree_base=tmp_path / "wt",
        default_branch="main",
    )
    wt = create_worktree(client, _BRANCH, task=_base_row())
    git_in(wt, "branch", "--unset-upstream")
    return client, wt, workspace


class TestUnsavedWork:
    def test_unsaved_reason_base_row_clean_when_even_with_base(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, _wt, _ws = _base_cut_worktree(tmp_path, make_git_repo)

        assert unsaved_work_reason(client, _BRANCH, task=_base_row()) is None
        null = unsaved_work_reason(client, _BRANCH)
        assert null is not None
        assert null.startswith("1 commit(s) ahead of origin/main ")

    def test_unsaved_reason_names_base_ref_when_ahead(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, _ws = _base_cut_worktree(tmp_path, make_git_repo)
        commit_tracked_file(wt, "work.py")

        reason = unsaved_work_reason(client, _BRANCH, task=_base_row())

        assert reason is not None
        assert reason.startswith(f"1 commit(s) ahead of origin/{_BASE} ")

    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            (None, ["origin/main", "main"]),
            (_base_row(None), ["origin/main", "main"]),
            (_base_row(), [f"origin/{_BASE}", _BASE]),
        ],
        ids=["no-row", "null", "base"],
    )
    def test_unsaved_ladder_null_base_candidates_unchanged(
        self,
        row: TicketTask | None,
        expected: list[str],
        tmp_path: Path,
        make_git_repo: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _wt, _ws = _base_cut_worktree(tmp_path, make_git_repo)
        bases: list[str] = []

        def _ahead(base: str, _wt_path: Path) -> int | None:
            bases.append(base)
            return None

        patch_worktree(monkeypatch, "_commits_ahead", _ahead)

        reason = unsaved_work_reason(client, _BRANCH, task=row)

        assert bases == expected
        assert reason == "no base ref resolvable (offline or bare clone)"

    def test_unsaved_local_base_fallback(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        client, wt, workspace = _base_cut_worktree(tmp_path, make_git_repo)
        git_in(workspace, "update-ref", "-d", f"refs/remotes/origin/{_BASE}")

        assert unsaved_work_reason(client, _BRANCH, task=_base_row()) is None
        commit_tracked_file(wt, "work.py")
        reason = unsaved_work_reason(client, _BRANCH, task=_base_row())
        assert reason is not None
        assert reason.startswith(f"1 commit(s) ahead of {_BASE} ")


# --- empty-diff gate (review_gates) -----------------------------------------


class TestEmptyDiffGate:
    def test_empty_diff_gate_measures_against_base(
        self, tmp_path: Path, make_git_repo: Callable[..., Path]
    ) -> None:
        repo = make_git_repo("ws")
        add_diverged_base_origin(repo, _BASE)
        git_in(repo, "checkout", _BASE)
        clients = {
            "acme": ClientConfig(
                name="acme",
                workspace_path=repo,
                worktree_base=tmp_path / "wt",
                default_branch="main",
            )
        }
        base_row = _make_ticket_task(
            client="acme", base_branch=_BASE, worktree_path=repo
        )
        null_row = _make_ticket_task(client="acme", worktree_path=repo)

        assert rg_mod._should_gate_for_empty_diff(base_row, clients) is True
        assert rg_mod._should_gate_for_empty_diff(null_row, clients) is False

    def test_empty_diff_gate_null_base_passes_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        monkeypatch.setattr(
            rg_mod,
            "commits_ahead_of_default",
            lambda _path, base: seen.append(base) or 1,
        )
        clients = {
            "acme": ClientConfig(
                name="acme", workspace_path=tmp_path, default_branch="trunk"
            )
        }

        rg_mod._should_gate_for_empty_diff(_make_ticket_task(client="acme"), clients)
        rg_mod._should_gate_for_empty_diff(_base_row(), clients)

        assert seen == ["trunk", _BASE]

    def test_empty_diff_gate_unresolvable_client_returns_false_before_the_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seam = MagicMock(return_value=_BASE)
        monkeypatch.setattr(rg_mod, "resolve_base_branch", seam)

        assert rg_mod._should_gate_for_empty_diff(_base_row(), {}) is False
        seam.assert_not_called()


# --- branch_behind_main sweep (R3, R8) ---------------------------------------


def _record_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Path, str]]:
    probed: list[tuple[Path, str]] = []

    def _probe(worktree: Path, base: str) -> MergeProbe:
        probed.append((worktree, base))
        state = "conflict" if base == _BASE else "clean"
        return MergeProbe(state, "a" * 40, ("x",) if state == "conflict" else (), "")

    monkeypatch.setattr(branch_freshness, "probe_default_merge", _probe)
    return probed


def _wt_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


class TestBranchBehindMainSweep:
    def test_conflict_sweep_probes_row_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probed = _record_probe(monkeypatch)
        wt = _wt_dir(tmp_path, "wt")

        found = sweep_resolve(tmp_path, sweep_row(wt, base_branch=_BASE))

        assert found == {(SWEEP_CLIENT, SWEEP_TICKET)}
        assert probed == [(wt, _BASE)]

    def test_conflict_sweep_mixed_rows_probe_each_rows_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probed = _record_probe(monkeypatch)
        wt1, wt2 = _wt_dir(tmp_path, "wt1"), _wt_dir(tmp_path, "wt2")
        rows = (
            sweep_row(wt1, ticket_id="1", base_branch=_BASE),
            sweep_row(wt2, ticket_id="2"),
        )

        found = sweep_resolve(
            tmp_path, *rows, events=[sweep_event("1"), sweep_event("2")]
        )

        assert probed == [(wt1, _BASE), (wt2, "main")]
        assert found == {(SWEEP_CLIENT, "1")}

    def test_conflict_sweep_null_base_probe_arg_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probed = _record_probe(monkeypatch)
        wt = _wt_dir(tmp_path, "wt")

        assert sweep_resolve(tmp_path, sweep_row(wt)) == frozenset()
        assert probed == [(wt, "main")]

    def test_conflict_sweep_base_row_worktree_path_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_gate_worktree`` is inert for dispatch rows (no stamped path, #2990)."""
        probed = _record_probe(monkeypatch)

        found = sweep_resolve(tmp_path, sweep_row(None, base_branch=_BASE))

        assert found == frozenset()
        assert probed == []


# --- cancelled-row probe (R7, R8) --------------------------------------------


class TestCancelledRowProbe:
    @pytest.mark.parametrize(
        ("base", "expected"), [(_BASE, _BASE), (None, "dev")], ids=["base", "null"]
    )
    def test_cancelled_row_key_carries_resolved_base(
        self, base: str | None, expected: str, tmp_path: Path
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="dev"))

        keys = cancelled_row_targets([make_cancelled_row(tmp_path, base_branch=base)])

        assert keys == [CancelledRowKey("acme", "GEN-1", tmp_path, expected)]
        assert keys[0].base_branch == expected

    @pytest.mark.parametrize(
        ("base", "expected"), [(_BASE, _BASE), (None, "main")], ids=["base", "null"]
    )
    def test_cancelled_row_read_uses_base_ref(
        self,
        base: str | None,
        expected: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))
        calls: list[tuple[str, ...]] = []

        def _git(*args: str, **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(list(args), 0, "abc one\n", "")

        monkeypatch.setattr("cw.worktree._scope._run_git", _git)
        probes = new_cancelled_row_probes(budget_seconds=None)

        capture_cancelled_row_probes(
            [make_cancelled_row(tmp_path, base_branch=base)], probes=probes
        )

        assert calls == [("log", f"origin/{expected}..HEAD", "--oneline")]

    def test_cancelled_row_evidence_value_is_resolved_base_under_default_branch_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_clients_yaml(ClientSpec("acme", tmp_path, default_branch="main"))
        monkeypatch.setattr(
            cancelled_row_probes, "_has_commits_beyond_base", lambda *_a: True
        )
        row = make_cancelled_row(tmp_path, base_branch=_BASE)
        probes = new_cancelled_row_probes(budget_seconds=None)
        capture_cancelled_row_probes([row], probes=probes)

        (candidate,) = _detect_cancelled_row_candidates([row], probes)

        assert candidate.evidence["default_branch"] == _BASE


# --- harvest fork point (R3, R8) ---------------------------------------------

_HANDLE = LocalLivenessHandle(pid=2_000_000_000, start_time_ns=1)


def _harvest_target(
    monkeypatch: pytest.MonkeyPatch,
    worktree: Path,
    task: TicketTask,
    clients: dict[str, ClientConfig],
) -> None:
    session = _make_daemon_session(id="s-3780", client="client-a")
    monkeypatch.setattr(
        local_mod,
        "_git_harvest_targets",
        lambda *_a: [(session, _HANDLE, worktree, task)],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.load_effective_clients", lambda *_a, **_kw: clients
    )


class TestHarvestForkPoint:
    @pytest.mark.parametrize(
        ("clients", "row_base", "expected"),
        [
            ({"client-a": "trunk"}, _BASE, _BASE),
            ({"client-a": "trunk"}, None, "trunk"),
            ({}, None, "main"),
        ],
        ids=["base", "null", "no-client"],
    )
    def test_capture_local_harvest_facts_passes_row_base(
        self,
        clients: dict[str, str],
        row_base: str | None,
        expected: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        configs = {
            name: ClientConfig(name=name, workspace_path=tmp_path, default_branch=b)
            for name, b in clients.items()
        }
        row = _make_ticket_task(client="client-a", base_branch=row_base)
        _harvest_target(monkeypatch, tmp_path, row, configs)
        captured: list[str] = []
        monkeypatch.setattr(
            HarvestFacts,
            "capture",
            lambda _self, _sid, _handle, _wt, base: captured.append(base),
        )

        local_mod.capture_local_harvest_facts(CwState(), [], facts=HarvestFacts())

        assert captured == [expected]

    @pytest.mark.parametrize(
        ("row_base", "expected"), [(_BASE, _BASE), (None, "main")], ids=["base", "null"]
    )
    def test_harvest_fork_point_argv_uses_base(
        self,
        row_base: str | None,
        expected: str,
        tmp_path: Path,
        make_git_repo: Callable[[str], Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worktree = _local_git_worktree(make_git_repo, "wt-harvest", with_commit=True)
        git_in(worktree, "update-ref", f"refs/remotes/origin/{_BASE}", "HEAD~1")
        configs = {
            "client-a": ClientConfig(
                name="client-a", workspace_path=tmp_path, default_branch="main"
            )
        }
        row = _make_ticket_task(client="client-a", base_branch=row_base)
        _harvest_target(monkeypatch, worktree, row, configs)
        spy = MagicMock(wraps=run_git)
        monkeypatch.setattr("cw.local_runner.run_git", spy)

        local_mod.capture_local_harvest_facts(CwState(), [], facts=HarvestFacts())

        argvs = [call.args[0] for call in spy.call_args_list]
        assert [
            "-C",
            str(worktree),
            "merge-base",
            "HEAD",
            f"origin/{expected}",
        ] in argvs

    def test_harvest_lookup_identity_includes_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "cw.reconcile.harvest_synthesis.git_facts", lambda *_a: {"commits": []}
        )
        store = HarvestFacts()
        store.capture("s-3780", _HANDLE, tmp_path, _BASE)

        with pytest.raises(HarvestFactsUnavailableError, match="identity changed"):
            store.lookup("s-3780", _HANDLE, tmp_path, "main")
        assert store.lookup("s-3780", _HANDLE, tmp_path, _BASE) == {"commits": []}

    def test_harvest_synthetic_task_has_default_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configs = {
            "client-a": ClientConfig(
                name="client-a", workspace_path=tmp_path, default_branch="trunk"
            )
        }
        synthetic = TicketTask(ticket_id="", client="client-a")
        _harvest_target(monkeypatch, tmp_path, synthetic, configs)
        captured: list[str] = []
        monkeypatch.setattr(
            HarvestFacts,
            "capture",
            lambda _self, _sid, _handle, _wt, base: captured.append(base),
        )

        local_mod.capture_local_harvest_facts(CwState(), [], facts=HarvestFacts())

        assert captured == ["trunk"]


# --- repair engine (R3, R7) --------------------------------------------------


def _candidate(task: TicketTask) -> ReviewRecipeCandidate:
    assert task.pr_url is not None
    return ReviewRecipeCandidate(
        ticket_id=task.ticket_id,
        client=task.client,
        lane=task.lane,
        recipe=CHANGELOG_SPEC.recipe,
        attention_state="merge_blocked",
        pr_url=task.pr_url,
        evidence={},
        session_id=None,
    )


class TestRepairEngine:
    @pytest.mark.parametrize(
        ("base", "expected"), [(_BASE, _BASE), (None, "main")], ids=["base", "null"]
    )
    def test_prepare_repair_job_stamps_base(
        self, base: str | None, expected: str, tmp_path: Path
    ) -> None:
        client_cfg = make_client(tmp_path / "repo")
        task = repair_task(base_branch=base)

        job = repair_engine.prepare_repair_job(
            CHANGELOG_SPEC,
            task,
            _candidate(task),
            clients={task.client: client_cfg},
            repo_slugs=RepoSlugs(),
            guard_target=lambda _task, _clients: None,
            config=None,
            repeat_fire_counts=None,
        )

        assert job is not None
        assert job.base_branch == expected

    def test_prepare_repair_fetches_and_diffs_against_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_cfg = make_client(tmp_path / "repo")
        job = make_repair_job(client_cfg, task=repair_task(base_branch=_BASE))
        fetched: list[tuple[Path, str, str]] = []
        diffed: list[str] = []

        def _fetch(repo: Path, base: str, branch: str) -> str:
            fetched.append((repo, base, branch))
            return EXPECTED_SHA

        def _conflicts(
            _repo: Path, default_ref: str, _sha: str, **_kw: object
        ) -> tuple[frozenset[str], str]:
            diffed.append(default_ref)
            return frozenset({"CHANGELOG.md"}), "c" * 40

        monkeypatch.setattr(repair_engine, "_fetch_and_resolve", _fetch)
        monkeypatch.setattr(repair_engine, "_read_head", lambda *_a: make_head())
        monkeypatch.setattr(repair_engine, "_conflict_set", _conflicts)

        prep = repair_engine.prepare_repair(job, repair_engine.RepairState(job))

        assert fetched == [(tmp_path / "repo", _BASE, job.branch)]
        assert diffed == [f"refs/remotes/origin/{_BASE}"]
        assert prep.base_branch == _BASE
        assert prep.default_ref == f"refs/remotes/origin/{_BASE}"

    @pytest.mark.parametrize(
        ("row_base", "pr_base", "passes"),
        [
            (_BASE, "main", False),
            (_BASE, _BASE, True),
            (None, "main", True),
            (None, "other", False),
        ],
        ids=["base-pr-on-main", "base-pr-on-base", "null-main", "null-other"],
    )
    def test_read_head_requires_pr_base_equal_row_base(
        self,
        row_base: str | None,
        pr_base: str,
        passes: bool,
        tmp_path: Path,
        head: FakeGhHead,
    ) -> None:
        job = make_repair_job(
            make_client(tmp_path / "repo"), task=repair_task(base_branch=row_base)
        )
        head.base_ref_name = pr_base
        head.ref_name = job.branch

        if passes:
            assert repair_engine._read_head(job, job.branch, EXPECTED_SHA, tmp_path)
            return
        with pytest.raises(RepairStopError) as raised:
            repair_engine._read_head(job, job.branch, EXPECTED_SHA, tmp_path)
        assert raised.value.disposition is RepairDisposition.PR_HEAD_MISMATCH
        expected_base = row_base or "main"
        assert f"base={pr_base}" in str(raised.value)
        assert f"base={expected_base} (the row's base)" in str(raised.value)

    @pytest.mark.parametrize("base", [_BASE, "main"])
    def test_mechanical_repair_env_and_commit_message_use_base(
        self, base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_cfg = make_client(tmp_path / "repo")
        job = make_repair_job(client_cfg, task=repair_task(base_branch=base))
        prep = make_prepared(
            client_cfg,
            branch=job.branch,
            base_branch=base,
            default_ref=f"refs/remotes/origin/{base}",
        )
        envs: list[dict[str, str]] = []
        messages: list[str] = []

        def _commit(_scratch: Path, message: str) -> None:
            messages.append(message)
            raise RepairStopError(RepairDisposition.MERGE_FAILED, "stop here")

        monkeypatch.setattr(
            mech_mod,
            "_entry",
            lambda _cfg: SimpleNamespace(
                paths=("uv.lock",), repair_command=["true"], repair_timeout_seconds=5
            ),
        )
        monkeypatch.setattr(mech_mod, "path_in_scope", lambda _paths: lambda _p: True)
        monkeypatch.setattr(mech_mod, "_start_merge", lambda *_a: frozenset())
        monkeypatch.setattr(mech_mod, "resolve_argv", lambda argv, _repo: argv)
        monkeypatch.setattr(
            mech_mod, "run_repair", lambda *_a, env, **_kw: envs.append(env)
        )
        monkeypatch.setattr(mech_mod, "_stage_scoped", lambda *_a, **_kw: None)
        monkeypatch.setattr(mech_mod, "_scan_markers", lambda *_a: None)
        monkeypatch.setattr(mech_mod, "_commit_merge", _commit)

        with pytest.raises(RepairStopError):
            mech_mod._rewrite(
                job,
                prep,
                Staged(tmp_path / "arm", "", None),
                tmp_path,
                repair_engine.RepairState(job),
            )

        assert envs[0]["CW_REPAIR_DEFAULT_REF"] == f"refs/remotes/origin/{base}"
        assert messages[0].startswith(f"chore(merge): sync {base} and repair")

    @pytest.mark.parametrize("base", [_BASE, "main"])
    def test_changelog_rebuild_commit_message_names_base(
        self, base: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_cfg = make_client(tmp_path / "repo")
        job = make_repair_job(client_cfg, task=repair_task(base_branch=base))
        prep = make_prepared(client_cfg, branch=job.branch, base_branch=base)
        rebuilt: list[str] = []

        def _commit(_scratch: Path, _text: str, _ticket: str, onto: str) -> None:
            rebuilt.append(onto)
            raise RepairStopError(RepairDisposition.REBASE_FAILED, "stop here")

        monkeypatch.setattr(changelog_repair, "_rebase_onto", lambda *_a: None)
        monkeypatch.setattr(changelog_repair, "_remap_eligible", lambda _job: False)
        monkeypatch.setattr(changelog_repair, "_commit_rebuilt", _commit)
        staged = Staged(
            tmp_path / "arm", "", SimpleNamespace(main_text="", rebuilt="x")
        )

        with pytest.raises(RepairStopError):
            changelog_repair._rewrite_changelog(
                job, prep, staged, tmp_path, repair_engine.RepairState(job)
            )

        assert rebuilt == [base]


# --- fix agent (R4) ----------------------------------------------------------


class TestFixAgent:
    @pytest.mark.parametrize(
        ("row", "expected"),
        [(_base_row(), _BASE), (None, "main"), (_base_row(None), "main")],
        ids=["base", "no-row", "null"],
    )
    def test_probe_default_merge_probes_row_base(
        self,
        row: TicketTask | None,
        expected: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        probed: list[tuple[Path, str]] = []
        states = iter(["conflict", "unavailable"])

        def _probe(worktree: Path, base: str) -> MergeProbe:
            probed.append((worktree, base))
            return MergeProbe(next(states), None, ("a.py",), "no merge-base")

        monkeypatch.setattr(fix_agent, "probe_default_merge", _probe)
        client = ClientConfig(
            name="acme", workspace_path=tmp_path, default_branch="main"
        )

        with pytest.raises(CwError, match=f"merging origin/{expected} into dev/1"):
            fix_agent._probe_default_merge(client, "dev/1", tmp_path, task=row)
        note = fix_agent._probe_default_merge(client, "dev/1", tmp_path, task=row)

        assert probed == [(tmp_path, expected), (tmp_path, expected)]
        assert note is not None
        assert note.startswith(f"could not check whether origin/{expected} conflicts")

    def test_probe_default_merge_null_base_message_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fix_agent,
            "probe_default_merge",
            lambda *_a: MergeProbe("unavailable", None, (), "boom"),
        )

        note = fix_agent._probe_default_merge(
            ClientConfig(name="acme", workspace_path=tmp_path, default_branch="main"),
            "dev/1",
            tmp_path,
        )

        assert note == (
            "could not check whether origin/main conflicts with dev/1 (boom); "
            "no merge was attempted and the branch was left as is"
        )

    @pytest.mark.parametrize(
        ("row", "expected"),
        [(_base_row(), _BASE), (None, "main")],
        ids=["base", "null"],
    )
    def test_fetch_origin_bounded_degraded_note_names_base(
        self,
        row: TicketTask | None,
        expected: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock()
        monkeypatch.setattr("cw.worktree._git.subprocess.run", hanging_run(clock))
        gate = TickFetchGate(warned=set(), clock=clock)

        with gate.tick():
            note = fix_agent._fetch_origin_bounded(
                ClientConfig(
                    name="acme", workspace_path=tmp_path, default_branch="main"
                ),
                task=row,
            )

        assert note is not None
        assert f"the conflict check against origin/{expected} used" in note

    def test_dispatch_fix_agent_threads_task_to_create_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, object]] = []

        def _record(*_a: object, **kwargs: object) -> Path:
            seen.append(kwargs)
            msg = "stop after recording the call"
            raise WorktreeError(msg)

        monkeypatch.setattr(fix_agent, "create_worktree", _record)
        row = _base_row()

        with pytest.raises(WorktreeError, match="stop after recording"):
            fix_agent.dispatch_fix_agent(
                client=ClientConfig(
                    name="acme", workspace_path=tmp_path, default_branch="main"
                ),
                branch="dev/1",
                prompt="fix it",
                label="fix-1",
                ticket_id="3780",
                lane="default",
                parent="no-such-session",
                native_daemon=FakeNativeDaemonClient(),
                task=row,
            )

        assert len(seen) == 1
        assert seen[0]["task"] is row

    def test_fix_dispatch_job_carries_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = make_fix_loop_client(tmp_path, monkeypatch)
        _seed_task(base_branch=_BASE, pending_fix_dispatch=_make_pending_fix_dispatch())
        recorder = install_stub_dispatch(monkeypatch)
        candidate = fix_dispatch._FixDispatchCandidate(
            ticket_id=_FIX_LOOP_TICKET, client=_FIX_LOOP_CLIENT
        )

        jobs, _stale = fix_dispatch._build_dispatch_jobs(
            [candidate], {_FIX_LOOP_CLIENT: client}
        )
        fix_dispatch._act_on_pending_fix_dispatches(
            [candidate], clients={_FIX_LOOP_CLIENT: client}
        )

        assert [job.task.base_branch if job.task else None for job in jobs] == [_BASE]
        (call,) = recorder.calls
        assert isinstance(call["task"], TicketTask)
        assert call["task"].base_branch == _BASE


# --- claim spawn (R4) ---------------------------------------------------------


class TestClaimSpawn:
    def test_spawn_claimed_task_passes_row_to_create_worktree_and_unsaved_check(
        self,
        sample_client_config: ClientConfig,
        simple_config: OrchestratorConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cw.dispatch.claim import _claim_next_pending, _spawn_claimed_task

        write_clients_yaml(sample_client_config)
        add_ticket(_make_ticket_task(ticket_id="T-9", base_branch=_BASE))
        created: list[dict[str, object]] = []
        checked: list[dict[str, object]] = []

        def _record(*_a: object, **kwargs: object) -> Path:
            created.append(kwargs)
            msg = "stale"
            raise StaleWorktreeError(msg)

        def _unsaved(*_a: object, **kwargs: object) -> str:
            checked.append(kwargs)
            return "1 uncommitted path(s)"

        monkeypatch.setattr("cw.dispatch.claim.spawn.create_worktree", _record)
        monkeypatch.setattr("cw.dispatch.claim.spawn.unsaved_work_reason", _unsaved)
        task, _skipped = _claim_next_pending(
            "test-client",
            lane="default",
            client=sample_client_config,
            config=simple_config,
        )
        assert task is not None

        _spawn_claimed_task(
            task,
            sample_client_config,
            resolved_native_daemon=FakeNativeDaemonClient(),
            parent=None,
            emit=None,
        )

        assert [kw["task"] for kw in created] == [task]
        assert [kw["task"] for kw in checked] == [task]
        assert load_dev_queue().tasks[0].base_branch == _BASE


# --- merge-base diff-scope pin (R8) -------------------------------------------


@pytest.mark.parametrize("base", [_BASE, "main"])
def test_compute_branch_diff_scope_uses_origin_ref_of_given_branch(
    base: str, make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_git_repo("ws")
    calls: list[tuple[str, ...]] = []

    def _git(*args: str, **_kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 1, "", "")

    monkeypatch.setattr("cw.worktree._scope._run_git", _git)

    assert compute_branch_diff_scope(repo, base) is None
    assert calls == [("merge-base", f"origin/{base}", "HEAD")]


# --- grep-acceptance guard (R6) -----------------------------------------------

_S2_SCOPE = ("cw/worktree/", "cw/dispatch/", "cw/reconcile/")
_S2_FILES = frozenset({"cw/branch_ahead.py", "cw/merge_probe.py"})
# S3-deferred readers (R2). Shrink-only: a file leaves when S3 flips it. S3a
# (RFC 0020) flipped the four it held; no S2-scope file is deferred now.
_S3_DEFERRED: frozenset[str] = frozenset()


def _unannotated_default_branch_reads(source: str) -> list[int]:
    """Lines reading ``.default_branch`` with no retained-read annotation.

    A read is annotated when a ``# stays on client.default_branch:`` comment
    sits on a line of its enclosing statement up to the read, or in the
    comment block directly above that statement.
    """
    tree = ast.parse(source)
    comments = {
        tok.start[0]: tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    }
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)]
    unannotated: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and node.attr == "default_branch"
            and isinstance(node.ctx, ast.Load)
        ):
            continue
        stmt = max(
            (
                s
                for s in statements
                if s.lineno <= node.lineno <= (s.end_lineno or s.lineno)
            ),
            key=lambda s: s.lineno,
        )
        lines = list(range(stmt.lineno, node.lineno + 1))
        above = stmt.lineno - 1
        while above in comments:
            lines.append(above)
            above -= 1
        if not any(_MARKER in comments.get(line, "") for line in lines):
            unannotated.append(node.lineno)
    return unannotated


def _in_s2_scope(relpath: str) -> bool:
    return relpath in _S2_FILES or relpath.startswith(_S2_SCOPE)


def test_default_branch_reads_in_s2_scope_are_annotated_or_deferred() -> None:
    found: list[str] = []
    deferred_hits: dict[str, int] = dict.fromkeys(_S3_DEFERRED, 0)
    for path in _iter_src_files():
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        if not _in_s2_scope(relpath):
            continue
        hits = _unannotated_default_branch_reads(path.read_text(encoding="utf-8"))
        if relpath in _S3_DEFERRED:
            deferred_hits[relpath] = len(hits)
            continue
        found.extend(f"{relpath}:{line}" for line in hits)

    assert found == [], (
        "a .default_branch read in an S2 package is neither routed through "
        "resolve_base_branch nor annotated '# stays on client.default_branch: "
        f"<reason>': {found}"
    )
    # Shrink-only: an allowlisted file with no read left must leave the list.
    assert all(deferred_hits.values()), deferred_hits


def test_collector_flags_unannotated_read() -> None:
    bare = "def f(client):\n    return client.default_branch\n"
    annotated = (
        "def f(client):\n"
        "    # stays on client.default_branch: the primary checkout\n"
        "    return client.default_branch\n"
    )
    in_statement = (
        "def f(client):\n"
        "    # stays on client.default_branch: remedy text\n"
        "    msg = (\n"
        '        "a"\n'
        '        f"{client.default_branch}"\n'
        "    )\n"
        "    return msg\n"
    )
    stored = "def f(client):\n    client.default_branch = 'x'\n"

    assert _unannotated_default_branch_reads(bare) == [2]
    assert _unannotated_default_branch_reads(annotated) == []
    assert _unannotated_default_branch_reads(in_statement) == []
    assert _unannotated_default_branch_reads(stored) == []
