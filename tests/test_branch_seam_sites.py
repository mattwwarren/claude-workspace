"""Every migrated branch-derivation site reads the row's branch (#2948).

Before #2948 each site derived the ticket's branch as
``<feature_branch_prefix>/<ticket_id>`` itself, so a ticket whose PR lives on
any other branch (an adopted PR, #2943) was invisible to it. Each site now asks
:func:`cw.models.resolve_feature_branch`, passing the row when it is in scope.

Every site test is parametrized over the same two rows: an adopted-style row on
a non-prefix branch (the site must use that branch) and a ``branch=None`` row
(the site must resolve byte-identically to the old ``dev/<ticket>`` derivation).
The four session-keyed merged-PR fallbacks also cover the row-absent case, which
must keep the prefix fallback. Hermetic: ``gh``, ``git`` and the daemon are
stubbed at each module's seam; the repo's autouse isolation fixtures apply.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue import add_ticket, save_dev_queue
from cw.exceptions import ApproveGateError, WorktreeError
from cw.gh import OpenPrLookup
from cw.models import (
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    PendingFixDispatch,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.models.client import TrackerMcpGateConfig
from cw.native_daemon import FakeNativeDaemonClient
from cw.repair_disposition import RepairDisposition
from cw.worktree import resolve_task_worktree, worktree_path_for
from tests._changelog_repair_helpers import make_client, make_repair_job, repair_task
from tests._clients_yaml import write_clients_yaml
from tests.conftest import (
    _make_daemon_session,
    _make_ticket_task,
    _write_hook_context_file,
    occupy_worktree,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, Session

_TICKET = "T-9"
_CLIENT = "test-client"
_ADOPTED = "feature/adopted-x"
_PREFIX_BRANCH = f"dev/{_TICKET}"
_SESSION_ID = "sess-seam-9"
_SESSION_NAME = f"{_CLIENT}/auto-dev/{_TICKET}"
_HEAD_SHA = "a" * 40
_CLAIM = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

_ROWS = pytest.mark.parametrize(
    ("branch", "expected"),
    [(_ADOPTED, _ADOPTED), (None, _PREFIX_BRANCH)],
    ids=["adopted", "unset"],
)


def _row(branch: str | None, **overrides: object) -> TicketTask:
    return _make_ticket_task(
        ticket_id=_TICKET, client=_CLIENT, branch=branch, **overrides
    )


class _BranchRecorder:
    """Records the branch a stubbed seam was asked about."""

    def __init__(self) -> None:
        self.branches: list[str] = []


# ---------------------------------------------------------------------------
# worktree / dev_queue
# ---------------------------------------------------------------------------


@_ROWS
def test_resolve_task_worktree_trusts_the_row_branch_worktree(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = worktree_path_for(sample_client_config, expected)
    worktree.mkdir(parents=True)
    checked_out = {"branch": expected}
    monkeypatch.setattr(
        "cw.worktree._paths._checked_out_branch", lambda _p: checked_out["branch"]
    )

    assert resolve_task_worktree(_row(branch), sample_client_config) == worktree
    checked_out["branch"] = "dev/someone-else"
    assert resolve_task_worktree(_row(branch), sample_client_config) is None


@_ROWS
def test_resolve_task_worktree_without_checkout_probe(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3040: ``verify_checkout=False`` runs no branch probe and no subprocess."""

    def _no_probe(_path: object) -> str:
        msg = "verify_checkout=False must not probe the branch"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.worktree._paths._checked_out_branch", _no_probe)
    worktree = worktree_path_for(sample_client_config, expected)
    row = _row(branch)

    assert (
        resolve_task_worktree(row, sample_client_config, verify_checkout=False) is None
    )
    worktree.mkdir(parents=True)
    assert (
        resolve_task_worktree(row, sample_client_config, verify_checkout=False)
        == worktree
    )


