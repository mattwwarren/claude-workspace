"""Codex branch of the crash-only local harvest sweep (#2387, RFC 0014 A1).

A codex session that carries a :class:`LocalLivenessHandle` whose process has
died is picked up by ``_detect_local_harvest_candidates`` like any aider or
opencode session, but it is never harvested through a result synthesizer:
there is no sentinel to synthesize. ``_act_on_local_harvest_candidates``
branches it out before ``_synthesize_harvest_sentinel`` and applies an
audited four-part clean-requeue gate instead (reap_policy ``auto``, codex fix
loop off, worktree clean apart from the review verdict, HEAD unmoved since
the review's baseline). All four pass → requeue; any one fails → park. Either
way the session closes ``COMPLETED``/``CRASHED`` behind a ``SESSION_COMPLETED``
audit event recorded before any transition.

Fixtures are shared with ``test_reconcile_codex_boot.py`` rather than
re-implemented. The orchestrator config is injected through the sweep's
``config`` parameter (what ``reconcile()`` passes), so the boot-pass
``_use_config`` helpers — which pin ``codex_boot.load_effective_config`` — are
not needed here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CompletionReason,
    DevQueueStore,
    LocalLivenessHandle,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    SessionStatus,
    Stage,
)
from cw.reconcile import (
    ProposedAction,
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
)
from cw.reconcile import local as reconcile_local
from cw.reconcile.codex_boot import (
    _PARK_REASON_DIRTY_WORKTREE,
    _PARK_REASON_FIX_LOOP_ENABLED,
    _PARK_REASON_GIT_ERROR,
    _PARK_REASON_HEAD_MOVED,
    _PARK_REASON_REAP_POLICY_NOT_AUTO,
)
from cw.reconcile.local import (
    CODEX_HARVEST_CLEAN_REQUEUE_REASON,
    CODEX_HARVEST_ORPHANED_DISPOSITION,
)
from tests.conftest import commit_tracked_file
from tests.test_reconcile_codex_boot import (
    _assert_session_closed,
    _assert_session_left_active,
    _attention_events,
    _completed_events,
    _failing_record_event,
    _requeued_events,
    _seed_clean_codex_orphan,
    _task_without_base_ref,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import Session

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_TICKET = "T-orphan"
# Far above any real pid_max, so the recycled-PID guard reads it as dead.
_DEAD_PID = 2_000_000_000
_DEAD_START_TIME_NS = 123

_AUTO = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)
_SIGNAL_ONLY = OrchestratorConfig(reap_policy=ReapPolicy.SIGNAL_ONLY)
_AUTO_FIX_LOOP_ON = OrchestratorConfig(
    reap_policy=ReapPolicy.AUTO, default_codex_fix_loop_enabled=True
)

_GATE_KEYS = ("reap_policy_auto", "fix_loop_disabled", "worktree_clean", "head_unmoved")


def _stamp_dead_codex_liveness() -> Session:
    """Turn the seeded orphan into a dead-PID codex harvest candidate.

    ``_seed_clean_codex_orphan`` builds a daemon-surfaced session; a harvest
    candidate has no ``surface_ref`` and a ``local_liveness`` handle instead.
    """
    state = load_state()
    session = state.sessions[0].model_copy(
        update={
            "surface_ref": None,
            "local_liveness": LocalLivenessHandle(
                pid=_DEAD_PID, start_time_ns=_DEAD_START_TIME_NS, backend="codex"
            ),
        }
    )
    state.sessions[0] = session
    save_state(state)
    return session


def _seed(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> Path:
    repo, _ = _seed_clean_codex_orphan(tmp_config_dir, tmp_path, make_git_repo)
    _stamp_dead_codex_liveness()
    return repo


def _harvest(config: OrchestratorConfig | None) -> list[str]:
    """Run one detect+act sweep, as ``reconcile()`` does."""
    state = load_state()
    task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    candidates = _detect_local_harvest_candidates(state, task_by_ticket=task_by_ticket)
    if config is None:
        return _act_on_local_harvest_candidates(
            state, candidates, now=_NOW, task_by_ticket=task_by_ticket
        )
    return _act_on_local_harvest_candidates(
        state, candidates, now=_NOW, task_by_ticket=task_by_ticket, config=config
    )


def _gate_checks(consumer: str) -> dict[str, object]:
    payloads = _completed_events(consumer)
    assert len(payloads) == 1
    checks = payloads[0]["gate_checks"]
    assert isinstance(checks, dict)
    return checks


def _assert_codex_parked(consumer: str, reason: str, failing_check: str) -> None:
    """Parked (not requeued), session closed, and only *failing_check* failed."""
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == CODEX_HARVEST_ORPHANED_DISPOSITION
    assert task.session_id is None
    assert task.codex_orphan_session_id is None
    _assert_session_closed()
    attention = _attention_events(f"{consumer}-attention")
    assert len(attention) == 1
    assert attention[0]["paused_status"] == CODEX_HARVEST_ORPHANED_DISPOSITION
    assert reason in str(attention[0]["breadcrumbs"])
    assert _requeued_events(f"{consumer}-requeued") == []
    checks = _gate_checks(f"{consumer}-completed")
    assert set(checks) == set(_GATE_KEYS)
    assert {key for key, passed in checks.items() if not passed} == {failing_check}


def test_codex_backend_is_detected_as_a_harvest_candidate(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    candidates = _detect_local_harvest_candidates(load_state())

    assert len(candidates) == 1
    assert candidates[0].proposed_action is ProposedAction.HARVEST_LOCAL_COMPLETE
    assert candidates[0].session_id == _TICKET
    assert candidates[0].ticket_id == _TICKET


def test_reap_policy_not_auto_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _harvest(_SIGNAL_ONLY) == []

    _assert_codex_parked(
        "codex-harvest-signal-only",
        _PARK_REASON_REAP_POLICY_NOT_AUTO,
        "reap_policy_auto",
    )


def test_fix_loop_enabled_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _harvest(_AUTO_FIX_LOOP_ON) == []

    _assert_codex_parked(
        "codex-harvest-fix-loop", _PARK_REASON_FIX_LOOP_ENABLED, "fix_loop_disabled"
    )


def test_dirty_worktree_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    repo = _seed(tmp_config_dir, tmp_path, make_git_repo)
    (repo / "extra.txt").write_text("stray\n")

    assert _harvest(_AUTO) == []

    _assert_codex_parked(
        "codex-harvest-dirty", _PARK_REASON_DIRTY_WORKTREE, "worktree_clean"
    )


def test_head_moved_parks(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    repo = _seed(tmp_config_dir, tmp_path, make_git_repo)
    commit_tracked_file(repo, "extra.txt")

    assert _harvest(_AUTO) == []

    _assert_codex_parked(
        "codex-harvest-head-moved", _PARK_REASON_HEAD_MOVED, "head_unmoved"
    )


def test_unresolvable_baseline_parks_as_git_error(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    """No stage_base_ref and no local tracking ref: HEAD check is unknown.

    The tri-state ``None`` reads as a failed check in the audit dict and
    parks with the boot pass's git-error reason, never as a requeue.
    """
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    save_dev_queue(DevQueueStore(tasks=[_task_without_base_ref()]))

    assert _harvest(_AUTO) == []

    _assert_codex_parked(
        "codex-harvest-git-error", _PARK_REASON_GIT_ERROR, "head_unmoved"
    )


def test_all_checks_pass_requeues(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _harvest(_AUTO) == []

    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.PENDING
    assert task.session_id is None
    assert task.disposition is None
    _assert_session_closed()
    session = load_state().sessions[0]
    assert session.completed_reason is CompletionReason.CRASHED
    assert session.completed_at == _NOW
    assert _attention_events("codex-harvest-requeue-attention") == []
    assert _requeued_events("codex-harvest-requeue") == [
        {
            "ticket_id": _TICKET,
            "client": "client-a",
            "from_stage": Stage.REVIEW,
            "to_stage": Stage.REVIEW,
            "reason": CODEX_HARVEST_CLEAN_REQUEUE_REASON,
            "session_id": _TICKET,
        }
    ]


def test_audit_event_carries_full_recovery_evidence(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    _harvest(_AUTO)

    events = read_events(
        consumer="codex-harvest-audit",
        event_types=[OrchestratorEventType.SESSION_COMPLETED],
    )
    assert len(events) == 1
    assert events[0].correlation_id == _TICKET
    assert events[0].payload == {
        "session_id": _TICKET,
        "session_name": f"client-a/auto-dev/{_TICKET}",
        "ticket_id": _TICKET,
        "client": "client-a",
        "executor": "codex",
        "crashed": True,
        "salvaged": False,
        "prior_status": "active",
        "resulting_status": "completed",
        "disposition": "requeued",
        "reason": CODEX_HARVEST_CLEAN_REQUEUE_REASON,
        "gate_checks": dict.fromkeys(_GATE_KEYS, True),
        "pid": _DEAD_PID,
        "start_time_ns": _DEAD_START_TIME_NS,
    }


def test_audit_event_records_a_park_disposition(
    tmp_config_dir: Path, tmp_path: Path, make_git_repo: Callable[..., Path]
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    _harvest(_SIGNAL_ONLY)

    payloads = _completed_events("codex-harvest-audit-park")
    assert len(payloads) == 1
    assert payloads[0]["disposition"] == "parked"
    assert payloads[0]["reason"] == _PARK_REASON_REAP_POLICY_NOT_AUTO


def test_failed_audit_write_blocks_the_transition(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit before effect: a failed event write transitions nothing.

    The session stays ACTIVE and the task keeps its claim, so the next tick
    re-detects the same dead PID and completes the disposition.
    """
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    real = _failing_record_event(
        monkeypatch, reconcile_local, OrchestratorEventType.SESSION_COMPLETED
    )

    assert _harvest(_AUTO) == []

    session = _assert_session_left_active()
    task = load_dev_queue().tasks[0]
    assert task.status is QueueItemStatus.RUNNING
    assert task.session_id == session.id
    assert _requeued_events("codex-harvest-failed-audit-requeued") == []
    assert _attention_events("codex-harvest-failed-audit-attention") == []

    monkeypatch.setattr(reconcile_local, "record_event", real)

    assert _harvest(_AUTO) == []
    _assert_session_closed()
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING
    assert len(_completed_events("codex-harvest-failed-audit-completed")) == 1


