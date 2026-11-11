"""Tests for ``cw.dispatch.review_approval_gate`` (#2773).

The claim-time backstop for finalize Step 4.0: a PENDING FINALIZE row whose tier
is ``large`` on the row and whose review approval is not valid for its branch
head is parked ``BLOCKED_ON_USER`` before any worker starts. Real git repos via
``make_approval_repo``; the predicate's own matrix is in
``test_review_approval.py``.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cw import review_approval
from cw._lock_guard import held_locks
from cw.dev_queue import (
    REVIEW_APPROVAL_GATE_DISPOSITION,
    load_dev_queue,
    save_dev_queue,
)
from cw.dispatch import review_approval_gate as gate_mod
from cw.dispatch.claim.screening import _claim_next_pending
from cw.dispatch.lanes import _resolve_review_approval_holds_if_gated
from cw.dispatch.review_approval_gate import (
    REVIEW_APPROVAL_MAX_DEFERRED_TICKS,
    REVIEW_APPROVAL_MAX_PROBES_PER_TICK,
    ReviewApprovalHold,
    resolve_review_approval_holds,
)
from cw.events import read_events
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    StagePipelineConfig,
)
from cw.models.orchestrator_config.lane import LaneConfig
from cw.review_approval import APPROVAL_MISSING, APPROVAL_STALE, APPROVAL_UNREADABLE
from tests._clients_yaml import write_clients_yaml
from tests._review_approval_helpers import (
    ApprovalRepo,
    advance_main,
    cw_context,
    head,
    make_approval_repo,
    run_step40_fence,
    write_context,
)
from tests._shared.git_repos import commit_tracked_file, git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask
    from cw.native_daemon import FakeNativeDaemonClient

_CLIENT = "test-client"
_TICKET = "T-2773"
_BRANCH = "feature"
_STEP4_RELEASE = "cw dev-queue requeue T-2773 -c test-client --stage review"


@pytest.fixture(autouse=True)
def _reset_gate_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh process-lifetime state (streaks, warn-once maps) for every test."""
    monkeypatch.setattr(gate_mod, "_DEFER_STREAK", {})
    monkeypatch.setattr(gate_mod, "_WARNED_DEFER", {})
    monkeypatch.setattr(gate_mod, "_WARNED_TIER_UNKNOWN", {})


@pytest.fixture
def repo(make_git_repo_with_origin: Callable[..., tuple[Path, Path]]) -> ApprovalRepo:
    return make_approval_repo(make_git_repo_with_origin, "wt-gate", branch=_BRANCH)


@pytest.fixture
def client(repo: ApprovalRepo) -> ClientConfig:
    return ClientConfig(
        name=_CLIENT, workspace_path=repo.worktree, default_branch="main"
    )


def _row(repo: ApprovalRepo, **overrides: object) -> TicketTask:
    """A PENDING FINALIZE row, large on the row, approved at ``repo.approved``."""
    fields: dict[str, object] = {
        "ticket_id": _TICKET,
        "status": QueueItemStatus.PENDING,
        "stage": Stage.FINALIZE,
        "scope_hint": "large",
        "branch": _BRANCH,
        "review_approved_at": datetime.now(UTC),
        "review_approver_kind": "operator",
        "review_approved_sha": repo.approved,
    }
    fields.update(overrides)
    return _make_ticket_task(**fields)


def _snapshot(*rows: TicketTask) -> DevQueueStore:
    return DevQueueStore(tasks=list(rows))


def _resolve(client: ClientConfig, *rows: TicketTask) -> dict[str, ReviewApprovalHold]:
    return resolve_review_approval_holds(client, _snapshot(*rows))


