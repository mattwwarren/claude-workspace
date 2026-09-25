"""Tests for cw.reconcile.fix_dispatch — the async fix-loop handoff (#2017 R21).

The module under test is deliberately NOT a review recipe: it is called
unconditionally from ``reconcile.core``, so these tests assert the absence of a
``review_recipes_enabled`` gate as explicitly as they assert the dispatch
behaviour itself.

Fixtures come from ``tests/conftest.py``'s canonical builders
(``_make_ticket_task``, ``_make_daemon_session``) rather than hand-rolled model
construction.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.events import record_event as _real_record_event
from cw.exceptions import CwError, HookContextConflictError, RemoteRefUnresolvedError
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    PendingFixDispatch,
    QueueItemStatus,
    SessionPurpose,
    SessionStatus,
    Stage,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import fix_dispatch, reconcile
from tests._reconcile_helpers import _make_pending_fix_dispatch
from tests.conftest import _make_daemon_session, _make_ticket_task, git_in
from tests.test_reconcile_review_recipes import (
    _make_fix_client,
    _seed_origin,
    _seed_worktree_with_unpushed_commit,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TICKET = "2017"
_CLIENT = "acme"


def _pending(**overrides: Any) -> PendingFixDispatch:
    kwargs: dict[str, Any] = {
        "label": f"fix-{_TICKET}",
        "requested_at": datetime(2026, 8, 26, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return _make_pending_fix_dispatch(**kwargs)


def _seed_task(**overrides: Any) -> None:
    """Persist a single dev-queue row built from the canonical task builder."""
    kwargs: dict[str, Any] = {
        "ticket_id": _TICKET,
        "client": _CLIENT,
        "status": QueueItemStatus.RUNNING,
    }
    kwargs.update(overrides)
    save_dev_queue(DevQueueStore(tasks=[_make_ticket_task(**kwargs)]))


def _only_task() -> Any:
    tasks = load_dev_queue().tasks
    assert len(tasks) == 1
    return tasks[0]


@pytest.fixture
def acme_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClientConfig:
    """Resolvable ``acme`` client, patched in at the module's own lookup seam."""
    workspace = tmp_path / "acme-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    client = ClientConfig(name=_CLIENT, workspace_path=workspace)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )
    return client


class _DispatchRecorder:
    """Records dispatch_fix_agent calls and applies an optional side effect."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.side_effect: Any = None

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.side_effect is not None:
            self.side_effect(**kwargs)
        return "fix-sess"


@pytest.fixture
def stub_dispatch(monkeypatch: pytest.MonkeyPatch) -> _DispatchRecorder:
    recorder = _DispatchRecorder()
    monkeypatch.setattr(fix_dispatch, "dispatch_fix_agent", recorder)
    return recorder


# --- detect phases ---------------------------------------------------------


def test_detect_pending_fix_dispatches_finds_marked_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)
    task.pending_fix_dispatch = _pending()

    candidates = fix_dispatch._detect_pending_fix_dispatches([task])

    assert [(c.ticket_id, c.client) for c in candidates] == [(_TICKET, _CLIENT)]


def test_detect_pending_fix_dispatches_skips_unmarked_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)

    assert fix_dispatch._detect_pending_fix_dispatches([task]) == []


def test_detect_fix_dispatch_completions_finds_dispatched_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)
    task.fix_dispatch_session_id = "fix-sess"

    candidates = fix_dispatch._detect_fix_dispatch_completions([task])

    assert [(c.ticket_id, c.client) for c in candidates] == [(_TICKET, _CLIENT)]


def test_detect_fix_dispatch_completions_skips_undispatched_task() -> None:
    task = _make_ticket_task(ticket_id=_TICKET, client=_CLIENT)

    assert fix_dispatch._detect_fix_dispatch_completions([task]) == []


# --- pending-dispatch act phase --------------------------------------------


def test_act_on_pending_fix_dispatches_spawns_and_clears_latch(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A successful dispatch consumes the handoff and points at the new session."""
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == [_TICKET]
    assert len(stub_dispatch.calls) == 1
    call = stub_dispatch.calls[0]
    assert call["branch"] == "dev/2017"
    assert call["prompt"] == "fix the MUST_FIX items\n"
    assert call["label"] == "fix-2017"
    assert call["parent"] == "review-sess"
    task = _only_task()
    assert task.pending_fix_dispatch is None
    assert task.fix_dispatch_session_id == "fix-sess"
    # The row must stay RUNNING: that is what keeps claim.py's PENDING-only
    # reclaim from dispatching a second REVIEW session mid-fix.
    assert task.status == QueueItemStatus.RUNNING


def test_act_on_pending_fix_dispatches_retries_on_transient_conflict(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A still-held worktree leaves the handoff intact for the next tick.

    This is the expected failure while the REVIEW session finishes going
    terminal -- dropping the record here would lose the entire action list.
    """

    def _conflict(**_kwargs: Any) -> None:
        msg = "worktree still held"
        raise HookContextConflictError(msg, conflicting_session_id="review-sess")

    stub_dispatch.side_effect = _conflict
    # A fresh requested_at: the transient-retry posture only holds inside the
    # escalation window (#2075) — the shared builder's fixed 2026-08-26 stamp
    # is aged past it by construction.
    _seed_task(pending_fix_dispatch=_pending(requested_at=datetime.now(UTC)))

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    task = _only_task()
    assert task.pending_fix_dispatch is not None
    assert task.fix_dispatch_session_id is None
    assert task.status == QueueItemStatus.RUNNING
    assert (
        read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]) == []
    )


def test_act_on_pending_fix_dispatches_escalates_conflict_past_age_bound(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A conflict on an aged handoff is no longer transient (#2075).

    The silent variant: some other session holds the worktree (a stray revert
    let a fresh REVIEW session claim the row), so the per-tick retry would
    recur forever with NO operator signal. Past _CONFLICT_ESCALATION_SECONDS
    the conflict routes through the loud failure path instead.
    """

    def _conflict(**_kwargs: Any) -> None:
        msg = "worktree still held"
        raise HookContextConflictError(msg, conflicting_session_id="other-review")

    stub_dispatch.side_effect = _conflict
    _seed_task(pending_fix_dispatch=_pending())  # builder stamp: aged by days

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    task = _only_task()
    assert task.pending_fix_dispatch is None
    assert task.status == QueueItemStatus.PENDING

    errored = read_events(event_types=[OrchestratorEventType.STAGE_ERRORED])
    assert len(errored) == 1
    assert errored[0].payload["error_kind"] == "fix_dispatch_failed"

    attention = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attention) == 1
    assert attention[0].payload["paused_status"] == "fix_dispatch_failed"
    assert "worktree still held" in attention[0].payload["breadcrumbs"]


