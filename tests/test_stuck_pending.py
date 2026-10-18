"""Tests for ``cw.stuck_pending``: which PENDING rows will not clear themselves."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

from cw.auto_dev_result import SCOPE_GATED_APPROVAL_STATUSES, AutoDevResult
from cw.dispatch import TICK_STALE_SECONDS
from cw.events import record_event
from cw.models import (
    DEFAULT_LANE,
    DispatchSkipReason,
    LaneConcurrencyOverride,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.result import reconstruct_staged_sentinel
from cw.stuck_pending import (
    HANDOFF_STUCK_SECONDS,
    NEEDS_ACTION_SKIP_REASONS,
    SELF_CLEARING_SKIP_REASONS,
    STUCK_PENDING_MINUTES,
    PendingBlock,
    PendingTracker,
    classify_pending,
    handoff_unrouted_block,
    is_internal_handoff,
    probe_pending,
    scan_pending,
)
from cw.ticket_view import ViewSources, build_ticket_view
from tests._reconcile_helpers import _fix_loop_pending_payload, _make_terminal_payload
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    install_acme_client,
    make_task,
    seed_queue,
    write_lane_override,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models.ticket_view import TicketView

_NOW = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
_RESUME = f"cw lane resume {ACME} {DEFAULT_LANE}"
_THRESHOLD = STUCK_PENDING_MINUTES * 60


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch, lanes=(DEFAULT_LANE,))


def _seed_pending(**overrides: object) -> None:
    seed_queue([make_task(status=QueueItemStatus.PENDING, **overrides)])


def _view() -> TicketView:
    return build_ticket_view(ACME_TICKET, ACME, now=_NOW, sources=ViewSources())


def _ticket_tick(code: str, at: datetime) -> None:
    with freeze_time(at):
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": ACME,
                "ticket_id": ACME_TICKET,
                "claimed": 0,
                "skip_reason": code,
            },
        )


def _client_tick(code: str, at: datetime) -> None:
    with freeze_time(at):
        record_event(
            OrchestratorEventType.DISPATCH_TICK,
            {
                "client": ACME,
                "claimed": 0,
                "pending": 1,
                "running": 0,
                "cap": 2,
                "skip_reason": code,
            },
        )


def _breaker(*, auto_resume_at: datetime | None) -> LaneConcurrencyOverride:
    return LaneConcurrencyOverride(
        paused=True,
        paused_at=_NOW - timedelta(minutes=5),
        pause_source="circuit_breaker",
        pause_trigger="spawn_errors",
        consecutive_spawn_errors=3,
        circuit_auto_resume_at=auto_resume_at,
    )


class TestSkipReasonPartition:
    def test_sets_partition_dispatch_skip_reason(self) -> None:
        classified = NEEDS_ACTION_SKIP_REASONS | SELF_CLEARING_SKIP_REASONS
        assert classified == {reason.value for reason in DispatchSkipReason}
        assert not NEEDS_ACTION_SKIP_REASONS & SELF_CLEARING_SKIP_REASONS

    @pytest.mark.parametrize(
        "code",
        ["attempt_cap_blocked", "stale_pr_blocked", "tracker_mcp_gate_blocked"],
    )
    def test_parking_codes_are_not_pending_needs_action(self, code: str) -> None:
        # Dispatch parks these rows BLOCKED_ON_USER with a page; `wait` exits 2.
        assert code in SELF_CLEARING_SKIP_REASONS


class TestClassifyPending:
    def test_breaker_pause_with_auto_resume_is_not_stuck(self) -> None:
        _seed_pending()
        write_lane_override(
            ACME,
            DEFAULT_LANE,
            override=_breaker(auto_resume_at=_NOW + timedelta(minutes=10)),
        )
        assert classify_pending(_view()) is None

    def test_breaker_pause_without_auto_resume_is_stuck(self) -> None:
        _seed_pending()
        write_lane_override(ACME, DEFAULT_LANE, override=_breaker(auto_resume_at=None))

        assert classify_pending(_view()) == PendingBlock(
            code="lane_circuit_paused",
            detail=(
                "lane default circuit breaker tripped (spawn_errors);"
                " no auto-resume is scheduled"
            ),
            command=_RESUME,
        )

    def test_breaker_pause_without_trigger_says_unknown(self) -> None:
        _seed_pending()
        override = _breaker(auto_resume_at=None).model_copy(
            update={"pause_trigger": None}
        )
        write_lane_override(ACME, DEFAULT_LANE, override=override)

        block = classify_pending(_view())

        assert block is not None
        assert block.detail == (
            "lane default circuit breaker tripped (unknown);"
            " no auto-resume is scheduled"
        )

    def test_operator_pause_is_stuck_lane_paused(self) -> None:
        _seed_pending()
        write_lane_override(
            ACME,
            DEFAULT_LANE,
            override=LaneConcurrencyOverride(paused=True, pause_source="operator"),
        )

        assert classify_pending(_view()) == PendingBlock(
            code="lane_paused",
            detail="lane default is paused (operator); resume it explicitly",
            command=_RESUME,
        )

    @pytest.mark.parametrize("code", ["cap_full", "lane_cap_blocked"])
    def test_caps_are_not_stuck(self, code: str) -> None:
        _seed_pending()
        _client_tick(code, _NOW - timedelta(seconds=5))
        assert classify_pending(_view()) is None

    def test_attempt_cap_on_the_row_is_never_stuck(self) -> None:
        # A PENDING row at its ceiling exists only until dispatch parks it.
        _seed_pending(unproductive_attempts=99)
        view = _view()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "attempt_cap_blocked"
        assert classify_pending(view) is None

    @pytest.mark.parametrize(
        ("code", "detail"),
        [
            (
                "ssh_key_gate",
                f"no unlocked SSH key for client {ACME}; dispatch is held",
            ),
            ("disk_pressure_gate", "worktree disk is nearly full; dispatch is held"),
        ],
    )
    def test_fresh_gate_is_stuck_without_a_command(
        self, code: str, detail: str
    ) -> None:
        _seed_pending()
        _client_tick(code, _NOW - timedelta(seconds=5))

        assert classify_pending(_view()) == PendingBlock(
            code=code, detail=detail, command=None
        )

    def test_fresh_per_ticket_event_counts(self) -> None:
        _seed_pending()
        _ticket_tick("ssh_key_gate", _NOW - timedelta(seconds=5))
        block = classify_pending(_view())
        assert block is not None
        assert block.code == "ssh_key_gate"

    @pytest.mark.parametrize("tick", [_ticket_tick, _client_tick])
    def test_stale_event_sourced_gate_is_ignored(self, tick: object) -> None:
        _seed_pending()
        assert callable(tick)
        tick("ssh_key_gate", _NOW - timedelta(seconds=TICK_STALE_SECONDS + 1))

        view = _view()

        assert view.dispatch is not None
        assert view.dispatch.stale
        assert classify_pending(view) is None

    def test_requeued_ticket_with_stale_gate_tick_never_alerts(self) -> None:
        # The old ssh_key_gate tick outlived its condition; the client now
        # waits on a lane cap, which is not stuck either.
        _seed_pending()
        _ticket_tick("ssh_key_gate", _NOW - timedelta(minutes=20))
        _client_tick("lane_cap_blocked", _NOW - timedelta(seconds=5))
        assert classify_pending(_view()) is None

    def test_row_sourced_breaker_pause_is_stuck_regardless_of_ticks(self) -> None:
        _seed_pending()
        write_lane_override(ACME, DEFAULT_LANE, override=_breaker(auto_resume_at=None))
        _ticket_tick("cap_full", _NOW - timedelta(minutes=20))

        view = _view()

        assert view.dispatch is not None
        assert view.dispatch.source.value == "row"
        block = classify_pending(view)
        assert block is not None
        assert block.code == "lane_circuit_paused"

    def test_no_dispatch_signal_is_not_stuck(self) -> None:
        _seed_pending()
        assert classify_pending(_view()) is None

    def test_non_pending_row_is_not_stuck(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="s-1")])
        assert classify_pending(_view()) is None


class TestProbeAndScan:
    def test_vanished_row_is_skipped(self) -> None:
        seed_queue([])
        assert probe_pending(ACME_TICKET, ACME, now=_NOW, sources=ViewSources()) is None

    def test_scan_skips_vanished_and_non_pending_rows(self) -> None:
        pending = make_task(status=QueueItemStatus.PENDING)
        running = make_task(
            ticket_id="ACM-2", status=QueueItemStatus.RUNNING, session_id="s-2"
        )
        ghost = make_task(ticket_id="ACM-3", status=QueueItemStatus.PENDING)
        seed_queue([pending, running])
        write_lane_override(ACME, DEFAULT_LANE, override=_breaker(auto_resume_at=None))

        probes = scan_pending([pending, running, ghost], now=_NOW)

        assert [p.view.ticket_id for p in probes] == [ACME_TICKET]
        assert probes[0].block is not None
        assert probes[0].block.code == "lane_circuit_paused"


_BLOCK_A = PendingBlock(code="ssh_key_gate", detail="a", command=None)
_BLOCK_B = PendingBlock(code="lane_paused", detail="b", command=_RESUME)


class TestPendingTracker:
    def test_no_alert_before_threshold(self) -> None:
        tracker = PendingTracker(threshold_seconds=_THRESHOLD)
        assert tracker.observe("T", _BLOCK_A, at=100.0) is None
        assert tracker.observe("T", _BLOCK_A, at=100.0 + _THRESHOLD - 1) is None

    def test_exactly_one_alert_after_threshold(self) -> None:
        tracker = PendingTracker(threshold_seconds=_THRESHOLD)
        tracker.observe("T", _BLOCK_A, at=0.0)

        assert tracker.observe("T", _BLOCK_A, at=_THRESHOLD + 5) == _THRESHOLD + 5
        assert tracker.observe("T", _BLOCK_A, at=_THRESHOLD * 3) is None

    def test_code_change_re_arms(self) -> None:
        tracker = PendingTracker(threshold_seconds=_THRESHOLD)
        tracker.observe("T", _BLOCK_A, at=0.0)
        assert tracker.observe("T", _BLOCK_A, at=_THRESHOLD) == _THRESHOLD

        assert tracker.observe("T", _BLOCK_B, at=_THRESHOLD + 1) is None
        assert tracker.observe("T", _BLOCK_B, at=_THRESHOLD * 2 + 1) == _THRESHOLD
        assert tracker.observe("T", _BLOCK_A, at=_THRESHOLD * 2 + 2) is None
        assert tracker.observe("T", _BLOCK_A, at=_THRESHOLD * 3 + 2) == _THRESHOLD

    def test_clears_when_block_goes_away(self) -> None:
        tracker = PendingTracker(threshold_seconds=_THRESHOLD)
        tracker.observe("T", _BLOCK_A, at=0.0)
        tracker.observe("T", None, at=10.0)

        assert tracker.observe("T", _BLOCK_A, at=20.0) is None
        assert tracker.observe("T", _BLOCK_A, at=20.0 + _THRESHOLD) == _THRESHOLD

    def test_retain_forgets_rows_that_left_pending(self) -> None:
        tracker = PendingTracker(threshold_seconds=_THRESHOLD)
        tracker.observe("T", _BLOCK_A, at=0.0)
        tracker.observe("U", _BLOCK_A, at=0.0)

        tracker.retain({"U"})

        assert tracker.observe("T", _BLOCK_A, at=_THRESHOLD) is None
        assert tracker.observe("U", _BLOCK_A, at=_THRESHOLD) == _THRESHOLD


def _sentinel(payload: dict[str, object]) -> AutoDevResult:
    sentinel = reconstruct_staged_sentinel(payload)
    assert isinstance(sentinel, AutoDevResult)
    return sentinel


def _ordinary_blocked() -> dict[str, object]:
    return {
        **_make_terminal_payload("blocked", ACME_TICKET),
        "blocker": {"stage": "stage1_plan", "reason": "impl_failed"},
    }


def _shipped() -> dict[str, object]:
    return {
        **_make_terminal_payload("merge_pending", ACME_TICKET),
        "status": "shipped",
        "next_actions": ["wait_for_ci"],
    }


class TestIsInternalHandoff:
    def test_fix_loop_blocked_is_a_handoff(self) -> None:
        assert is_internal_handoff(_sentinel(_fix_loop_pending_payload()))

    @pytest.mark.parametrize("status", sorted(SCOPE_GATED_APPROVAL_STATUSES))
    def test_scope_gated_statuses_are_handoffs(self, status: str) -> None:
        assert is_internal_handoff(_sentinel(_make_terminal_payload(status, "T-1")))

    def test_ordinary_blocked_is_not(self) -> None:
        assert not is_internal_handoff(_sentinel(_ordinary_blocked()))

    def test_shipped_is_not(self) -> None:
        assert not is_internal_handoff(_sentinel(_shipped()))


class TestHandoffUnroutedBlock:
    def test_detail_names_status_and_seconds(self) -> None:
        block = handoff_unrouted_block("review_pending_approval", HANDOFF_STUCK_SECONDS)
        assert block == PendingBlock(
            code="handoff_unrouted",
            detail=(
                "dispatch has not routed the recorded review_pending_approval"
                " result for 300s; is `cw dev-queue run` alive?"
            ),
            command=None,
        )