def test_codex_candidate_never_reaches_git_synthesis_or_opencode_parse(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        msg = "codex harvest must not synthesize a sentinel"
        raise AssertionError(msg)

    for name in (
        "_synthesize_harvest_sentinel",
        "synthesize_git_result",
        "synthesize_opencode_result",
        "emit_result_on",
    ):
        monkeypatch.setattr(reconcile_local, name, _forbidden)
    for backend in ("aider", "opencode"):
        monkeypatch.setitem(reconcile_local._HARVEST_SYNTHESIZERS, backend, _forbidden)

    _harvest(_AUTO)

    session = load_state().sessions[0]
    assert session.last_result is None
    assert session.status is SessionStatus.COMPLETED


@pytest.mark.parametrize("config", [_AUTO, _SIGNAL_ONLY], ids=["requeue", "park"])
def test_codex_harvest_ticket_id_excluded_from_harvested_list(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    config: OrchestratorConfig,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)

    assert _TICKET not in _harvest(config)
    _assert_session_closed()


def _assert_untouched(consumer: str) -> None:
    _assert_session_left_active()
    assert _completed_events(f"{consumer}-completed") == []
    assert _attention_events(f"{consumer}-attention") == []
    assert _requeued_events(f"{consumer}-requeued") == []


def test_missing_task_row_leaves_session_untouched(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No dev-queue row for the ticket: the gate has nothing to decide on.

    The sweep's synthetic-task fallback must not stand in for the real row.
    """
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    save_dev_queue(DevQueueStore(tasks=[]))

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        assert _harvest(_AUTO) == []

    _assert_untouched("codex-harvest-no-row")
    assert _TICKET in caplog.text


def test_unknown_client_leaves_session_untouched(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    state = load_state()
    state.sessions[0] = state.sessions[0].model_copy(update={"client": "client-zz"})
    save_state(state)

    with caplog.at_level(logging.WARNING, logger=reconcile_local.__name__):
        assert _harvest(_AUTO) == []

    _assert_untouched("codex-harvest-no-client")
    assert "client-zz" in caplog.text


def test_config_param_defaults_to_effective_config_when_omitted(
    tmp_config_dir: Path,
    tmp_path: Path,
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(tmp_config_dir, tmp_path, make_git_repo)
    loads: list[int] = []

    def _load() -> OrchestratorConfig:
        loads.append(1)
        return _AUTO

    monkeypatch.setattr(reconcile_local, "load_effective_config", _load)

    _harvest(None)

    assert loads == [1]
    # The loaded (auto) config is what the gate ran against.
    assert load_dev_queue().tasks[0].status is QueueItemStatus.PENDING