def test_act_on_pending_fix_dispatches_clears_and_escalates_on_hard_failure(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A merge conflict (or any non-conflict CwError) unparks and escalates.

    No session is running when an async dispatch fails, so the two events ARE
    the operator signal -- there is no sentinel to carry a blocker.reason.
    """

    def _boom(**_kwargs: Any) -> None:
        msg = "merging origin/main into dev/2017 conflicted"
        raise CwError(msg)

    stub_dispatch.side_effect = _boom
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    task = _only_task()
    assert task.pending_fix_dispatch is None
    assert task.fix_dispatch_session_id is None
    assert task.status == QueueItemStatus.PENDING
    # #2075: the failure unpark must not charge the attempt ceiling — the
    # REVIEW round behind the handoff produced a real action list, and the
    # respawned round's own claim already increments raw attempts.
    assert task.unproductive_attempts == 0

    errored = read_events(event_types=[OrchestratorEventType.STAGE_ERRORED])
    assert len(errored) == 1
    assert errored[0].correlation_id == _TICKET
    assert errored[0].payload["session_id"] == "review-sess"
    assert errored[0].payload["ticket_id"] == _TICKET
    assert errored[0].payload["stage"] == "s3_fix_loop"
    assert errored[0].payload["error_kind"] == "fix_dispatch_failed"
    assert errored[0].payload["started_at"]

    attention = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attention) == 1
    assert attention[0].correlation_id == _TICKET
    assert attention[0].payload["session_id"] == "review-sess"
    assert attention[0].payload["client"] == _CLIENT
    assert attention[0].payload["claude_session_id"] is None
    assert attention[0].payload["paused_status"] == "fix_dispatch_failed"
    assert "conflicted" in attention[0].payload["breadcrumbs"]
    assert attention[0].payload["crashed"] is False


def test_act_on_pending_fix_dispatches_drops_stale_handoff_when_row_reverted_to_pending(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A row re-parked to PENDING under an unconsumed handoff must not spawn (#2142).

    The observed race: some other non-sentinel RUNNING->PENDING revert
    (crash/phantom/stall/salvage sweep) fires on a row carrying a
    ``pending_fix_dispatch``. Dispatching anyway spawns a FIX session that
    ``dispatch_fix_agent`` never correlates to the row (it passes no ``task=``
    kwarg), producing a roster-ACTIVE orphan that inflates the session-based
    client ceiling while the row itself stays PENDING.
    """
    _seed_task(status=QueueItemStatus.PENDING, pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    task = _only_task()
    assert task.pending_fix_dispatch is None
    assert task.fix_dispatch_session_id is None
    # No bogus transition: the row was already PENDING and stays there, so
    # claim.py's ordinary reclaim can pick it up now that the latch is gone.
    assert task.status == QueueItemStatus.PENDING

    errored = read_events(event_types=[OrchestratorEventType.STAGE_ERRORED])
    assert len(errored) == 1
    assert errored[0].correlation_id == _TICKET
    assert errored[0].payload["session_id"] == "review-sess"
    assert errored[0].payload["ticket_id"] == _TICKET
    assert errored[0].payload["stage"] == "s3_fix_loop"
    assert errored[0].payload["error_kind"] == "fix_dispatch_stale_row"

    attention = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attention) == 1
    assert attention[0].correlation_id == _TICKET
    assert attention[0].payload["session_id"] == "review-sess"
    assert attention[0].payload["client"] == _CLIENT
    assert attention[0].payload["paused_status"] == "fix_dispatch_stale_row"
    assert attention[0].payload["crashed"] is False
    assert "pending" in attention[0].payload["breadcrumbs"]


def test_act_on_pending_fix_dispatches_drops_stale_handoff_when_row_blocked_on_user(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """The guard is ``!= RUNNING``, not ``== PENDING`` (#2142).

    A row parked BLOCKED_ON_USER under an unconsumed handoff is the same
    orphan-spawn hazard: nothing about the fix agent's spawn path checks the
    row's status, so any non-RUNNING status must drop the handoff.
    """
    _seed_task(status=QueueItemStatus.BLOCKED_ON_USER, pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    task = _only_task()
    assert task.pending_fix_dispatch is None
    assert task.status == QueueItemStatus.BLOCKED_ON_USER

    errored = read_events(event_types=[OrchestratorEventType.STAGE_ERRORED])
    assert len(errored) == 1
    assert errored[0].payload["error_kind"] == "fix_dispatch_stale_row"

    attention = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attention) == 1
    assert attention[0].payload["paused_status"] == "fix_dispatch_stale_row"
    assert "blocked_on_user" in attention[0].payload["breadcrumbs"]


def test_stale_handoff_survives_when_its_audit_event_cannot_be_persisted(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed audit emission must not leave the handoff durably dropped (#2142).

    Round-3 binding contract: emission now happens per candidate, unlocked.
    A failure is logged and that candidate is dropped from this tick — it does
    NOT raise out of ``_act_on_pending_fix_dispatches`` (round 1's emit-under-
    the-lock design had to let it propagate, since nothing else in the shared
    transaction could be reasoned about; the unlocked per-candidate design no
    longer needs that). The handoff survives on disk; the next reconcile tick
    re-detects it and retries the page.
    """

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        msg = "events file unwritable"
        raise OSError(msg)

    _seed_task(status=QueueItemStatus.PENDING, pending_fix_dispatch=_pending())
    monkeypatch.setattr(fix_dispatch, "record_event", _explode)

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    task = _only_task()
    assert task.pending_fix_dispatch is not None
    assert task.pending_fix_dispatch.label == f"fix-{_TICKET}"
    assert task.status == QueueItemStatus.PENDING
    # No audit event was actually persisted -- record_event exploded on the
    # first call of the pair.
    assert read_events(event_types=[OrchestratorEventType.STAGE_ERRORED]) == []


def test_stale_handoff_events_emit_without_holding_dev_queue_lock(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-2 audit events fire only after ``dev_queue_lock()`` releases.

    (#2142 round 3.)

    Verified with a real, non-blocking ``flock`` probe against the exact lock
    file ``fix_dispatch.py`` itself acquires — not a timing heuristic. If the
    probe's own ``LOCK_EX | LOCK_NB`` acquisition fails, the module's lock was
    still held while ``record_event`` ran.
    """
    import fcntl

    from cw.config import dev_queue_lock as dev_queue_lock_file

    _seed_task(status=QueueItemStatus.PENDING, pending_fix_dispatch=_pending())

    lock_was_free: list[bool] = []

    def _probing_record_event(*args: Any, **kwargs: Any) -> Any:
        lock_path = dev_queue_lock_file()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        probe_fd = lock_path.open("w")
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_was_free.append(False)
        else:
            lock_was_free.append(True)
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
        finally:
            probe_fd.close()
        return _real_record_event(*args, **kwargs)

    monkeypatch.setattr(fix_dispatch, "record_event", _probing_record_event)

    fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    # Two record_event calls (STAGE_ERRORED + SESSION_NEEDS_ATTENTION), both
    # probed while the module's dev_queue_lock() was free.
    assert lock_was_free == [True, True]


def test_stale_handoff_not_cleared_when_row_reclaimed_before_revalidation(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row reclaimed back to RUNNING between phase 1 and phase 2 wins (#2142 round 3).

    Simulates a fresh REVIEW dispatch claiming the row while phase 2's event
    emission is in flight (unlocked). Phase 2's re-validation must see the
    live RUNNING status and skip the clear — silently, no exception — even
    though the event pair already fired describing the moment-ago condition.
    """
    _seed_task(status=QueueItemStatus.PENDING, pending_fix_dispatch=_pending())

    real_emit = fix_dispatch._emit_fix_dispatch_operator_signal

    def _emit_then_reclaim(**kwargs: Any) -> None:
        with dev_queue_lock():
            store = load_dev_queue()
            task = fix_dispatch._find_task(store, _TICKET, _CLIENT)
            assert task is not None
            task.status = QueueItemStatus.RUNNING
            save_dev_queue(store)
        real_emit(**kwargs)

    monkeypatch.setattr(
        fix_dispatch, "_emit_fix_dispatch_operator_signal", _emit_then_reclaim
    )

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    task = _only_task()
    assert task.pending_fix_dispatch is not None
    assert task.status == QueueItemStatus.RUNNING

    # The event pair still fired -- it described a real condition at the
    # moment phase 1 ran, and is not clawed back after the fact.
    errored = read_events(event_types=[OrchestratorEventType.STAGE_ERRORED])
    assert len(errored) == 1


def test_stale_handoff_not_cleared_when_rearmed_with_a_new_handoff(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handoff replaced by a fresh one between phase 1 and phase 2 wins.

    (#2142 round 3.)

    Same race as the RUNNING case, but the row stays non-RUNNING while a new
    REVIEW round records a *different* handoff (a new ``requested_at``/
    ``cycle``) before phase 2 re-validates. The old snapshot's identity no
    longer matches, so the new handoff must survive uncleared.
    """
    _seed_task(status=QueueItemStatus.PENDING, pending_fix_dispatch=_pending())

    real_emit = fix_dispatch._emit_fix_dispatch_operator_signal

    def _emit_then_rearm(**kwargs: Any) -> None:
        with dev_queue_lock():
            store = load_dev_queue()
            task = fix_dispatch._find_task(store, _TICKET, _CLIENT)
            assert task is not None
            task.pending_fix_dispatch = _pending(
                cycle=2,
                requested_by_session_id="new-review-sess",
                requested_at=datetime(2026, 8, 27, tzinfo=UTC),
            )
            save_dev_queue(store)
        real_emit(**kwargs)

    monkeypatch.setattr(
        fix_dispatch, "_emit_fix_dispatch_operator_signal", _emit_then_rearm
    )

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    task = _only_task()
    assert task.pending_fix_dispatch is not None
    assert task.pending_fix_dispatch.cycle == 2
    assert task.pending_fix_dispatch.requested_by_session_id == "new-review-sess"


def test_stale_handoff_phase_two_tolerates_row_removed_before_revalidation(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row cancelled between phase 1 and phase 2 is skipped, not crashed on.

    Same family as ``test_act_phases_tolerate_a_row_removed_mid_tick``, but
    for the specific window this round-3 rework introduces: between phase 1's
    snapshot and phase 2's re-acquired lock.
    """
    _seed_task(status=QueueItemStatus.PENDING, pending_fix_dispatch=_pending())

    real_emit = fix_dispatch._emit_fix_dispatch_operator_signal

    def _emit_then_cancel(**kwargs: Any) -> None:
        save_dev_queue(DevQueueStore(tasks=[]))
        real_emit(**kwargs)

    monkeypatch.setattr(
        fix_dispatch, "_emit_fix_dispatch_operator_signal", _emit_then_cancel
    )

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    assert load_dev_queue().tasks == []
    errored = read_events(event_types=[OrchestratorEventType.STAGE_ERRORED])
    assert len(errored) == 1


def test_act_on_pending_fix_dispatches_drops_job_mutated_during_stale_handoff_window(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job built alongside a stale candidate is re-checked before dispatch.

    (#2142 round 5.)

    ``_build_dispatch_jobs`` snapshots ``jobs`` and ``stale`` together under one
    lock, then releases it. ``_drop_stale_handoffs`` then spends real
    wall-clock time unlocked emitting the stale candidate's audit events —
    during that window, a *different* row (already built into a job this same
    tick) can be reverted RUNNING->PENDING by some other non-sentinel sweep.
    Simulates that exact interleaving from inside the stale candidate's own
    emit hook and asserts the mutated row's job is dropped from this tick's
    dispatch while an untouched job in the same tick still spawns.
    """
    mutated_ticket = _TICKET
    stale_ticket = "2018"
    untouched_ticket = "2019"
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id=mutated_ticket,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    pending_fix_dispatch=_pending(label=f"fix-{mutated_ticket}"),
                ),
                _make_ticket_task(
                    ticket_id=stale_ticket,
                    client=_CLIENT,
                    status=QueueItemStatus.PENDING,
                    pending_fix_dispatch=_pending(label=f"fix-{stale_ticket}"),
                ),
                _make_ticket_task(
                    ticket_id=untouched_ticket,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    pending_fix_dispatch=_pending(label=f"fix-{untouched_ticket}"),
                ),
            ]
        )
    )

    real_emit = fix_dispatch._emit_fix_dispatch_operator_signal

    def _emit_then_revert_other_row(**kwargs: Any) -> None:
        with dev_queue_lock():
            store = load_dev_queue()
            task = fix_dispatch._find_task(store, mutated_ticket, _CLIENT)
            assert task is not None
            task.status = QueueItemStatus.PENDING
            save_dev_queue(store)
        real_emit(**kwargs)

    monkeypatch.setattr(
        fix_dispatch, "_emit_fix_dispatch_operator_signal", _emit_then_revert_other_row
    )

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [
            fix_dispatch._FixDispatchCandidate(
                ticket_id=mutated_ticket, client=_CLIENT
            ),
            fix_dispatch._FixDispatchCandidate(ticket_id=stale_ticket, client=_CLIENT),
            fix_dispatch._FixDispatchCandidate(
                ticket_id=untouched_ticket, client=_CLIENT
            ),
        ],
        clients={_CLIENT: acme_client},
    )

    assert acted == [untouched_ticket]
    dispatched_tickets = {call["ticket_id"] for call in stub_dispatch.calls}
    assert dispatched_tickets == {untouched_ticket}

    mutated_task = fix_dispatch._find_task(load_dev_queue(), mutated_ticket, _CLIENT)
    assert mutated_task is not None
    # Dropped from dispatch only — the handoff itself is left in place for the
    # next tick's _build_dispatch_jobs to re-detect under the row's new
    # (non-RUNNING) status and route through the ordinary stale-handling path.
    assert mutated_task.pending_fix_dispatch is not None
    assert mutated_task.status == QueueItemStatus.PENDING


def test_act_on_pending_fix_dispatches_drops_job_stage_changed_mid_window(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job whose row stays RUNNING but changes stage is re-checked before dispatch.

    (#2142 round 6.)

    Round 5's ``_revalidate_dispatch_jobs`` only re-checked RUNNING + no
    ``fix_dispatch_session_id`` — a row that leaves and re-enters RUNNING at a
    *different* stage during the unlocked stale-handoff window (reverted, then
    re-claimed for a later stage) still passed that check, so the fix agent
    built for the old stage would still spawn against it. Simulates that
    interleaving from inside the stale candidate's own emit hook and asserts
    the stage-mutated row's job is dropped from this tick's dispatch while an
    untouched job in the same tick still spawns.
    """
    mutated_ticket = _TICKET
    stale_ticket = "2018"
    untouched_ticket = "2019"
    save_dev_queue(
        DevQueueStore(
            tasks=[
                _make_ticket_task(
                    ticket_id=mutated_ticket,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    stage=Stage.REVIEW,
                    pending_fix_dispatch=_pending(label=f"fix-{mutated_ticket}"),
                ),
                _make_ticket_task(
                    ticket_id=stale_ticket,
                    client=_CLIENT,
                    status=QueueItemStatus.PENDING,
                    pending_fix_dispatch=_pending(label=f"fix-{stale_ticket}"),
                ),
                _make_ticket_task(
                    ticket_id=untouched_ticket,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    stage=Stage.REVIEW,
                    pending_fix_dispatch=_pending(label=f"fix-{untouched_ticket}"),
                ),
            ]
        )
    )

    real_emit = fix_dispatch._emit_fix_dispatch_operator_signal

    def _emit_then_advance_other_row_stage(**kwargs: Any) -> None:
        with dev_queue_lock():
            store = load_dev_queue()
            task = fix_dispatch._find_task(store, mutated_ticket, _CLIENT)
            assert task is not None
            # Status stays RUNNING — only the stage moves, e.g. reverted and
            # re-claimed for finalize while this tick's job was already built.
            task.stage = Stage.FINALIZE
            save_dev_queue(store)
        real_emit(**kwargs)

    monkeypatch.setattr(
        fix_dispatch,
        "_emit_fix_dispatch_operator_signal",
        _emit_then_advance_other_row_stage,
    )

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [
            fix_dispatch._FixDispatchCandidate(
                ticket_id=mutated_ticket, client=_CLIENT
            ),
            fix_dispatch._FixDispatchCandidate(ticket_id=stale_ticket, client=_CLIENT),
            fix_dispatch._FixDispatchCandidate(
                ticket_id=untouched_ticket, client=_CLIENT
            ),
        ],
        clients={_CLIENT: acme_client},
    )

    assert acted == [untouched_ticket]
    dispatched_tickets = {call["ticket_id"] for call in stub_dispatch.calls}
    assert dispatched_tickets == {untouched_ticket}

    mutated_task = fix_dispatch._find_task(load_dev_queue(), mutated_ticket, _CLIENT)
    assert mutated_task is not None
    # Dropped from dispatch only — the handoff itself is left in place for the
    # next tick's _build_dispatch_jobs to re-detect under the row's new stage.
    assert mutated_task.pending_fix_dispatch is not None
    assert mutated_task.status == QueueItemStatus.RUNNING
    assert mutated_task.stage == Stage.FINALIZE


def test_act_on_pending_fix_dispatches_skips_unresolvable_client(
    tmp_config_dir: Path,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """An unresolvable client cannot be dispatched and must not be dropped."""
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    assert _only_task().pending_fix_dispatch is not None


def test_act_phases_tolerate_a_row_removed_mid_tick(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """A row cancelled between detect and act is skipped, not crashed on.

    Both act phases re-resolve the row under their own lock precisely so a
    concurrent ``cw dev-queue cancel`` cannot make them write back a snapshot
    of a row that no longer exists.
    """
    save_dev_queue(DevQueueStore(tasks=[]))
    candidates = [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]

    assert (
        fix_dispatch._act_on_pending_fix_dispatches(
            candidates, clients={_CLIENT: acme_client}
        )
        == []
    )
    assert fix_dispatch._act_on_fix_dispatch_completions(candidates) == []
    assert stub_dispatch.calls == []


def test_stamp_helpers_tolerate_a_row_removed_after_dispatch(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
) -> None:
    """The post-lock stamps run after the spawn, so the row can be gone by then."""
    save_dev_queue(DevQueueStore(tasks=[]))
    job = fix_dispatch._DispatchJob(
        client_cfg=acme_client,
        branch="dev/2017",
        pending=_pending(),
        ticket_id=_TICKET,
        client=_CLIENT,
        lane="default",
        stage=Stage.REVIEW,
    )

    fix_dispatch._stamp_dispatch_success(job, "fix-sess")
    fix_dispatch._stamp_dispatch_failure(job, CwError("gone"))

    assert load_dev_queue().tasks == []


# --- completion act phase ---------------------------------------------------


def _save_fix_session(status: SessionStatus) -> None:
    from cw.config import save_state

    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id="fix-sess",
                    name=f"{_CLIENT}/fix/{_TICKET}",
                    client=_CLIENT,
                    status=status,
                )
            ]
        )
    )


def test_act_on_fix_dispatch_completions_unparks_task(tmp_config_dir: Path) -> None:
    _seed_task(fix_dispatch_session_id="fix-sess")
    _save_fix_session(SessionStatus.COMPLETED)

    unparked = fix_dispatch._act_on_fix_dispatch_completions(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]
    )

    assert unparked == [_TICKET]
    task = _only_task()
    assert task.fix_dispatch_session_id is None
    assert task.status == QueueItemStatus.PENDING
    # #2075: the routine per-cycle unpark is progress (a review round ran AND
    # its fix session completed) — charging it walked healthy fix loops to
    # attempt_cap_blocked at an already-approved finalize.
    assert task.unproductive_attempts == 0


def test_act_on_fix_dispatch_completions_skips_still_live_session(
    tmp_config_dir: Path,
) -> None:
    _seed_task(fix_dispatch_session_id="fix-sess")
    _save_fix_session(SessionStatus.ACTIVE)

    unparked = fix_dispatch._act_on_fix_dispatch_completions(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]
    )

    assert unparked == []
    task = _only_task()
    assert task.fix_dispatch_session_id == "fix-sess"
    assert task.status == QueueItemStatus.RUNNING


def test_act_on_fix_dispatch_completions_unparks_unresolvable_session(
    tmp_config_dir: Path,
) -> None:
    """A session cw cannot resolve at all is gone, not pending.

    Treating it as still-live would strand the row RUNNING forever, since
    nothing else clears fix_dispatch_session_id.
    """
    _seed_task(fix_dispatch_session_id="vanished")

    unparked = fix_dispatch._act_on_fix_dispatch_completions(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)]
    )

    assert unparked == [_TICKET]
    assert _only_task().status == QueueItemStatus.PENDING


# --- entry point ------------------------------------------------------------


def test_run_fix_dispatch_is_not_gated_by_review_recipes_enabled(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """The whole point of siting this outside the review_recipes package.

    Registering it there would have inherited a default-off master switch and
    silently disabled the fix loop for every client that never opted in.
    """
    _seed_task(pending_fix_dispatch=_pending())

    acted = fix_dispatch.run_fix_dispatch(
        config=OrchestratorConfig(review_recipes_enabled=False)
    )

    assert acted == [_TICKET]
    assert len(stub_dispatch.calls) == 1
    assert _only_task().fix_dispatch_session_id == "fix-sess"


def test_run_fix_dispatch_no_candidates_is_a_noop(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    _seed_task()

    assert fix_dispatch.run_fix_dispatch(config=OrchestratorConfig()) == []
    assert stub_dispatch.calls == []


# --- sessions_lock integration (#2064) ---------------------------------------


def test_run_fix_dispatch_spawns_real_fix_session_through_sessions_lock(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile() must dispatch fix agents post-lock (#2064).

    Exercises the REAL ``dispatch_fix_agent`` -> ``spawn_create_impl`` path
    (only the native daemon is faked), so the spawn's own ``sessions_lock()``
    acquisition genuinely runs. On the pre-fix tree this dies with
    ``SessionsLockReentryError`` inside ``spawn_create_impl``, is caught by
    ``_act_on_pending_fix_dispatches``'s broad ``except CwError``, and no fix
    session is ever spawned.
    """
    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2019"
    _seed_origin(client, branch)
    monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: mock_native_daemon)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )

    # Parent session dispatch_fix_agent -> spawn_create_impl resolves via
    # PendingFixDispatch.requested_by_session_id ("review-sess" default).
    # Terminal so it never enters phantom detection (_LIVE_STATUSES-only).
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id="review-sess",
                    name=f"{_CLIENT}/review/2019",
                    client=_CLIENT,
                    status=SessionStatus.COMPLETED,
                )
            ]
        )
    )
    task = _make_ticket_task(
        ticket_id="2019",
        client=_CLIENT,
        status=QueueItemStatus.RUNNING,
    )
    task.pending_fix_dispatch = _pending(label="fix-2019")
    save_dev_queue(DevQueueStore(tasks=[task]))

    reconcile()

    fix_sessions = [s for s in load_state().sessions if s.purpose == SessionPurpose.FIX]
    assert len(fix_sessions) == 1
    updated = _only_task()
    assert updated.pending_fix_dispatch is None
    assert updated.fix_dispatch_session_id == fix_sessions[0].id
    assert read_events(event_types=[OrchestratorEventType.STAGE_ERRORED]) == []


