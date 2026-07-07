"""Tests for cw.reconcile.escalation (RFC 0008 capstone, GitHub #1015)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import freezegun
import pytest

from cw.config import save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue, save_dev_queue
from cw.events import read_events
from cw.models import (
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    TicketTask,
)
from cw.reconcile.escalation import ESCALATION_PARK_MINUTES, run_escalation_sweep

_NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)

# The 7 escalation-eligible (status, disposition) combos per the binding
# two-branch formula.
_ELIGIBLE_COMBOS: list[tuple[QueueItemStatus, str | None]] = [
    (QueueItemStatus.BLOCKED_ON_USER, "ambiguities_pending_resolution"),
    (QueueItemStatus.BLOCKED_ON_USER, "plan_pending_approval"),
    (QueueItemStatus.BLOCKED_ON_USER, "review_pending_approval"),
    (QueueItemStatus.BLOCKED_ON_USER, "stalled_retry_cap_parked"),
    # None: recipe 1 (false_park_requeue)'s null-disposition target (the
    # idle-watchdog's silently-idle park) is ceiling-refusable exactly like
    # stalled_retry_cap_parked — a ceiling-refused row here must also
    # escalate, or it's a silent stuck row. Review follow-up, see
    # cw.reconcile.escalation._ELIGIBLE_DISPOSITIONS.
    #
    # #976: fixing the null-disposition park bug means idle.py now stamps
    # "idle_stall" instead of None for its wall-clock-budget park path, so
    # None here now covers only dispositions this suite doesn't otherwise
    # exercise (e.g. pre-#976 legacy rows) — the 4 ReapReason values below
    # cover the reconcile park paths that used to fall through to None.
    (QueueItemStatus.BLOCKED_ON_USER, None),
    (QueueItemStatus.BLOCKED_ON_USER, "idle_stall"),
    (QueueItemStatus.BLOCKED_ON_USER, "wall_clock_budget"),
    (QueueItemStatus.BLOCKED_ON_USER, "phantom_surface"),
    (QueueItemStatus.BLOCKED_ON_USER, "silently_idle"),
    (QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, None),
    (QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, "signoff_gate"),
    (QueueItemStatus.FAILED, None),
    (QueueItemStatus.FAILED, "abandoned"),
]


def _make_task(
    ticket_id: str = "GEN-1",
    client: str = "acme",
    status: QueueItemStatus = QueueItemStatus.BLOCKED_ON_USER,
    disposition: str | None = None,
    escalation_parked_at: datetime | None = None,
    escalation_fired_at: datetime | None = None,
) -> TicketTask:
    return TicketTask(
        ticket_id=ticket_id,
        client=client,
        status=status,
        disposition=disposition,
        escalation_parked_at=escalation_parked_at,
        escalation_fired_at=escalation_fired_at,
    )


class TestEligibilityFormula:
    @pytest.mark.parametrize(("status", "disposition"), _ELIGIBLE_COMBOS)
    def test_eligible_combo_stamps_then_fires_then_latches(
        self,
        tmp_config_dir: Path,
        status: QueueItemStatus,
        disposition: str | None,
    ) -> None:
        task = _make_task(status=status, disposition=disposition)
        save_dev_queue(DevQueueStore(tasks=[task]))

        # Tick 1: enters the eligible set — stamps escalation_parked_at, no fire.
        fired = run_escalation_sweep(now=_NOW)
        assert fired == []
        store = load_dev_queue()
        parked_at = store.tasks[0].escalation_parked_at
        assert parked_at == _NOW
        assert store.tasks[0].escalation_fired_at is None

        # Tick 2: still under threshold — no fire yet.
        fired = run_escalation_sweep(
            now=_NOW + timedelta(minutes=ESCALATION_PARK_MINUTES - 1)
        )
        assert fired == []
        store = load_dev_queue()
        assert store.tasks[0].escalation_fired_at is None

        # Tick 3: threshold crossed — fires exactly once.
        fire_time = _NOW + timedelta(minutes=ESCALATION_PARK_MINUTES)
        fired = run_escalation_sweep(now=fire_time)
        assert fired == ["GEN-1"]
        store = load_dev_queue()
        assert store.tasks[0].escalation_fired_at == fire_time

        # Tick 4: latch — does not re-fire.
        fired = run_escalation_sweep(
            now=fire_time + timedelta(minutes=ESCALATION_PARK_MINUTES)
        )
        assert fired == []

    def test_premises_pending_verification_not_eligible(
        self, tmp_config_dir: Path
    ) -> None:
        """premises_pending_verification is DELIBERATELY EXCLUDED."""
        task = _make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="premises_pending_verification",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        fired = run_escalation_sweep(now=_NOW)

        assert fired == []
        store = load_dev_queue()
        assert store.tasks[0].escalation_parked_at is None

    def test_pending_status_not_eligible(self, tmp_config_dir: Path) -> None:
        task = _make_task(status=QueueItemStatus.PENDING, disposition=None)
        save_dev_queue(DevQueueStore(tasks=[task]))

        fired = run_escalation_sweep(now=_NOW)

        assert fired == []
        store = load_dev_queue()
        assert store.tasks[0].escalation_parked_at is None

    def test_completed_status_not_eligible(self, tmp_config_dir: Path) -> None:
        task = _make_task(status=QueueItemStatus.COMPLETED, disposition="shipped")
        save_dev_queue(DevQueueStore(tasks=[task]))

        fired = run_escalation_sweep(now=_NOW)

        assert fired == []

    def test_blocked_on_user_with_unrelated_disposition_not_eligible(
        self, tmp_config_dir: Path
    ) -> None:
        task = _make_task(
            status=QueueItemStatus.BLOCKED_ON_USER, disposition="dirty_worktree"
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        fired = run_escalation_sweep(now=_NOW)

        assert fired == []


class TestFlatThreshold:
    def test_threshold_is_flat_not_per_stage(self, tmp_config_dir: Path) -> None:
        """P1: a flat 45-min threshold regardless of Stage — unlike concierge's
        own per-stage-floor transcript check, which is a different mechanism."""
        from cw.models import Stage

        for stage in (Stage.PLAN, Stage.IMPL, Stage.REVIEW, Stage.FINALIZE):
            task = TicketTask(
                ticket_id=f"GEN-{stage.value}",
                client="acme",
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition="plan_pending_approval",
                stage=stage,
            )
            save_dev_queue(DevQueueStore(tasks=[task]))
            run_escalation_sweep(now=_NOW)
            fired = run_escalation_sweep(
                now=_NOW + timedelta(minutes=ESCALATION_PARK_MINUTES)
            )
            assert fired == [f"GEN-{stage.value}"], f"stage={stage} should fire flat"


class TestEventPayload:
    def test_operator_escalation_event_payload(self, tmp_config_dir: Path) -> None:
        task = _make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="review_pending_approval",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        run_escalation_sweep(now=_NOW)
        run_escalation_sweep(now=_NOW + timedelta(minutes=ESCALATION_PARK_MINUTES))

        events = read_events(
            consumer="test-escalation-payload",
            event_types=[OrchestratorEventType.OPERATOR_ESCALATION],
        )
        assert len(events) == 1
        assert events[0].payload["ticket_id"] == "GEN-1"
        assert events[0].payload["client"] == "acme"
        assert events[0].correlation_id == "GEN-1"


class TestClearOnExit:
    def test_fields_clear_via_transition_task_status_seam(
        self, tmp_config_dir: Path
    ) -> None:
        """Latch fields clear when the row leaves the eligible set — via the
        transition_task_status seam, not via this module."""
        from cw.dev_queue import transition_task_status

        task = _make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="plan_pending_approval",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        run_escalation_sweep(now=_NOW)
        run_escalation_sweep(now=_NOW + timedelta(minutes=ESCALATION_PARK_MINUTES))
        store = load_dev_queue()
        parked = store.tasks[0]
        assert parked.escalation_parked_at is not None
        assert parked.escalation_fired_at is not None

        transition_task_status(parked, QueueItemStatus.PENDING)
        save_dev_queue(store)

        store = load_dev_queue()
        assert store.tasks[0].escalation_parked_at is None
        assert store.tasks[0].escalation_fired_at is None

    def test_run_escalation_sweep_never_clears_fields_itself(
        self, tmp_config_dir: Path
    ) -> None:
        """This module only ever SETS the latch fields, never clears them —
        even for a non-eligible row that happens to carry stale values."""
        stale_parked_at = _NOW - timedelta(days=10)
        stale_fired_at = _NOW - timedelta(days=9)
        task = _make_task(
            status=QueueItemStatus.PENDING,
            disposition=None,
            escalation_parked_at=stale_parked_at,
            escalation_fired_at=stale_fired_at,
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        run_escalation_sweep(now=_NOW)

        store = load_dev_queue()
        assert store.tasks[0].escalation_parked_at == stale_parked_at
        assert store.tasks[0].escalation_fired_at == stale_fired_at


class TestLockScope:
    def test_does_not_touch_session_state(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Self-contained: never touches Session state / sessions_lock."""
        calls: list[str] = []
        monkeypatch.setattr(
            "cw.config.sessions_lock",
            lambda: (_ for _ in ()).throw(AssertionError("must not lock sessions")),
        )
        task = _make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="plan_pending_approval",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))
        save_state(CwState(sessions=[]))

        # Should not raise — run_escalation_sweep never imports/calls
        # sessions_lock at all.
        run_escalation_sweep(now=_NOW)
        assert calls == []

    def test_acquires_its_own_dev_queue_lock(self, tmp_config_dir: Path) -> None:
        """Standalone-callable: acquiring dev_queue_lock directly beforehand
        would deadlock if run_escalation_sweep tried to acquire it again while
        we already hold it — so we assert it can be called on its own without
        any pre-held lock (the realistic call shape from `cw watchdog tick`)."""
        task = _make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="plan_pending_approval",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        fired = run_escalation_sweep(now=_NOW)
        assert fired == []

        # Confirm the lock is released afterward — a subsequent direct
        # acquisition must not block.
        with dev_queue_lock():
            pass


class TestDefaultNow:
    def test_defaults_to_datetime_now_utc(self, tmp_config_dir: Path) -> None:
        fixed = datetime(2026, 1, 1, tzinfo=UTC)
        task = _make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            disposition="plan_pending_approval",
        )
        save_dev_queue(DevQueueStore(tasks=[task]))

        with freezegun.freeze_time(fixed):
            run_escalation_sweep()

        store = load_dev_queue()
        assert store.tasks[0].escalation_parked_at == fixed