class _GitSpy:
    """Counts ``run_git`` calls the shared predicate makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: set[object] = set()
        self.locks: list[tuple[object, ...]] = []
        real = review_approval.run_git

        def _spy(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(tuple(argv))
            self.timeouts.add(kwargs.get("timeout"))
            self.locks.append(held_locks())
            return real(argv, **kwargs)

        monkeypatch.setattr(review_approval, "run_git", _spy)


def _timeout_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["git"], 10)

    monkeypatch.setattr(review_approval, "run_git", _timeout)


# --- the resolver -----------------------------------------------------------


def test_valid_approval_is_not_a_hit(repo: ApprovalRepo, client: ClientConfig) -> None:
    assert _resolve(client, _row(repo)) == {}


def test_large_without_approval_is_a_missing_hit(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    row = _row(
        repo,
        review_approved_at=None,
        review_approver_kind=None,
        review_approved_sha=None,
    )

    hits = _resolve(client, row)

    hit = hits[_TICKET]
    assert hit.token == APPROVAL_MISSING
    assert hit.defer is False
    assert hit.branch == _BRANCH
    assert hit.head_sha == repo.approved


def test_computed_scope_tier_large_is_gated(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    row = _row(
        repo,
        scope_hint=None,
        computed_scope_tier="large",
        review_approved_sha=None,
        review_approver_kind=None,
        review_approved_at=None,
    )

    assert _resolve(client, row)[_TICKET].token == APPROVAL_MISSING


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": QueueItemStatus.RUNNING},
        {"status": QueueItemStatus.BLOCKED_ON_USER},
        {"stage": Stage.REVIEW},
        {"stage": Stage.IMPL},
        {"client": "someone-else"},
    ],
)
def test_only_pending_finalize_rows_of_the_client_are_scanned(
    repo: ApprovalRepo,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    spy = _GitSpy(monkeypatch)
    row = _row(
        repo,
        review_approved_at=None,
        review_approver_kind=None,
        review_approved_sha=None,
        **overrides,
    )

    assert _resolve(client, row) == {}
    assert spy.calls == []


_REVIEW_LESS_STAGES = [Stage.PLAN, Stage.IMPL, Stage.FINALIZE]


def test_review_less_client_pipeline_is_not_gated(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's only release is ``requeue --stage review``, refused here (#2773)."""
    spy = _GitSpy(monkeypatch)
    review_less = client.model_copy(
        update={"pipeline": StagePipelineConfig(stages=_REVIEW_LESS_STAGES)}
    )
    row = _row(
        repo,
        review_approved_at=None,
        review_approver_kind=None,
        review_approved_sha=None,
    )

    assert _resolve(review_less, row) == {}
    assert spy.calls == []


