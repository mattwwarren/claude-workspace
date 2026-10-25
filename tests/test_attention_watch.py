"""Tests for ``cw.attention_watch``: scope, dedupe latch and line rendering (#2693).

The pure half of ``cw orchestrate watch``. The stateful loop (cursor, pid
advisory, stuck scan) is ``tests/test_attention_stream.py``. The rendering,
liveness and breadcrumb cases are ported from the retired skill script
``attention_watch.py`` (#2250, #2004, #1597), now against the real ``cw``
constants instead of hand-copies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.attention_watch import (
    BREADCRUMB_PAUSED_STATUSES,
    CLEAR_TYPES,
    LATCH_MAX_KEYS,
    SURFACED_LIVENESS,
    WATCHED_TYPES,
    Latch,
    RowIndex,
    ScopeSource,
    WatchScope,
    exit_line,
    first_next_command,
    in_scope,
    refused_reason,
    render_event,
    render_scope_line,
    render_stuck,
    resolve_scope,
)
from cw.dev_queue import load_dev_queue
from cw.dispatch import BREADCRUMB_ELIGIBLE_PAUSED_STATUSES
from cw.dispatch.regress_repeat import _FINALIZE_REGRESS_REPEAT_REASON
from cw.exceptions import CwError
from cw.focus import set_focus
from cw.models import (
    DEFAULT_LANE,
    LivenessBucket,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.reconcile._shared import (
    SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON,
    SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON,
)
from cw.stuck_pending import PendingBlock
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    make_task,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

_CREATED = datetime(2026, 1, 1, 0, 0, 5, 500000, tzinfo=UTC)
_NA = OrchestratorEventType.SESSION_NEEDS_ATTENTION
_LIVENESS = OrchestratorEventType.SESSION_LIVENESS_CHANGED
_FAST = "fast"


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch, lanes=(DEFAULT_LANE, _FAST))


def _event(etype: OrchestratorEventType = _NA, **payload: object) -> OrchestratorEvent:
    return OrchestratorEvent(type=etype, payload=payload, created_at=_CREATED)


def _page(**payload: object) -> OrchestratorEvent:
    base: dict[str, object] = {
        "client": ACME,
        "ticket_id": ACME_TICKET,
        "paused_status": "blocked",
    }
    return _event(_NA, **{**base, **payload})


def _liveness(
    *,
    ticket_id: str = "T-123",
    new_bucket: str = "stale_30m",
    stale_minutes: float = 32.4,
    session_id: str = "sess-abcdef1234567890",
) -> OrchestratorEvent:
    return _event(
        _LIVENESS,
        client=ACME,
        session_id=session_id,
        ticket_id=ticket_id,
        stage="impl",
        old_bucket="stale_15m",
        new_bucket=new_bucket,
        stale_minutes=stale_minutes,
    )


def _scope(
    *,
    lanes: frozenset[str] = frozenset(),
    tickets: frozenset[str] = frozenset(),
    all_lanes: bool = False,
    client: str = ACME,
) -> WatchScope:
    return WatchScope(
        client=client,
        lanes=lanes,
        tickets=tickets,
        all_lanes=all_lanes,
        source=ScopeSource.FLAGS,
    )


def _rows() -> RowIndex:
    return RowIndex(load_dev_queue(), ACME)


def _render_all(events: list[OrchestratorEvent]) -> list[str]:
    """Run *events* through clear, latch and render like the stream does."""
    latch = Latch()
    lines: list[str] = []
    for event in events:
        if latch.clear(event) or not latch.admit(event):
            continue
        lines.append(render_event(event, None))
    return lines


# --- constants ---------------------------------------------------------------


class TestConstants:
    def test_watched_types_are_the_seven_attention_types(self) -> None:
        assert {t.value for t in WATCHED_TYPES} == {
            "session.needs_attention",
            "operator.escalation",
            "session.timed_out",
            "session.reap_proposed",
            "session.stage_timed_out_retried",
            "session.phantom_reverted",
            "session.liveness_changed",
        }

    def test_clear_types(self) -> None:
        assert {t.value for t in CLEAR_TYPES} == {
            "task.transition",
            "ticket.requeued",
            "lane.resumed",
            "dispatch.tick",
        }

    def test_surfaced_liveness_is_derived_from_the_bucket_enum(self) -> None:
        assert frozenset(
            {LivenessBucket.STALE_30M.value, LivenessBucket.STALE_45M.value}
        ) == (SURFACED_LIVENESS)

    def test_breadcrumb_allowlist_uses_the_routing_constants(self) -> None:
        assert BREADCRUMB_ELIGIBLE_PAUSED_STATUSES <= BREADCRUMB_PAUSED_STATUSES
        assert {
            _FINALIZE_REGRESS_REPEAT_REASON,
            SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON,
            SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON,
        } <= BREADCRUMB_PAUSED_STATUSES


# --- scope resolution ---------------------------------------------------------


class TestResolveScope:
    def test_focus_lane_is_the_default(self) -> None:
        set_focus("sess-1", ACME, _FAST)
        scope = resolve_scope(ACME, (), (), all_lanes=False, session_id="sess-1")
        assert scope.lanes == frozenset({_FAST})
        assert scope.source == ScopeSource.FOCUS

    def test_focus_supplies_the_client(self) -> None:
        set_focus("sess-1", ACME, _FAST)
        scope = resolve_scope(None, (), (), all_lanes=False, session_id="sess-1")
        assert scope.client == ACME

    def test_focus_for_another_client_is_ignored(self) -> None:
        set_focus("sess-1", "other", _FAST)
        scope = resolve_scope(ACME, (), (), all_lanes=False, session_id="sess-1")
        assert scope.everything
        assert scope.source == ScopeSource.CLIENT

    def test_client_only_focus_watches_the_whole_client(self) -> None:
        set_focus("sess-1", ACME, None)
        scope = resolve_scope(ACME, (), (), all_lanes=False, session_id="sess-1")
        assert scope.everything
        assert scope.source == ScopeSource.CLIENT

    def test_all_lanes_ignores_focus(self) -> None:
        set_focus("sess-1", ACME, _FAST)
        scope = resolve_scope(ACME, (), (), all_lanes=True, session_id="sess-1")
        assert scope.everything
        assert scope.source == ScopeSource.ALL_LANES

    def test_ticket_only_scope_does_not_add_focus_lane(self) -> None:
        set_focus("sess-1", ACME, _FAST)
        scope = resolve_scope(ACME, (), ("T-9",), all_lanes=False, session_id="sess-1")
        assert scope.lanes == frozenset()
        assert scope.tickets == frozenset({"T-9"})
        assert scope.source == ScopeSource.FLAGS

    def test_no_client_and_no_focus_raises(self) -> None:
        with pytest.raises(CwError, match="--client"):
            resolve_scope(None, (), (), all_lanes=False, session_id=None)

    def test_consumer_names_differ_by_scope(self) -> None:
        a = _scope(lanes=frozenset({_FAST})).consumer
        b = _scope(lanes=frozenset({DEFAULT_LANE})).consumer
        c = _scope().consumer
        assert len({a, b, c}) == 3
        assert a.startswith(f"attention-{ACME}-")
        assert len(a.rsplit("-", 1)[1]) == 8

    def test_consumer_name_replaces_slashes_and_is_stable(self) -> None:
        scope = _scope(client="org/acme", tickets=frozenset({"B", "A"}))
        again = _scope(client="org/acme", tickets=frozenset({"A", "B"}))
        assert scope.consumer.startswith("attention-org-acme-")
        assert scope.consumer == again.consumer


class TestScopeLine:
    def test_focus_scope_line(self) -> None:
        scope = WatchScope(
            client=ACME,
            lanes=frozenset({_FAST}),
            tickets=frozenset(),
            all_lanes=False,
            source=ScopeSource.FOCUS,
        )
        assert render_scope_line(scope, "resume") == (
            f"WATCH | scope | client={ACME} | lanes=fast | tickets=- | source=focus"
            f" | start=resume | cursor={scope.consumer}"
        )

    def test_ticket_only_and_everything_markers(self) -> None:
        ticket_only = _scope(tickets=frozenset({"T-2", "T-1"}))
        assert "| lanes=- | tickets=T-1,T-2 |" in render_scope_line(ticket_only, "now")
        everything = _scope(lanes=frozenset({"b", "a"}), all_lanes=True)
        assert "| lanes=* |" in render_scope_line(everything, "since")


# --- in_scope -----------------------------------------------------------------


class TestInScope:
    def test_other_client_dropped(self) -> None:
        assert not in_scope(_page(client="other"), _scope(), _rows())

    def test_event_without_client_dropped(self) -> None:
        assert not in_scope(_event(_NA, ticket_id="T"), _scope(), _rows())

    def test_whole_client_keeps_everything(self) -> None:
        assert in_scope(_page(lane="anything"), _scope(), _rows())

    def test_sibling_lane_event_dropped(self) -> None:
        scope = _scope(lanes=frozenset({_FAST}))
        assert not in_scope(_page(lane=DEFAULT_LANE), scope, _rows())
        assert in_scope(_page(lane=_FAST), scope, _rows())

    def test_laneless_liveness_resolves_through_the_row_lane(self) -> None:
        seed_queue([make_task(ticket_id="T-123", lane=_FAST)])
        assert in_scope(_liveness(), _scope(lanes=frozenset({_FAST})), _rows())
        assert not in_scope(_liveness(), _scope(lanes=frozenset({"x"})), _rows())

    def test_unknown_ticket_with_lane_set_fails_open(self) -> None:
        assert in_scope(
            _liveness(ticket_id="GONE"), _scope(lanes=frozenset({_FAST})), _rows()
        )

    def test_client_level_page_kept_under_every_scope(self) -> None:
        stale = _event(
            _NA, client=ACME, ticket_id=None, paused_status="dispatch_loop_stale"
        )
        assert in_scope(stale, _scope(lanes=frozenset({_FAST})), _rows())
        assert in_scope(stale, _scope(tickets=frozenset({"T-1"})), _rows())

    def test_ticket_union_with_lanes(self) -> None:
        seed_queue([make_task(ticket_id="T-7", lane=DEFAULT_LANE)])
        scope = _scope(lanes=frozenset({_FAST}), tickets=frozenset({"T-7"}))
        assert in_scope(_page(ticket_id="T-7", lane=DEFAULT_LANE), scope, _rows())
        assert in_scope(_page(ticket_id="T-8", lane=_FAST), scope, _rows())

    def test_ticket_only_scope_drops_other_tickets(self) -> None:
        seed_queue([make_task(ticket_id="T-8", lane=_FAST)])
        scope = _scope(tickets=frozenset({"T-7"}))
        assert not in_scope(_page(ticket_id="T-8"), scope, _rows())
        assert not in_scope(_liveness(ticket_id="GONE"), scope, _rows())
        assert in_scope(_page(ticket_id="T-7"), scope, _rows())


def _starved(lane: str) -> OrchestratorEvent:
    return _event(
        _NA,
        client=ACME,
        ticket_id=None,
        paused_status="lane_circuit_paused",
        breadcrumbs="pending=2",
        lane=lane,
        session_id=f"lane:{ACME}/{lane}@2026-01-01T00:00:05+00:00",
    )


class TestStarvedLanePage:
    def test_kept_for_lane_in_scope(self) -> None:
        assert in_scope(_starved(_FAST), _scope(lanes=frozenset({_FAST})), _rows())

    def test_dropped_for_another_lane(self) -> None:
        scope = _scope(lanes=frozenset({_FAST}))
        assert not in_scope(_starved(DEFAULT_LANE), scope, _rows())

    def test_ticket_only_scope_keeps_it_only_for_the_tickets_lane(self) -> None:
        seed_queue([make_task(ticket_id="T-7", lane=_FAST)])
        scope = _scope(tickets=frozenset({"T-7"}))
        assert in_scope(_starved(_FAST), scope, _rows())
        assert not in_scope(_starved(DEFAULT_LANE), scope, _rows())

    def test_renders_unknown_ticket_and_no_command(self) -> None:
        line = render_event(_starved(_FAST), None)
        assert "| #? | lane_circuit_paused |" in line
        assert line.endswith("| next: -")


# --- latch --------------------------------------------------------------------


class TestLatch:
    def test_repeat_suppressed(self) -> None:
        latch = Latch()
        assert latch.admit(_page(session_id="s-1"))
        assert not latch.admit(_page(session_id="s-2"))

    def test_starved_lane_repeat_suppressed_despite_new_session_id(self) -> None:
        latch = Latch()
        assert latch.admit(_starved(_FAST))
        later = _starved(_FAST).model_copy(
            update={"payload": {**_starved(_FAST).payload, "session_id": "lane:x@2"}}
        )
        assert not latch.admit(later)

    def test_renotify_marker_change_re_pages(self) -> None:
        latch = Latch()
        assert latch.admit(_page(renotify_marker="m1"))
        assert not latch.admit(_page(renotify_marker="m1"))
        assert latch.admit(_page(renotify_marker="m2"))

    def test_different_status_is_a_different_key(self) -> None:
        latch = Latch()
        assert latch.admit(_page(paused_status="blocked"))
        assert latch.admit(_page(paused_status="merge_gate_blocked"))

    def test_task_transition_clears_only_its_ticket(self) -> None:
        latch = Latch()
        latch.admit(_page(ticket_id="T-1"))
        latch.admit(_page(ticket_id="T-2"))
        transition = _event(
            OrchestratorEventType.TASK_TRANSITION,
            client=ACME,
            ticket_id="T-1",
            lane=DEFAULT_LANE,
        )
        assert latch.clear(transition)
        assert latch.admit(_page(ticket_id="T-1"))
        assert not latch.admit(_page(ticket_id="T-2"))

    def test_ticket_requeued_clears_its_ticket(self) -> None:
        latch = Latch()
        latch.admit(_page())
        requeued = _event(
            OrchestratorEventType.TICKET_REQUEUED, client=ACME, ticket_id=ACME_TICKET
        )
        assert latch.clear(requeued)
        assert latch.admit(_page())

    def test_clear_for_another_client_clears_nothing(self) -> None:
        latch = Latch()
        latch.admit(_page())
        requeued = _event(
            OrchestratorEventType.TICKET_REQUEUED, client="other", ticket_id=ACME_TICKET
        )
        assert latch.clear(requeued)
        assert not latch.admit(_page())

    def test_lane_resumed_clears_lane_keys_only(self) -> None:
        latch = Latch()
        latch.admit(_starved(_FAST))
        latch.admit(_starved(DEFAULT_LANE))
        latch.admit(_page())
        resumed = _event(OrchestratorEventType.LANE_RESUMED, client=ACME, lane=_FAST)
        assert latch.clear(resumed)
        assert latch.admit(_starved(_FAST))
        assert not latch.admit(_starved(DEFAULT_LANE))
        assert not latch.admit(_page())

    def test_client_wide_tick_clears_dispatch_loop_stale(self) -> None:
        stale = _event(
            _NA, client=ACME, ticket_id=None, paused_status="dispatch_loop_stale"
        )
        latch = Latch()
        latch.admit(stale)
        latch.admit(_page())
        tick = _event(
            OrchestratorEventType.DISPATCH_TICK, client=ACME, claimed=0, cap=2
        )
        assert latch.clear(tick)
        assert latch.admit(stale)
        assert not latch.admit(_page())

    def test_per_ticket_tick_clears_nothing(self) -> None:
        stale = _event(
            _NA, client=ACME, ticket_id=None, paused_status="dispatch_loop_stale"
        )
        latch = Latch()
        latch.admit(stale)
        latch.admit(_page())
        tick = _event(
            OrchestratorEventType.DISPATCH_TICK,
            client=ACME,
            ticket_id=ACME_TICKET,
            skip_reason="attempt_cap_blocked",
        )
        assert latch.clear(tick)
        assert not latch.admit(stale)
        assert not latch.admit(_page())

    def test_watched_event_is_not_a_clear(self) -> None:
        assert not Latch().clear(_page())

    def test_round_trip(self) -> None:
        latch = Latch()
        latch.admit(_page(renotify_marker="m"))
        restored = Latch.from_dict(latch.to_dict())
        assert not restored.admit(_page(renotify_marker="m"))
        assert restored.to_dict() == latch.to_dict()

    @pytest.mark.parametrize("raw", [None, [], {"k": 1}, "x"])
    def test_from_dict_tolerates_garbage(self, raw: object) -> None:
        assert Latch.from_dict(raw).to_dict() == {}

    def test_size_cap_evicts_oldest(self) -> None:
        latch = Latch()
        for n in range(LATCH_MAX_KEYS + 1):
            latch.admit(_page(ticket_id=f"T-{n}"))
        assert len(latch.to_dict()) == LATCH_MAX_KEYS
        assert latch.admit(_page(ticket_id="T-0"))
        assert not latch.admit(_page(ticket_id=f"T-{LATCH_MAX_KEYS}"))


# --- liveness (ported from the script's latch) --------------------------------


class TestLiveness:
    def test_live_and_stale_15m_suppressed(self) -> None:
        assert _render_all([_liveness(new_bucket="live")]) == []
        assert _render_all([_liveness(new_bucket="stale_15m")]) == []

    def test_stale_30m_and_45m_surfaced(self) -> None:
        out30 = _render_all([_liveness(new_bucket="stale_30m", stale_minutes=32.4)])
        out45 = _render_all([_liveness(new_bucket="stale_45m", stale_minutes=47.1)])
        assert len(out30) == 1
        assert "#T-123" in out30[0]
        assert "stage=impl" in out30[0]
        assert "stale_30m" in out30[0]
        assert "stale_m=32.4" in out30[0]
        assert len(out45) == 1
        assert "stale_45m" in out45[0]

    def test_per_stage_duration_not_implied(self) -> None:
        out = _render_all([_liveness(stale_minutes=34.9)])
        assert len(out) == 1
        assert "34.9" in out[0]
        assert "35" not in out[0].replace("34.9", "")

    def test_repeat_same_bucket_suppressed(self) -> None:
        assert len(_render_all([_liveness(), _liveness()])) == 1

    def test_escalation_always_surfaced(self) -> None:
        out = _render_all([_liveness(), _liveness(new_bucket="stale_45m")])
        assert len(out) == 2
        assert "stale_30m" in out[0]
        assert "stale_45m" in out[1]

    def test_recovery_then_restall_same_bucket_both_surfaced(self) -> None:
        out = _render_all([_liveness(), _liveness(new_bucket="live"), _liveness()])
        assert len(out) == 2

    def test_distinct_tickets_not_cross_suppressed(self) -> None:
        out = _render_all([_liveness(ticket_id="T-1"), _liveness(ticket_id="T-2")])
        assert len(out) == 2

    def test_malformed_bucket_value_not_crashing(self) -> None:
        missing = _event(_LIVENESS, client=ACME, session_id="sess-x", ticket_id="T-1")
        assert _render_all([missing]) == []
        assert _render_all([_liveness(new_bucket="stale_99m")]) == []


# --- rendering ----------------------------------------------------------------


class TestRenderEvent:
    def test_needs_attention_rendering(self) -> None:
        event = _page(
            ticket_id="T-999",
            breadcrumbs="some breadcrumb text",
            stage="review",
            attempts=3,
            lane="debt",
            session_id="sess-needsattn00000",
        )
        line = render_event(event, "cw dev-queue requeue T-999 -c acme")
        assert line == (
            "ATTENTION | 2026-01-01T00:00:05 | session.needs_attention | #T-999"
            " | blocked | stage=review att=3 lane=debt reason=some breadcrumb text"
            " | sess-nee | next: cw dev-queue requeue T-999 -c acme"
        )

    def test_breadcrumbs_hidden_for_non_allowlisted_status(self) -> None:
        line = render_event(
            _page(paused_status="finalize_hold", breadcrumbs="/some/worktree"), None
        )
        assert "reason=" not in line

    @pytest.mark.parametrize(
        ("status", "crumbs"),
        [
            pytest.param(
                "merge_gate_blocked",
                "prior_pipeline_pr_open occurrence=2"
                " next_eligible_at=2026-07-16T12:02:00+00:00",
                id="merge_gate_blocked",
            ),
            pytest.param(
                "blocked",
                "prior_pipeline_pr_open occurrence=2"
                " next_eligible_at=2026-07-16T12:02:00+00:00",
                id="blocked",
            ),
        ],
    )
    def test_gate_block_occurrence_note_surfaced(
        self, status: str, crumbs: str
    ) -> None:
        """#2717: the gate-block backoff note rides the Rule 5 breadcrumbs."""
        line = render_event(_page(paused_status=status, breadcrumbs=crumbs), None)
        assert f"reason={crumbs}" in line

    def test_finalize_regress_repeat_breadcrumbs_surfaced(self) -> None:
        event = _page(paused_status="finalize_regress_repeat", breadcrumbs="attempts=3")
        assert "reason=attempts=3" in render_event(event, None)

    @pytest.mark.parametrize(
        "status",
        [
            SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON,
            SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON,
        ],
    )
    def test_stage_mismatch_breadcrumbs_surfaced(self, status: str) -> None:
        crumbs = "worker reported blocked; cw spawn close --requeue sess-1"
        line = render_event(_page(paused_status=status, breadcrumbs=crumbs), None)
        assert f"reason={crumbs}" in line

    def test_other_type_uses_reason_as_why(self) -> None:
        event = _event(
            OrchestratorEventType.OPERATOR_ESCALATION,
            client=ACME,
            ticket_id="T-77",
            reason="escalated",
        )
        line = render_event(event, None)
        assert "| #T-77 | escalated |" in line
        assert line.endswith("| next: -")

    def test_event_without_a_why_renders_an_empty_field(self) -> None:
        event = _event(
            OrchestratorEventType.SESSION_PHANTOM_REVERTED, client=ACME, ticket_id="T-5"
        )
        assert "| #T-5 |  |" in render_event(event, None)