def test_dispatch_fix_agent_falls_back_to_parent_none_when_unresolvable(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable ``requested_by_session_id`` spawns anyway with parent=None.

    Regression for #2149: pre-fix, ``dispatch_fix_agent`` -> ``spawn_create_impl``
    raised ``CwError: Parent session not found`` for ANY id that wasn't an exact
    hot cw id/name match -- including a same-cycle review session whose row had
    aged into an archive, or (unreproducibly in-repo, per the ticket) a claude
    session id. This exercises the fully unresolvable case: no session anywhere
    (hot or archived) matches. The fix session must still spawn, carrying a
    friction note in its prompt and a log-only fallback -- no STAGE_ERRORED.
    """
    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2023"
    _seed_origin(client, branch)
    monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: mock_native_daemon)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )

    task = _make_ticket_task(
        ticket_id="2023",
        client=_CLIENT,
        status=QueueItemStatus.RUNNING,
    )
    task.pending_fix_dispatch = _pending(
        label="fix-2023", requested_by_session_id="totally-unresolvable-id"
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reconcile()

    fix_sessions = [s for s in load_state().sessions if s.purpose == SessionPurpose.FIX]
    assert len(fix_sessions) == 1
    assert fix_sessions[0].parent_session_id is None
    updated = _only_task()
    assert updated.pending_fix_dispatch is None
    assert updated.fix_dispatch_session_id == fix_sessions[0].id
    assert updated.status == QueueItemStatus.RUNNING
    assert read_events(event_types=[OrchestratorEventType.STAGE_ERRORED]) == []

    prompt_sent = mock_native_daemon.spawn_calls[-1][1]
    assert "could not be resolved" in prompt_sent


def test_dispatch_fix_agent_resolves_parent_via_claude_session_id_end_to_end(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``requested_by_session_id`` holding a claude_session_id still resolves.

    Mirrors ``test_run_fix_dispatch_spawns_real_fix_session_through_sessions_lock``
    but the parent session's cw id deliberately differs from the claude id
    stamped into ``requested_by_session_id`` -- the exact id-space mismatch
    named in #2149.
    """
    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2024"
    _seed_origin(client, branch)
    monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: mock_native_daemon)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )

    claude_id = "1a2b3c4d-5e6f-7890-abcd-ef0123456789"
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id="review-cw1",
                    name=f"{_CLIENT}/review/2024",
                    client=_CLIENT,
                    status=SessionStatus.COMPLETED,
                    claude_session_id=claude_id,
                )
            ]
        )
    )
    task = _make_ticket_task(
        ticket_id="2024",
        client=_CLIENT,
        status=QueueItemStatus.RUNNING,
    )
    task.pending_fix_dispatch = _pending(
        label="fix-2024", requested_by_session_id=claude_id
    )
    save_dev_queue(DevQueueStore(tasks=[task]))

    reconcile()

    fix_sessions = [s for s in load_state().sessions if s.purpose == SessionPurpose.FIX]
    assert len(fix_sessions) == 1
    assert fix_sessions[0].parent_session_id == "review-cw1"
    updated = _only_task()
    assert updated.pending_fix_dispatch is None
    assert updated.fix_dispatch_session_id == fix_sessions[0].id
    assert read_events(event_types=[OrchestratorEventType.STAGE_ERRORED]) == []