def test_review_less_lane_is_not_gated_but_a_sibling_lane_is(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    lanes = [
        LaneConfig(
            name="no-review",
            pipeline=StagePipelineConfig(stages=_REVIEW_LESS_STAGES),
        ),
        LaneConfig(name="reviewed"),
    ]
    laned = client.model_copy(update={"lanes": lanes})
    unapproved = {
        "review_approved_at": None,
        "review_approver_kind": None,
        "review_approved_sha": None,
    }
    skipped = _row(repo, ticket_id="T-skip", lane="no-review", **unapproved)
    gated = _row(repo, ticket_id="T-gate", lane="reviewed", **unapproved)

    holds = _resolve(laned, skipped, gated)

    assert set(holds) == {"T-gate"}


def test_small_tier_makes_no_git_call(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _GitSpy(monkeypatch)
    row = _row(
        repo, scope_hint="small", review_approver_kind=None, review_approved_at=None
    )

    assert _resolve(client, row) == {}
    assert spy.calls == []


@pytest.mark.parametrize(
    ("scope_hint", "computed"), [(None, None), ("medium", None), (None, "Small")]
)
def test_tier_unknown_row_is_not_gated_and_warns_once(
    repo: ApprovalRepo,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scope_hint: str | None,
    computed: str | None,
) -> None:
    spy = _GitSpy(monkeypatch)
    row = _row(
        repo,
        scope_hint=scope_hint,
        computed_scope_tier=computed,
        review_approver_kind=None,
        review_approved_at=None,
    )

    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        assert _resolve(client, row) == {}
        assert _resolve(client, row) == {}

    assert spy.calls == []
    warnings = [r for r in caplog.records if "not gating" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"{_CLIENT}/{_TICKET}" in message
    assert "scope tier unresolved on the row" in message
    assert "Step 4.0 in the worker still applies" in message


def test_tier_unknown_warning_is_pruned_when_the_row_leaves_scope(
    repo: ApprovalRepo, client: ClientConfig, caplog: pytest.LogCaptureFixture
) -> None:
    row = _row(repo, scope_hint=None)
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        _resolve(client, row)
        _resolve(client)  # the row left the queue: its warn-once entry is dropped
        _resolve(client, row)

    warnings = [r for r in caplog.records if "not gating" in r.getMessage()]
    assert len(warnings) == 2


def test_new_commit_after_approval_is_stale(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    commit_tracked_file(repo.worktree, "later.py")

    assert _resolve(client, _row(repo))[_TICKET].token == APPROVAL_STALE


def test_sync_merged_head_and_abbreviated_sha_are_not_hits(
    repo: ApprovalRepo, client: ClientConfig, tmp_path: Path
) -> None:
    advance_main(repo.origin, repo.worktree, tmp_path / "side", "main_only.py")
    git_in(repo.worktree, "merge", "--no-ff", "--no-edit", "origin/main")

    row = _row(repo, review_approved_sha=repo.approved[:9])

    assert _resolve(client, row) == {}


def test_absent_branch_is_unreadable_and_not_deferred(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    hit = _resolve(client, _row(repo, branch="dev/never-created"))[_TICKET]

    assert hit.token == APPROVAL_UNREADABLE
    assert hit.defer is False
    assert hit.head_sha is None
    assert "HEAD is not readable" in hit.detail


def test_branch_resolves_from_the_prefix_when_the_row_has_none(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    git_in(repo.worktree, "branch", f"dev/{_TICKET}", repo.approved)

    assert _resolve(client, _row(repo, branch=None)) == {}


def test_origin_ref_is_the_fallback_when_the_local_ref_is_absent(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    git_in(repo.worktree, "checkout", "main")
    git_in(repo.worktree, "branch", "-D", _BRANCH)

    assert _resolve(client, _row(repo)) == {}


def test_local_ref_wins_when_it_differs_from_the_origin_ref(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    # origin/feature still points at the approved head; the local branch (the
    # worker's HEAD) moved on. The origin ref would read `recorded`.
    commit_tracked_file(repo.worktree, "local_only.py")
    assert git_in(repo.worktree, "rev-parse", f"origin/{_BRANCH}") == repo.approved

    hit = _resolve(client, _row(repo))[_TICKET]

    assert hit.token == APPROVAL_STALE
    assert hit.head_sha == head(repo.worktree)


def test_kind_set_with_no_approved_at_reads_missing(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    """Channel parity: cw-context carries no approval when approved_at is None."""
    row = _row(repo, review_approved_at=None)

    hit = _resolve(client, row)[_TICKET]

    assert hit.token == APPROVAL_MISSING
    assert hit.approved_at_set is False
    assert hit.approver_kind == "operator"


def test_no_lock_is_held_while_git_runs(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _GitSpy(monkeypatch)

    _resolve(client, _row(repo))

    assert spy.calls
    assert all(locks == () for locks in spy.locks)


def test_git_calls_use_the_probe_timeout(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cw.merge_probe import PROBE_TIMEOUT_SECONDS

    spy = _GitSpy(monkeypatch)

    _resolve(client, _row(repo))

    assert spy.timeouts == {PROBE_TIMEOUT_SECONDS}


# --- transient failures, streak and cap -------------------------------------


def test_git_timeout_defers_and_keeps_the_row_pending(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _timeout_everywhere(monkeypatch)

    hit = _resolve(client, _row(repo))[_TICKET]

    assert hit.defer is True
    assert hit.token == APPROVAL_UNREADABLE


def test_first_transient_verdict_defers_the_remaining_candidates(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _timeout_everywhere(monkeypatch)
    rows = [_row(repo, ticket_id=f"T-{n}") for n in range(3)]
    probes: list[str] = []
    real = gate_mod._evaluate

    def _count(*args: Any, **kwargs: Any) -> Any:
        probes.append("probe")
        return real(*args, **kwargs)

    monkeypatch.setattr(gate_mod, "_evaluate", _count)

    hits = _resolve(client, *rows)

    assert len(probes) == 1
    assert set(hits) == {"T-0", "T-1", "T-2"}
    assert all(hit.defer for hit in hits.values())
    # Only the row that actually hit the fault counts toward the streak.
    assert set(gate_mod._DEFER_STREAK) == {(_CLIENT, "T-0")}


def test_persistent_transient_fault_warns_once_then_parks_after_the_streak(
    repo: ApprovalRepo,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _timeout_everywhere(monkeypatch)
    row = _row(repo)

    with caplog.at_level(logging.INFO, logger="cw.dispatch"):
        outcomes = [
            _resolve(client, row)[_TICKET]
            for _ in range(REVIEW_APPROVAL_MAX_DEFERRED_TICKS)
        ]

    assert [hit.defer for hit in outcomes] == [True] * (
        REVIEW_APPROVAL_MAX_DEFERRED_TICKS - 1
    ) + [False]
    assert outcomes[-1].token == APPROVAL_UNREADABLE
    deferring = [r for r in caplog.records if "gate deferring" in r.getMessage()]
    assert len(deferring) == 1
    assert f"{_CLIENT}/{_TICKET}" in deferring[0].getMessage()
    assert f"of {REVIEW_APPROVAL_MAX_DEFERRED_TICKS})" in deferring[0].getMessage()


def test_a_clean_resolve_resets_the_streak(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _row(repo)
    with monkeypatch.context() as patch:
        _timeout_everywhere(patch)
        _resolve(client, row)
        _resolve(client, row)
    assert gate_mod._DEFER_STREAK[(_CLIENT, _TICKET)] == 2

    assert _resolve(client, row) == {}

    assert (_CLIENT, _TICKET) not in gate_mod._DEFER_STREAK


def test_streak_is_pruned_when_the_row_leaves_scope(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _timeout_everywhere(monkeypatch)
    _resolve(client, _row(repo))
    assert gate_mod._DEFER_STREAK

    _resolve(client)

    assert gate_mod._DEFER_STREAK == {}


def test_other_clients_streaks_survive_a_scan(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    gate_mod._DEFER_STREAK[("other-client", "T-9")] = 3

    _resolve(client)

    assert gate_mod._DEFER_STREAK == {("other-client", "T-9"): 3}


def test_probe_cap_defers_the_rest_without_claiming_them(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes: list[str] = []
    real = gate_mod.evaluate_review_approval

    def _count(*args: Any, **kwargs: Any) -> Any:
        probes.append("probe")
        return real(*args, **kwargs)

    monkeypatch.setattr(gate_mod, "evaluate_review_approval", _count)
    total = REVIEW_APPROVAL_MAX_PROBES_PER_TICK + 3
    rows = [_row(repo, ticket_id=f"T-{n:02d}") for n in range(total)]

    hits = _resolve(client, *rows)

    assert len(probes) == REVIEW_APPROVAL_MAX_PROBES_PER_TICK
    assert len(hits) == 3
    assert all(hit.defer for hit in hits.values())
    assert set(hits) == {
        f"T-{n:02d}" for n in range(REVIEW_APPROVAL_MAX_PROBES_PER_TICK, total)
    }
    # A cap deferral is not a transient fault: no streak is charged.
    assert gate_mod._DEFER_STREAK == {}


def test_deferrals_log_one_summary_per_tick(
    repo: ApprovalRepo,
    client: ClientConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _timeout_everywhere(monkeypatch)
    rows = [_row(repo, ticket_id=f"T-{n}") for n in range(3)]

    with caplog.at_level(logging.INFO, logger="cw.dispatch"):
        _resolve(client, *rows)

    summaries = [r for r in caplog.records if "deferred" in r.getMessage()]
    assert any("3" in r.getMessage() for r in summaries)


# --- the lanes wrapper ------------------------------------------------------


def test_lanes_wrapper_skips_a_saturated_client(
    repo: ApprovalRepo, client: ClientConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _GitSpy(monkeypatch)
    row = _row(repo, review_approved_sha=None, review_approver_kind=None)

    holds = _resolve_review_approval_holds_if_gated(
        client, _snapshot(row), available_client_slots=0
    )

    assert holds == {}
    assert spy.calls == []


def test_lanes_wrapper_resolves_with_capacity(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    row = _row(repo, review_approved_sha=None, review_approver_kind=None)

    holds = _resolve_review_approval_holds_if_gated(
        client, _snapshot(row), available_client_slots=1
    )

    assert holds[_TICKET].token == APPROVAL_MISSING


# --- screening: the claim path ----------------------------------------------


def _seed(*rows: TicketTask) -> None:
    save_dev_queue(_snapshot(*rows))


def _stored(ticket_id: str = _TICKET) -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)


def _claim(
    client: ClientConfig, holds: dict[str, ReviewApprovalHold] | None, **kwargs: Any
) -> TicketTask | None:
    claimed, _ = _claim_next_pending(
        _CLIENT,
        lane=_stored().lane,
        client=client,
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 4}
        ),
        review_approval_holds=holds,
        **kwargs,
    )
    return claimed


def _unapproved(repo: ApprovalRepo, **overrides: object) -> TicketTask:
    fields: dict[str, object] = {
        "review_approved_at": None,
        "review_approver_kind": None,
        "review_approved_sha": None,
        "unproductive_attempts": 2,
        "attempts": 3,
    }
    fields.update(overrides)
    return _row(repo, **fields)


def test_screening_parks_a_hit_row(repo: ApprovalRepo, client: ClientConfig) -> None:
    row = _unapproved(repo)
    _seed(row)
    holds = _resolve(client, row)

    assert _claim(client, holds) is None

    parked = _stored()
    assert parked.status == QueueItemStatus.BLOCKED_ON_USER
    assert parked.disposition == REVIEW_APPROVAL_GATE_DISPOSITION
    assert parked.blocked_reason == "review_approval_missing_pre_dispatch"
    assert parked.unproductive_attempts == 2
    assert parked.attempts == 3


def test_screening_leaves_a_deferral_pending_and_writes_nothing(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    row = _unapproved(repo)
    _seed(row)
    holds = {
        _TICKET: ReviewApprovalHold(
            branch=_BRANCH,
            head_sha=None,
            token=APPROVAL_UNREADABLE,
            detail="git timed out",
            approver_kind=None,
            approved_sha=None,
            approved_at_set=False,
            defer=True,
        )
    }

    assert _claim(client, holds) is None

    assert _stored().status == QueueItemStatus.PENDING
    assert _stored().disposition is None


def test_screening_without_holds_screens_nothing(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    _seed(_unapproved(repo))

    claimed = _claim(client, None)

    assert claimed is not None
    assert _stored().status == QueueItemStatus.RUNNING


@pytest.mark.parametrize(
    "change",
    [
        {"stage": Stage.REVIEW},
        {"review_approver_kind": "recipe"},
        {"review_approved_sha": "c" * 40},
        {"review_approved_at": datetime(2026, 1, 1, tzinfo=UTC)},
    ],
    ids=["stage-moved", "kind-changed", "sha-changed", "approved-at-appeared"],
)
def test_a_hit_is_ignored_when_the_row_changed_since_the_snapshot(
    repo: ApprovalRepo, client: ClientConfig, change: dict[str, object]
) -> None:
    snapshot_row = _unapproved(repo)
    holds = _resolve(client, snapshot_row)
    _seed(_unapproved(repo, **change))

    claimed = _claim(client, holds)

    assert claimed is not None
    assert _stored().status == QueueItemStatus.RUNNING


def test_the_earlier_gates_run_first(repo: ApprovalRepo, client: ClientConfig) -> None:
    from cw.dispatch.adopted_head_gate import AdoptedHeadDrift

    pin = "a" * 40
    row = _unapproved(repo, adopted_head_sha=pin)
    _seed(row)
    holds = _resolve(client, row)
    drift = {_TICKET: AdoptedHeadDrift(_BRANCH, pin, "b" * 40, "")}

    assert _claim(client, holds, adopted_head_drifts=drift) is None

    assert _stored().disposition == "adopted_head_drift"


# --- events -----------------------------------------------------------------


def test_attention_event_carries_the_token_branch_and_release(
    repo: ApprovalRepo, client: ClientConfig
) -> None:
    row = _unapproved(repo)
    _seed(row)
    holds = _resolve(client, row)

    _claim(client, holds)

    events = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    payload = events[-1].payload
    assert payload["paused_status"] == REVIEW_APPROVAL_GATE_DISPOSITION
    assert payload["branch"] == _BRANCH
    assert payload["approval_token"] == APPROVAL_MISSING
    assert payload["breadcrumbs"] == ""
    details = str(payload["details"])
    assert "Step 4.0 would park this large-tier ticket (missing:" in details
    assert "the claim was refused before any worker started" in details
    assert _STEP4_RELEASE in details
    assert "A plain finalize requeue re-parks." in details


# --- acceptance: dispatch_tick with a stubbed worker ------------------------


def _write_client(
    repo: ApprovalRepo,
    tmp_path: Path,
    pipeline: StagePipelineConfig | None = None,
) -> None:
    extra: dict[str, object] = {} if pipeline is None else {"pipeline": pipeline}
    write_clients_yaml(
        ClientConfig(
            name=_CLIENT,
            workspace_path=repo.worktree,
            default_branch="main",
            worktree_base=tmp_path / "worktrees",
            **extra,
        )
    )


def test_dispatch_tick_never_spawns_for_a_large_row_without_approval(
    repo: ApprovalRepo,
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
) -> None:
    from cw.dispatch import dispatch_tick

    git_in(repo.worktree, "checkout", "main")
    _write_client(repo, tmp_path)
    _seed(_unapproved(repo))

    result = dispatch_tick(
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 1}
        ),
        native_daemon=mock_native_daemon,
    )

    assert result.spawned == 0
    assert mock_native_daemon.spawn_calls == []
    parked = _stored()
    assert parked.status == QueueItemStatus.BLOCKED_ON_USER
    assert parked.disposition == REVIEW_APPROVAL_GATE_DISPOSITION
    events = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert events[-1].payload["paused_status"] == REVIEW_APPROVAL_GATE_DISPOSITION


def test_dispatch_tick_claims_a_large_row_on_a_review_less_lane(
    repo: ApprovalRepo,
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
) -> None:
    """No claim-time park without a release: Step 4.0 in the worker parks it."""
    from cw.dispatch import dispatch_tick

    git_in(repo.worktree, "checkout", "main")
    _write_client(repo, tmp_path, StagePipelineConfig(stages=_REVIEW_LESS_STAGES))
    _seed(_unapproved(repo))

    result = dispatch_tick(
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 1}
        ),
        native_daemon=mock_native_daemon,
    )

    assert result.spawned == 1
    assert _stored().disposition != REVIEW_APPROVAL_GATE_DISPOSITION


def test_dispatch_tick_spawns_for_a_large_row_with_a_valid_approval(
    repo: ApprovalRepo,
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
) -> None:
    from cw.dispatch import dispatch_tick

    git_in(repo.worktree, "checkout", "main")
    _write_client(repo, tmp_path)
    _seed(_row(repo))

    result = dispatch_tick(
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 1}
        ),
        native_daemon=mock_native_daemon,
    )

    assert result.spawned == 1
    assert len(mock_native_daemon.spawn_calls) == 1


def test_tier_unknown_row_is_still_claimed_by_dispatch_tick(
    repo: ApprovalRepo,
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
) -> None:
    """A tier-less FINALIZE row is the worker's to judge (Step 4.0 fails closed)."""
    from cw.dispatch import dispatch_tick

    git_in(repo.worktree, "checkout", "main")
    _write_client(repo, tmp_path)
    _seed(_unapproved(repo, scope_hint=None))

    result = dispatch_tick(
        config=OrchestratorConfig(
            tick_interval_seconds=30, per_client_max_parallel={_CLIENT: 1}
        ),
        native_daemon=mock_native_daemon,
    )

    assert result.spawned == 1


# --- one predicate ----------------------------------------------------------


def _scenario_repo(
    repo: ApprovalRepo, scenario: str, tmp_path: Path
) -> dict[str, object]:
    """Mutate *repo* into *scenario*; return the row overrides it needs."""
    if scenario == "stale":
        commit_tracked_file(repo.worktree, "later.py")
    elif scenario == "sync-merge":
        advance_main(repo.origin, repo.worktree, tmp_path / "side", "m.py")
        git_in(repo.worktree, "merge", "--no-ff", "--no-edit", "origin/main")
    elif scenario == "missing":
        return {
            "review_approved_at": None,
            "review_approver_kind": None,
            "review_approved_sha": None,
        }
    elif scenario == "small":
        return {"scope_hint": "small"}
    return {}


def _gate_token(client: ClientConfig, row: TicketTask) -> str:
    hits = _resolve(client, row)
    return hits[_TICKET].token if _TICKET in hits else "pass"


def _cli_token(repo: ApprovalRepo, tier: str) -> str:
    from click.testing import CliRunner

    from cw.cli import main

    out = (
        CliRunner()
        .invoke(
            main,
            [
                "review",
                "check-approval",
                "--tier",
                tier,
                "--worktree",
                str(repo.worktree),
            ],
        )
        .output.strip()
    )
    return _line_token(out)


def _line_token(line: str) -> str:
    if line in {"REVIEW_APPROVAL_RECORDED", "REVIEW_APPROVAL_NOT_REQUIRED"}:
        return "pass"
    return line.removeprefix("REVIEW_APPROVAL_PARK: ").split(" ", 1)[0]


@pytest.mark.parametrize(
    "scenario", ["approved", "stale", "missing", "sync-merge", "small"]
)
def test_fence_cli_and_gate_agree(
    repo: ApprovalRepo, client: ClientConfig, tmp_path: Path, scenario: str
) -> None:
    overrides = _scenario_repo(repo, scenario, tmp_path)
    row = _row(repo, **overrides)
    tier = "small" if scenario == "small" else "large"
    # The worker-side context carries what the row carries.
    write_context(repo.worktree, cw_context(repo.worktree, row))
    fence = _line_token(run_step40_fence(repo.worktree, tier))
    cli = _cli_token(repo, tier)
    gate = _gate_token(client, row)

    assert fence == cli == gate
    expected = {
        "approved": "pass",
        "stale": "stale",
        "missing": "missing",
        "sync-merge": "pass",
        "small": "pass",
    }[scenario]
    assert gate == expected


def test_no_second_implementation_of_the_ancestry_rule() -> None:
    from pathlib import Path as _Path

    import cw.cli.review.check_approval as cli_mod

    for module in (cli_mod, gate_mod):
        source = _Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "rev-list" not in source
        assert "merge-base" not in source