@_ROWS
def test_impl_bypass_reads_the_row_branch_worktree(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.dev_queue.requeue import (
        _impl_bypass_plan_available,
        _impl_bypass_worktree_path,
    )

    worktree = worktree_path_for(sample_client_config, expected)
    (worktree / ".cw").mkdir(parents=True)
    (worktree / ".cw" / "plan.md").write_text("plan\n", encoding="utf-8")
    checked_out = {"branch": expected}
    monkeypatch.setattr(
        "cw.dev_queue.requeue._checked_out_branch", lambda _p: checked_out["branch"]
    )
    monkeypatch.setattr("cw.dev_queue.requeue._plan_body_signoff_ok", lambda _b: True)
    task = _row(branch)

    assert _impl_bypass_worktree_path(task, sample_client_config) == worktree
    verdict = _impl_bypass_plan_available(
        task, sample_client_config, allow_tracker_fallback=False
    )
    assert verdict.available is True
    checked_out["branch"] = "dev/someone-else"
    verdict = _impl_bypass_plan_available(
        task, sample_client_config, allow_tracker_fallback=False
    )
    assert verdict.available is False


@_ROWS
def test_scope_drift_approval_binds_to_the_row_branch_head(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
    from cw.dev_queue import approve_scope_drift_ticket

    save_dev_queue(
        DevQueueStore(
            tasks=[
                _row(
                    branch,
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    stage=Stage.IMPL,
                    blocked_reason=PLAN_SCOPE_DRIFT_BLOCKER_REASON,
                )
            ]
        )
    )
    recorder = _BranchRecorder()

    def _fake_head(
        branch_name: str, *, timeout: int = 10, cwd: Path | None = None
    ) -> tuple[str | None, bool]:
        del timeout, cwd
        recorder.branches.append(branch_name)
        return None, True

    monkeypatch.setattr(
        "cw.dev_queue.scope_drift.get_client", lambda _name: sample_client_config
    )
    monkeypatch.setattr(
        "cw.dev_queue.scope_drift.branch_head_sha_on_origin", _fake_head
    )

    with pytest.raises(ApproveGateError, match=f"origin/{expected}"):
        approve_scope_drift_ticket(_TICKET, _CLIENT, ["src/a.py"])
    assert recorder.branches == [expected]


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


@_ROWS
def test_claim_spawn_provisions_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    simple_config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.dispatch.claim import _claim_next_pending, _spawn_claimed_task

    write_clients_yaml(sample_client_config)
    add_ticket(_row(branch))
    recorder = _BranchRecorder()

    def _record(_client: ClientConfig, branch_name: str, **_kwargs: object) -> Path:
        recorder.branches.append(branch_name)
        msg = "stop after recording the branch"
        raise WorktreeError(msg)

    monkeypatch.setattr("cw.dispatch.claim.spawn.create_worktree", _record)
    task, _skipped = _claim_next_pending(
        _CLIENT, lane="default", client=sample_client_config, config=simple_config
    )
    assert task is not None
    assert task.branch == branch

    _spawn_claimed_task(
        task,
        sample_client_config,
        resolved_native_daemon=FakeNativeDaemonClient(),
        parent=None,
        emit=None,
    )

    assert recorder.branches == [expected]


@_ROWS
def test_occupancy_finds_a_live_worker_in_the_row_branch_worktree(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
) -> None:
    """The safety case: a missed live worker lets a second one spawn into it."""
    from cw.dispatch.claim.screening import resolve_occupied_ticket_ids

    daemon = FakeNativeDaemonClient()
    worktree = worktree_path_for(sample_client_config, expected)
    worktree.mkdir(parents=True)
    occupy_worktree(sample_client_config, worktree, "roster", daemon=daemon)

    occupied = resolve_occupied_ticket_ids(
        sample_client_config, DevQueueStore(tasks=[_row(branch)]), daemon=daemon
    )

    assert set(occupied) == {_TICKET}


def test_occupancy_ignores_a_worker_at_the_prefix_path_for_an_adopted_row(
    sample_client_config: ClientConfig,
) -> None:
    """Kill-check: the adopted row's worktree is the branch-derived one only."""
    from cw.dispatch.claim.screening import resolve_occupied_ticket_ids

    daemon = FakeNativeDaemonClient()
    worktree = worktree_path_for(sample_client_config, _PREFIX_BRANCH)
    worktree.mkdir(parents=True)
    occupy_worktree(sample_client_config, worktree, "roster", daemon=daemon)

    occupied = resolve_occupied_ticket_ids(
        sample_client_config, DevQueueStore(tasks=[_row(_ADOPTED)]), daemon=daemon
    )

    assert occupied == {}


@_ROWS
def test_occupied_skip_log_names_the_row_branch_worktree(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    simple_config: OrchestratorConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cw.dispatch.claim.screening import _screen_and_claim

    task = _row(branch)
    caplog.set_level(logging.WARNING, logger="cw.dispatch")

    outcome = _screen_and_claim(
        task,
        client_name=_CLIENT,
        lane="default",
        client=sample_client_config,
        config=simple_config,
        stale_pr_ticket_ids={},
        occupied_ticket_reasons={_TICKET: "live session"},
        tracker_mcp_gate_hits={},
        adopted_head_drifts={},
        review_approval_holds={},
        store=DevQueueStore(tasks=[task]),
        now=datetime.now(UTC),
    )

    assert outcome == "skipped"
    worktree = str(worktree_path_for(sample_client_config, expected))
    assert any(
        worktree in record.getMessage()
        for record in caplog.records
        if record.name == "cw.dispatch"
    )


@_ROWS
def test_open_pr_gate_probes_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.dispatch.pr_gate import resolve_stale_pr_ticket_ids

    recorder = _BranchRecorder()

    def _lookup(
        branch_name: str, *, timeout: int = 10, cwd: Path | None = None
    ) -> OpenPrLookup:
        del timeout, cwd
        recorder.branches.append(branch_name)
        return OpenPrLookup(open_pr=False, gh_available=True, url=None)

    monkeypatch.setattr("cw.dispatch.pr_gate.pr_exists_for_branch", _lookup)

    stale = resolve_stale_pr_ticket_ids(
        sample_client_config, DevQueueStore(tasks=[_row(branch)]), ttl_seconds=0
    )

    assert stale == {}
    assert recorder.branches == [expected]


@_ROWS
def test_tracker_mcp_gate_reads_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.dispatch.tracker_mcp_gate import resolve_tracker_mcp_gate_hits

    client = sample_client_config.model_copy(
        update={
            "tracker_mcp_gate": TrackerMcpGateConfig(
                enabled=True, plugin_id="tracker@example"
            )
        }
    )
    recorder = _BranchRecorder()

    def _evaluate(_client: ClientConfig, _gate: object, branch_name: str) -> None:
        recorder.branches.append(branch_name)

    monkeypatch.setattr("cw.dispatch.tracker_mcp_gate._evaluate_branch", _evaluate)

    hits = resolve_tracker_mcp_gate_hits(client, DevQueueStore(tasks=[_row(branch)]))

    assert hits == {}
    assert recorder.branches == [expected]


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


def _pending_fix_dispatch() -> PendingFixDispatch:
    return PendingFixDispatch(
        prompt="fix it",
        label="fix",
        cycle=1,
        requested_by_session_id="sess-review",
        requested_at=_CLAIM,
    )


@_ROWS
def test_fix_dispatch_job_targets_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
) -> None:
    from cw.reconcile.fix_dispatch import _build_dispatch_jobs, _FixDispatchCandidate

    save_dev_queue(
        DevQueueStore(
            tasks=[
                _row(
                    branch,
                    status=QueueItemStatus.RUNNING,
                    stage=Stage.REVIEW,
                    pending_fix_dispatch=_pending_fix_dispatch(),
                )
            ]
        )
    )

    jobs, stale = _build_dispatch_jobs(
        [_FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        {_CLIENT: sample_client_config},
    )

    assert stale == []
    assert [job.branch for job in jobs] == [expected]


def _repair_row(branch: str | None) -> TicketTask:
    return repair_task(ticket_id=_TICKET, client=_CLIENT, branch=branch)


def _repair_client(tmp_path: Path) -> ClientConfig:
    return make_client(tmp_path / "repo").model_copy(update={"name": _CLIENT})


@_ROWS
def test_repair_job_carries_the_row_branch(
    branch: str | None, expected: str, tmp_path: Path
) -> None:
    from cw.reconcile.review_recipes._shared import RepoSlugs, ReviewRecipeCandidate
    from cw.reconcile.review_recipes.changelog_repair import CHANGELOG_SPEC
    from cw.reconcile.review_recipes.repair_engine import prepare_repair_job

    client_cfg = _repair_client(tmp_path)
    task = _repair_row(branch)
    assert task.pr_url is not None
    candidate = ReviewRecipeCandidate(
        ticket_id=_TICKET,
        client=_CLIENT,
        lane=task.lane,
        recipe=CHANGELOG_SPEC.recipe,
        attention_state="merge_blocked",
        pr_url=task.pr_url,
        evidence={},
        session_id=None,
    )

    job = prepare_repair_job(
        CHANGELOG_SPEC,
        task,
        candidate,
        clients={_CLIENT: client_cfg},
        repo_slugs=RepoSlugs(),
        guard_target=lambda _task, _clients: None,
        config=None,
        repeat_fire_counts=None,
    )

    assert job is not None
    assert job.branch == expected


@_ROWS
def test_repair_prepare_fetches_and_checks_the_row_branch(
    branch: str | None,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.reconcile.review_recipes import repair_engine
    from cw.reconcile.review_recipes.repair_git import RepairStopError

    client_cfg = _repair_client(tmp_path)
    job = make_repair_job(client_cfg, task=_repair_row(branch))
    fetched = _BranchRecorder()
    read = _BranchRecorder()

    def _fetch(_repo: Path, _default: str, branch_name: str) -> str:
        fetched.branches.append(branch_name)
        return _HEAD_SHA

    def _read_head(_job: object, branch_name: str, _sha: str, _repo: Path) -> object:
        read.branches.append(branch_name)
        raise RepairStopError(RepairDisposition.PR_HEAD_MISMATCH, "stop here")

    monkeypatch.setattr(repair_engine, "_fetch_and_resolve", _fetch)
    monkeypatch.setattr(repair_engine, "_read_head", _read_head)

    with pytest.raises(RepairStopError):
        repair_engine.prepare_repair(job, repair_engine.RepairState(job))

    assert job.branch == expected
    assert fetched.branches == [expected]
    assert read.branches == [expected]


@_ROWS
def test_repair_park_names_the_row_branch_on_an_unknown_push(
    branch: str | None,
    expected: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cw.reconcile.review_recipes import repair_engine
    from cw.reconcile.review_recipes.repair_git import RepairStopError

    job = make_repair_job(_repair_client(tmp_path), task=_repair_row(branch))
    caplog.set_level(logging.WARNING, logger="cw.reconcile.review_recipes")
    stop = RepairStopError(RepairDisposition.PUSH_OUTCOME_UNKNOWN, "push timed out")

    repair_engine._park(job, repair_engine.RepairState(job), stop)

    assert any(
        f"compare origin/{expected} with" in record.getMessage()
        for record in caplog.records
    )


def _claim_session(**overrides: object) -> Session:
    fields: dict[str, object] = {
        "id": _SESSION_ID,
        "name": _SESSION_NAME,
        "client": _CLIENT,
        "status": SessionStatus.COMPLETED,
        "surface_ref": None,
        "started_at": _CLAIM + timedelta(seconds=5),
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


def _unbound_running_row(branch: str | None) -> TicketTask:
    return _row(
        branch,
        status=QueueItemStatus.RUNNING,
        session_id=None,
        attempts=1,
        claimed_at=_CLAIM,
    )


def _write_claim_context(worktree: Path, row: TicketTask) -> None:
    worktree.mkdir(parents=True)
    _write_hook_context_file(
        worktree,
        session_id=_SESSION_ID,
        ticket_id=_TICKET,
        client=_CLIENT,
        task=row,
    )


@_ROWS
def test_unowned_running_reads_the_row_branch_worktree_context(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
) -> None:
    from cw.reconcile.unowned_running import detect_unowned_running

    row = _unbound_running_row(branch)
    _write_claim_context(worktree_path_for(sample_client_config, expected), row)

    found = detect_unowned_running(
        DevQueueStore(tasks=[row]),
        CwState(sessions=[_claim_session()]),
        {_CLIENT: sample_client_config},
        now=datetime.now(UTC),
        daemon=FakeNativeDaemonClient(),
        native_live=set(),
    ).adopts

    assert [candidate.session_id for candidate in found] == [_SESSION_ID]


def test_unowned_running_ignores_a_prefix_path_context_for_an_adopted_row(
    sample_client_config: ClientConfig,
) -> None:
    """Kill-check: the adopted row only finds its session at its own path."""
    from cw.reconcile.unowned_running import detect_unowned_running

    row = _unbound_running_row(_ADOPTED)
    _write_claim_context(worktree_path_for(sample_client_config, _PREFIX_BRANCH), row)

    found = detect_unowned_running(
        DevQueueStore(tasks=[row]),
        CwState(sessions=[_claim_session()]),
        {_CLIENT: sample_client_config},
        now=datetime.now(UTC),
        daemon=FakeNativeDaemonClient(),
        native_live=set(),
    ).adopts

    assert found == []


@_ROWS
def test_codex_boot_baseline_uses_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.reconcile.codex_boot import _baseline_ref, _head_matches_pre_review_ref

    clients = {_CLIENT: sample_client_config}
    task = _row(branch, stage_base_ref=None)
    refs: list[str] = []

    def _capture(
        _worktree: Path,
        *,
        ref: str = "HEAD",
        strict: bool = True,
        timeout: float | None = None,
    ) -> str:
        del strict, timeout
        refs.append(ref)
        return _HEAD_SHA

    monkeypatch.setattr("cw.reconcile.codex_boot.capture_head_sha", _capture)

    assert _baseline_ref(task, clients) == f"origin/{expected}"
    assert _head_matches_pre_review_ref(tmp_path, task, clients) is True
    assert refs == ["HEAD", f"origin/{expected}"]


def _merged_recorder(monkeypatch: pytest.MonkeyPatch, target: str) -> _BranchRecorder:
    recorder = _BranchRecorder()

    def _merged(
        _ticket_id: str,
        *,
        branch: str | None = None,
        timeout: int = 10,
        cwd: Path | None = None,
    ) -> tuple[bool | None, bool]:
        del timeout, cwd
        assert branch is not None
        recorder.branches.append(branch)
        return False, True

    monkeypatch.setattr(target, _merged)
    return recorder


# (row branch, expected) for the session-keyed fallbacks: adopted, unset, and
# the row-absent case (``_NO_ROW``), which keeps the prefix fallback.
_NO_ROW = "<no row>"
_SESSION_KEYED = pytest.mark.parametrize(
    ("branch", "expected"),
    [(_ADOPTED, _ADOPTED), (None, _PREFIX_BRANCH), (_NO_ROW, _PREFIX_BRANCH)],
    ids=["adopted", "unset", "row-absent"],
)


def _rows_for(branch: str | None) -> list[TicketTask]:
    return [] if branch == _NO_ROW else [_row(branch)]


@_SESSION_KEYED
def test_timed_out_merged_filter_probes_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.reconcile.tasks import _filter_merged_candidates

    recorder = _merged_recorder(
        monkeypatch, "cw.reconcile._deps.pr_is_merged_for_ticket"
    )
    task_by_ticket = {(t.client, t.ticket_id): t for t in _rows_for(branch)}

    completed = _filter_merged_candidates(
        [(_claim_session(), _TICKET)],
        {_CLIENT: sample_client_config},
        task_by_ticket,
        300,
    )

    assert completed == []
    assert recorder.branches == [expected]


@_SESSION_KEYED
def test_merge_state_prepass_probes_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.reconcile.prepasses import _capture_merge_state

    save_dev_queue(DevQueueStore(tasks=_rows_for(branch)))
    recorder = _merged_recorder(
        monkeypatch, "cw.reconcile._deps.pr_is_merged_for_ticket"
    )
    pre_state = CwState(
        sessions=[
            _claim_session(status=SessionStatus.ACTIVE, origin=SessionOrigin.DAEMON)
        ]
    )

    merged, blocked = _capture_merge_state(
        pre_state,
        clients={_CLIENT: sample_client_config},
        config=OrchestratorConfig(),
    )

    assert merged == frozenset()
    assert blocked == frozenset()
    assert recorder.branches == [expected]


@_SESSION_KEYED
def test_shipped_release_prepass_probes_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.reconcile import shipped_release
    from cw.reconcile.routed_result_sessions import StrandedRoutedSession

    save_dev_queue(DevQueueStore(tasks=_rows_for(branch)))
    hit = StrandedRoutedSession(
        session=_claim_session(status=SessionStatus.ACTIVE, surface_ref="ref-9"),
        ticket_id=_TICKET,
        lane="default",
        stage=Stage.FINALIZE,
        row_status=None,
        stale_minutes=0.0,
        surface_ref="ref-9",
    )
    monkeypatch.setattr(
        shipped_release,
        "_candidates",
        lambda *_args, **_kwargs: [(hit, "https://example.invalid/pull/9")],
    )
    recorder = _BranchRecorder()

    def _fetch(
        branch_name: str, timeout: int = 10, *, cwd: Path | None = None
    ) -> tuple[list[object] | None, bool]:
        del timeout, cwd
        recorder.branches.append(branch_name)
        return [], True

    monkeypatch.setattr("cw.reconcile._deps.fetch_branch_prs", _fetch)

    shipped_release.capture_shipped_pr_probes(
        config=OrchestratorConfig(), clients={_CLIENT: sample_client_config}
    )

    assert recorder.branches == [expected]


# ---------------------------------------------------------------------------
# review_history / doctor
# ---------------------------------------------------------------------------


@_ROWS
def test_review_history_candidates_include_the_row_branch_worktree(
    branch: str | None, expected: str, sample_client_config: ClientConfig
) -> None:
    from cw.review_history.collect import _automatic_candidates

    notes: list[str] = []
    candidates = _automatic_candidates(_row(branch), sample_client_config, None, notes)

    assert candidates == [worktree_path_for(sample_client_config, expected)]
    assert notes == []


@_SESSION_KEYED
def test_doctor_timed_out_merged_probes_the_row_branch(
    branch: str | None,
    expected: str,
    sample_client_config: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cw.doctor.loop_health import _check_timed_out_merged

    save_dev_queue(DevQueueStore(tasks=_rows_for(branch)))
    recorder = _merged_recorder(
        monkeypatch, "cw.doctor.loop_health.pr_is_merged_for_ticket"
    )
    state = CwState(
        sessions=[
            _claim_session(
                status=SessionStatus.TIMED_OUT, completed_at=datetime.now(UTC)
            )
        ]
    )

    results = _check_timed_out_merged(state, {_CLIENT: sample_client_config})

    assert results == []
    assert recorder.branches == [expected]


@_ROWS
def test_wedge_branch_falls_back_to_the_row_branch(
    branch: str | None, expected: str, sample_client_config: ClientConfig
) -> None:
    from cw.doctor.wedge.task_running import _resolve_wedge_branch

    task = _row(branch)

    assert _resolve_wedge_branch(task, {}, {_CLIENT: sample_client_config}) == expected
    assert _resolve_wedge_branch(task, {}, {}) == expected


def test_wedge_branch_still_prefers_the_session_branch(
    sample_client_config: ClientConfig,
) -> None:
    from cw.doctor.wedge.task_running import _resolve_wedge_branch

    task = _row(_ADOPTED, session_id=_SESSION_ID)
    session = _claim_session(branch="session/branch")

    resolved = _resolve_wedge_branch(
        task, {_SESSION_ID: session}, {_CLIENT: sample_client_config}
    )

    assert resolved == "session/branch"