class TestFirstNextCommand:
    def test_no_row_is_none(self) -> None:
        assert first_next_command(None, None) is None

    def test_running_row_leads_with_wait(self) -> None:
        task = make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")
        session = bound_session_for(task)
        assert first_next_command(task, session) == (
            f"cw dev-queue wait {ACME_TICKET} -c {ACME}"
        )


class TestRenderStuck:
    def test_template(self) -> None:
        block = PendingBlock(
            code="lane_circuit_paused",
            detail=(
                "lane default circuit breaker tripped (spawn_errors);"
                " no auto-resume is scheduled"
            ),
            command="cw lane resume acme default",
        )
        line = render_stuck(
            datetime(2026, 10, 18, 14, 3, 0, 750, tzinfo=UTC),
            ticket_id="ACM-7",
            lane=DEFAULT_LANE,
            block=block,
            age_seconds=15 * 60 + 59,
        )
        assert line == (
            "STUCK | 2026-10-18T14:03:00 | #ACM-7 | lane_circuit_paused | pending 15m"
            " | lane default circuit breaker tripped (spawn_errors); no auto-resume"
            " is scheduled | lane=default | next: cw lane resume acme default"
        )

    def test_no_command_renders_dash(self) -> None:
        block = PendingBlock(code="ssh_key_gate", detail="d", command=None)
        line = render_stuck(
            datetime(2026, 10, 18, tzinfo=UTC),
            ticket_id="T",
            lane="x",
            block=block,
            age_seconds=60.0,
        )
        assert line.endswith("| pending 1m | d | lane=x | next: -")


class TestExitLine:
    def test_refused_reason(self) -> None:
        assert exit_line(refused_reason(42, "attention-acme-1")) == (
            "WATCH | exited: another watcher (pid 42) owns cursor attention-acme-1;"
            " pass --force if it is stale (a recycled pid cannot be detected)"
        )