# --- per-tick spawn cap (#2064) -----------------------------------------------


def test_act_on_pending_fix_dispatches_caps_spawns_per_tick(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fan-out is bounded: only the first ``_MAX_FIX_DISPATCHES_PER_TICK``
    candidates spawn; the rest are left pending and reconsidered next tick."""
    monkeypatch.setattr(fix_dispatch, "_MAX_FIX_DISPATCHES_PER_TICK", 2)
    ticket_ids = ["2020", "2021", "2022"]
    tasks = [
        _make_ticket_task(
            ticket_id=ticket_id,
            client=_CLIENT,
            status=QueueItemStatus.RUNNING,
            pending_fix_dispatch=_pending(label=f"fix-{ticket_id}"),
        )
        for ticket_id in ticket_ids
    ]
    save_dev_queue(DevQueueStore(tasks=tasks))

    candidates = fix_dispatch._detect_pending_fix_dispatches(load_dev_queue().tasks)
    with caplog.at_level(logging.INFO, logger="cw.reconcile.fix_dispatch"):
        acted = fix_dispatch._act_on_pending_fix_dispatches(
            candidates, clients={_CLIENT: acme_client}
        )

    assert len(acted) == 2
    assert len(stub_dispatch.calls) == 2

    elided_ids = set(ticket_ids) - set(acted)
    assert len(elided_ids) == 1
    reloaded = {t.ticket_id: t for t in load_dev_queue().tasks}
    elided = reloaded[next(iter(elided_ids))]
    assert elided.pending_fix_dispatch is not None
    assert elided.fix_dispatch_session_id is None
    assert elided.status == QueueItemStatus.RUNNING

    assert any("cap" in rec.message for rec in caplog.records)


# --- impl-reported remote branch (#2209) -------------------------------------


def _save_sessions(*sessions: Any) -> None:
    save_state(CwState(sessions=list(sessions)))


def _impl_session(
    *,
    session_id: str,
    ticket_id: str = _TICKET,
    client: str = _CLIENT,
    branch: Any = "dev/2017-short-description",
    started_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> Any:
    """A terminal IMPL session whose sentinel reports *branch* for *ticket_id*."""
    return _make_daemon_session(
        id=session_id,
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        status=SessionStatus.COMPLETED,
        started_at=started_at,
        last_result={"branch": branch},
    )


def test_run_fix_dispatch_passes_impl_reported_branch_to_dispatch(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """The templated name stays the worktree key; the reported name rides along."""
    _save_sessions(_impl_session(session_id="impl-sess"))
    _seed_task(pending_fix_dispatch=_pending())

    fix_dispatch.run_fix_dispatch(config=OrchestratorConfig())

    assert len(stub_dispatch.calls) == 1
    call = stub_dispatch.calls[0]
    assert call["branch"] == "dev/2017"
    assert call["remote_branch"] == "dev/2017-short-description"


def test_run_fix_dispatch_reported_branch_skips_blank_and_non_str_and_picks_newest(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """ "Newest session with a non-blank ``str`` branch", not simply "newest".

    The scan is tie-broken on ``started_at`` — the same field
    ``concierge._find_session_for_ticket`` sorts on. A missing sentinel, a
    ``None``, a blank string and a non-``str`` value are each skipped cleanly,
    so the oldest-but-only-valid session is the one that wins. Sessions for
    another client, another ticket, and a non-``auto-dev/`` name must not
    contribute at all.
    """
    _save_sessions(
        # The common real case: the newest session for a ticket is often the
        # REVIEW session, which has emitted no sentinel yet.
        _make_daemon_session(
            id="no-sentinel",
            name=f"{_CLIENT}/auto-dev/{_TICKET}",
            client=_CLIENT,
            status=SessionStatus.ACTIVE,
            started_at=datetime(2026, 3, 7, tzinfo=UTC),
        ),
        _impl_session(
            session_id="non-str",
            branch=123,
            started_at=datetime(2026, 3, 4, tzinfo=UTC),
        ),
        _impl_session(
            session_id="null-branch",
            branch=None,
            started_at=datetime(2026, 3, 3, tzinfo=UTC),
        ),
        _impl_session(
            session_id="blank-branch",
            branch="",
            started_at=datetime(2026, 3, 2, tzinfo=UTC),
        ),
        _impl_session(
            session_id="valid-branch",
            branch="dev/2017-slug",
            started_at=datetime(2026, 3, 1, tzinfo=UTC),
        ),
        _impl_session(
            session_id="other-client",
            client="globex",
            branch="dev/2017-wrong-client",
            started_at=datetime(2026, 3, 5, tzinfo=UTC),
        ),
        _impl_session(
            session_id="other-ticket",
            ticket_id="9999",
            branch="dev/9999-wrong-ticket",
            started_at=datetime(2026, 3, 5, tzinfo=UTC),
        ),
        _make_daemon_session(
            id="fix-session",
            name=f"{_CLIENT}/fix/{_TICKET}",
            client=_CLIENT,
            status=SessionStatus.COMPLETED,
            started_at=datetime(2026, 3, 6, tzinfo=UTC),
            last_result={"branch": "dev/2017-wrong-name-prefix"},
        ),
    )
    _seed_task(pending_fix_dispatch=_pending())

    fix_dispatch.run_fix_dispatch(config=OrchestratorConfig())

    assert stub_dispatch.calls[0]["remote_branch"] == "dev/2017-slug"


def test_run_fix_dispatch_remote_branch_none_without_sentinel_branch(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """No session reports a branch: the ladder falls back to today's behaviour."""
    _seed_task(pending_fix_dispatch=_pending())

    fix_dispatch.run_fix_dispatch(config=OrchestratorConfig())

    assert stub_dispatch.calls[0]["remote_branch"] is None


def _seed_e2e_fix_row(ticket_id: str, *, with_impl_session: bool) -> None:
    """A RUNNING row with a handoff, plus the sessions reconcile() will resolve.

    The row carries ``session_id="review-sess"`` — the REVIEW session that
    recorded the handoff is the row's claimant (``auto-dev-review`` writes only
    ``pending_fix_dispatch``, never ``session_id``), and
    ``_park_for_unresolved_ref``'s ``expected_session_id`` guard matches on that
    identity. That session is deliberately NOT seeded into state here: a
    COMPLETED DAEMON session with a RUNNING row is exactly what
    ``revert_completed_silent_tasks`` reverts to PENDING, which would short the
    row out of the fix-dispatch pass before it runs. Any IMPL session seeded
    below is terminal, so it does not enter phantom detection
    (``_LIVE_STATUSES``-only), matching the sibling sessions_lock e2e test.
    """
    sessions: list[Any] = []
    if with_impl_session:
        sessions.append(
            _impl_session(
                session_id="impl-sess",
                ticket_id=ticket_id,
                branch=f"dev/{ticket_id}-short-description",
            )
        )
    save_state(CwState(sessions=sessions))
    task = _make_ticket_task(
        ticket_id=ticket_id,
        client=_CLIENT,
        status=QueueItemStatus.RUNNING,
    )
    # The REVIEW session that recorded the handoff is the row's claimant, as
    # claim.py stamps it; _park_for_unresolved_ref's expected_session_id guard
    # matches on that identity.
    task.session_id = "review-sess"
    task.pending_fix_dispatch = _pending(label=f"fix-{ticket_id}")
    save_dev_queue(DevQueueStore(tasks=[task]))


def test_reconcile_dispatches_fix_against_impl_reported_slug_branch(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2209 end-to-end, through the REAL ``dispatch_fix_agent`` and real git.

    The impl pushed under a slug name and set no upstream, so ``origin/dev/2020``
    never existed. Pre-#2209 this failed resolution every tick; now the
    sentinel-reported name carries the dispatch.
    """
    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2020"
    worktree = _seed_worktree_with_unpushed_commit(
        client, branch, auto_setup_merge=False
    )
    git_in(worktree, "push", "origin", "HEAD:refs/heads/dev/2020-short-description")
    monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: mock_native_daemon)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )
    _seed_e2e_fix_row("2020", with_impl_session=True)

    reconcile()

    fix_sessions = [s for s in load_state().sessions if s.purpose == SessionPurpose.FIX]
    assert len(fix_sessions) == 1
    updated = _only_task()
    assert updated.pending_fix_dispatch is None
    assert updated.fix_dispatch_session_id == fix_sessions[0].id
    assert read_events(event_types=[OrchestratorEventType.STAGE_ERRORED]) == []


def test_reconcile_parks_row_when_no_impl_branch_and_templated_ref_missing(
    tmp_config_dir: Path,
    make_git_repo: Callable[..., Path],
    tmp_path: Path,
    mock_native_daemon: FakeNativeDaemonClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing usable resolves: the row parks for the operator, handoff retained.

    ``branch.autoSetupMerge=false`` makes "nothing resolves" deterministic
    rather than resting on the coincidence that the local branch outruns
    ``origin/main``. With no IMPL session there is no reported name either, so
    only the never-pushed ``origin/dev/2020`` guess is in play.
    """
    client = _make_fix_client(make_git_repo, tmp_path)
    branch = "dev/2020"
    worktree = _seed_worktree_with_unpushed_commit(
        client, branch, auto_setup_merge=False
    )
    git_in(worktree, "push", "origin", "HEAD:refs/heads/dev/2020-short-description")
    monkeypatch.setattr("cw.spawn.get_native_daemon_client", lambda: mock_native_daemon)
    monkeypatch.setattr(
        fix_dispatch, "load_effective_clients", lambda: {_CLIENT: client}
    )
    _seed_e2e_fix_row("2020", with_impl_session=False)

    reconcile()

    assert [s for s in load_state().sessions if s.purpose == SessionPurpose.FIX] == []
    task = _only_task()
    assert task.status == QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == "fix_dispatch_ref_unresolved"
    assert task.pending_fix_dispatch is not None


# --- unresolvable-ref park (#2209) -------------------------------------------


def _raise_unresolved(**_kwargs: Any) -> None:
    msg = (
        "dispatch_fix_agent: cannot determine remote ref for dev/2017 -- "
        "no upstream configured, and origin/dev/2017 does not resolve either."
    )
    raise RemoteRefUnresolvedError(msg)


def test_act_on_pending_fix_dispatches_parks_row_on_unresolved_remote_ref(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one failure class that parks instead of clearing and reverting.

    Since #2075 the generic path charges no attempt, so a ref that can never
    resolve looped review -> failed dispatch -> review without bound. Parking
    BLOCKED_ON_USER ends the loop, keeps the action list for the requeue, and
    pages the operator exactly once.
    """
    stub_dispatch.side_effect = _raise_unresolved
    # session_id matches the handoff's requested_by_session_id, as it does in
    # production: the REVIEW session that recorded the handoff is the session
    # claim.py stamped on the row. The park's ``expected_session_id`` guard
    # requires that identity (see the mismatch test below).
    _seed_task(pending_fix_dispatch=_pending(), session_id="review-sess")

    with caplog.at_level(logging.WARNING, logger="cw.reconcile.fix_dispatch"):
        acted = fix_dispatch._act_on_pending_fix_dispatches(
            [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
            clients={_CLIENT: acme_client},
        )

    assert acted == []
    task = _only_task()
    assert task.status == QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == "fix_dispatch_ref_unresolved"
    assert task.pending_fix_dispatch is not None
    assert task.pending_fix_dispatch.prompt == "fix the MUST_FIX items\n"
    assert task.unproductive_attempts == 0
    assert task.session_id is None

    assert read_events(event_types=[OrchestratorEventType.STAGE_ERRORED]) == []
    attention = read_events(event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION])
    assert len(attention) == 1
    assert attention[0].payload["paused_status"] == "fix_dispatch_ref_unresolved"
    assert "cannot determine remote ref" in attention[0].payload["breadcrumbs"]
    # The row's own session_id, read before the park cleared it.
    assert attention[0].payload["session_id"] == "review-sess"

    assert any(
        rec.message.startswith("fix_dispatch_ref_unresolved ticket=")
        and rec.exc_info is not None
        for rec in caplog.records
    )


def test_parked_unresolved_ref_row_is_not_a_pending_dispatch_candidate() -> None:
    """A parked row must not hold a per-tick cap slot forever.

    Detect runs before the ``_MAX_FIX_DISPATCHES_PER_TICK`` slice, so leaving
    the parked row in the candidate list would starve healthy rows behind it.
    """
    parked = _make_ticket_task(
        ticket_id="2017",
        client=_CLIENT,
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="fix_dispatch_ref_unresolved",
        pending_fix_dispatch=_pending(),
    )
    healthy = [
        _make_ticket_task(
            ticket_id=ticket_id,
            client=_CLIENT,
            status=QueueItemStatus.RUNNING,
            pending_fix_dispatch=_pending(),
        )
        for ticket_id in ("2020", "2021", "2022")
    ]

    candidates = fix_dispatch._detect_pending_fix_dispatches([parked, *healthy])

    assert [c.ticket_id for c in candidates] == ["2020", "2021", "2022"]


def test_parked_unresolved_ref_row_survives_stale_handoff_drop(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
    stub_dispatch: _DispatchRecorder,
) -> None:
    """#2142 interplay: the retained handoff is exempt from the stale drop.

    Every other non-RUNNING row carrying a handoff gets it cleared and paged.
    This one keeps it while parked, preserving the REVIEW round's action list
    as evidence for the operator. (It is not a resume point: a requeue sets the
    row PENDING, and the drop this test exempts then applies normally — #2265.)
    """
    _seed_task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        disposition="fix_dispatch_ref_unresolved",
        pending_fix_dispatch=_pending(),
    )

    acted = fix_dispatch._act_on_pending_fix_dispatches(
        [fix_dispatch._FixDispatchCandidate(ticket_id=_TICKET, client=_CLIENT)],
        clients={_CLIENT: acme_client},
    )

    assert acted == []
    assert stub_dispatch.calls == []
    task = _only_task()
    assert task.pending_fix_dispatch is not None
    assert task.status == QueueItemStatus.BLOCKED_ON_USER
    assert task.disposition == "fix_dispatch_ref_unresolved"
    assert read_events() == []


def test_park_is_noop_when_row_no_longer_running(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
) -> None:
    """The shared park helper matches RUNNING rows only.

    A row already reverted to PENDING is left to the ordinary stale-handoff
    drop rather than being retro-parked out from under it.
    """
    _seed_task(status=QueueItemStatus.PENDING, pending_fix_dispatch=_pending())
    job = fix_dispatch._DispatchJob(
        client_cfg=acme_client,
        branch="dev/2017",
        pending=_pending(),
        ticket_id=_TICKET,
        client=_CLIENT,
        lane="default",
        stage=Stage.REVIEW,
    )

    fix_dispatch._park_for_unresolved_ref(job, RemoteRefUnresolvedError("no ref"))

    task = _only_task()
    assert task.status == QueueItemStatus.PENDING
    assert task.disposition is None
    assert task.pending_fix_dispatch is not None


def test_park_is_noop_when_row_reclaimed_by_a_newer_session(
    tmp_config_dir: Path,
    acme_client: ClientConfig,
) -> None:
    """RUNNING alone does not identify the claim this job belongs to.

    The row was reverted and re-claimed by a newer REVIEW session between the
    unlocked snapshot the job was built from and this park's own lock
    acquisition. It is RUNNING again, so ``(ticket_id, client, RUNNING)``
    matches — but the ref failure belongs to the *previous* claim. Parking here
    would file a healthy, unrelated session as blocked. ``expected_session_id``
    closes that window.
    """
    _seed_task(
        pending_fix_dispatch=_pending(),
        session_id="newer-review-sess",
    )
    job = fix_dispatch._DispatchJob(
        client_cfg=acme_client,
        branch="dev/2017",
        pending=_pending(),  # requested_by_session_id="review-sess"
        ticket_id=_TICKET,
        client=_CLIENT,
        lane="default",
        stage=Stage.REVIEW,
    )

    fix_dispatch._park_for_unresolved_ref(job, RemoteRefUnresolvedError("no ref"))

    task = _only_task()
    assert task.status == QueueItemStatus.RUNNING
    assert task.disposition is None
    assert task.session_id == "newer-review-sess"
    assert task.pending_fix_dispatch is not None
    assert read_events() == []
