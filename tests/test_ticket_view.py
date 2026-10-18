"""``cw.ticket_view``: the per-ticket view builder and renderer (#2691).

Also covers the ``TicketView`` output model (``cw.models.ticket_view``) and the
``cw.orchestrate.client_tick_snapshots`` wrapper the builder reads ticks through.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from freezegun import freeze_time

import cw.ticket_view
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import (
    get_effective_client,
    load_orchestrator_config,
    orchestrator_config_file,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    DEFAULT_LANE,
    DevQueueStore,
    LaneConcurrencyOverride,
    LastResultSource,
    LivenessBucket,
    MustFixOverride,
    OrchestratorConfig,
    OrchestratorEvent,
    OrchestratorEventType,
    PrState,
    QueueItemStatus,
    SessionStatus,
    Stage,
)
from cw.models.ticket_view import TicketView
from cw.orchestrate import _latest_tick_by_client, client_tick_snapshots
from cw.ticket_view import (
    ViewSources,
    build_ticket_view,
    find_ticket_clients,
    render_ticket_view,
    resolve_human_opt_in,
    resolve_view_client,
)
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    make_task,
    patch_roster,
    seed_queue,
    write_lane_client,
    write_lane_override,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import TicketTask


_C = ACME
_T = ACME_TICKET
_NOW = datetime(2026, 10, 15, 18, 0, 0, tzinfo=UTC)
_SURFACE = "a1b2c3d4"
_PR_URL = "https://github.com/o/r/pull/123"
_FINGERPRINT = "3fa2c1d9e0b4" + "0" * 52
_HEAD = "0123456789abcdef0123456789abcdef01234567"

_TEXT_KEYS = [
    "ticket",
    "client",
    "status",
    "stage",
    "disposition",
    "reason",
    "details",
    "lane",
    "priority",
    "attempts",
    "unproductive_attempts",
    "attempt_ceiling",
    "session",
    "attention",
    "last_result",
    "pr",
    "stage_base_ref",
    "skip_reason",
    "approval",
    "other_rows",
    "next",
]

_DOCUMENTED_FIELDS = {
    "schema_version",
    "ticket_id",
    "client",
    "status",
    "stage",
    "disposition",
    "blocked_reason",
    "blocker_details",
    "advisory_note",
    "lane",
    "priority",
    "scope_hint",
    "attempts",
    "unproductive_attempts",
    "attempt_ceiling",
    "session",
    "last_result",
    "pr",
    "stage_base_ref",
    "dispatch",
    "approval",
    "other_rows",
    "created_at",
    "completed_at",
    "next",
}


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch, lanes=(DEFAULT_LANE,))


def _build(ticket: str = _T, client: str = _C) -> TicketView:
    return build_ticket_view(ticket, client, now=_NOW, sources=ViewSources())


def _text(view: TicketView) -> str:
    return render_ticket_view(view, now=_NOW)


def _line(text: str, key: str) -> str:
    """The single ``key: value`` line for *key*."""
    matches = [ln for ln in text.splitlines() if ln.startswith(f"{key}:")]
    assert len(matches) == 1, f"{key}: {matches}"
    return matches[0]


def _tick(at: datetime, **payload: object) -> None:
    with freeze_time(at):
        record_event(OrchestratorEventType.DISPATCH_TICK, {"client": _C, **payload})


class TestFullPopulation:
    def test_blocker_details_are_verbatim_and_untruncated(self) -> None:
        details = "line one\n" + "x" * 600 + "\nline three"
        task = make_task(
            status=QueueItemStatus.BLOCKED_ON_USER,
            stage=Stage.PLAN,
            session_id="sess-1",
            blocked_reason="tool_denied",
            disposition="awaiting_operator",
            stage_base_ref="9fceb02",
        )
        session = bound_session_for(
            task,
            surface_ref=_SURFACE,
            last_result={
                "status": "blocked",
                "blocker": {
                    "stage": "plan",
                    "reason": "tool_denied",
                    "details": details,
                },
            },
            last_result_source=LastResultSource.STOP_HOOK_HARVEST,
            completed_at=_NOW - timedelta(minutes=5),
        )
        seed_queue([task], [session])

        view = _build()
        text = _text(view)

        assert view.blocker_details == details
        assert view.blocked_reason == "tool_denied"
        assert "details: line one" in text.splitlines()
        assert "  " + "x" * 600 in text.splitlines()
        assert "  line three" in text.splitlines()
        assert "…" not in text
        assert "..." not in text
        assert _line(text, "reason") == "reason: tool_denied"
        assert _line(text, "stage_base_ref") == "stage_base_ref: 9fceb02"
        assert _line(text, "last_result") == (
            "last_result: blocked (source=stop_hook_harvest) at 2026-10-15T17:55:00Z"
        )
        assert view.last_result is not None
        assert view.last_result.at == _NOW - timedelta(minutes=5)

    def test_advisory_note_shows_when_no_blocked_reason(self) -> None:
        seed_queue(
            [make_task(status=QueueItemStatus.RUNNING, advisory_note="slow but alive")]
        )
        view = _build()
        assert view.advisory_note == "slow but alive"
        assert _line(_text(view), "reason") == "reason: slow but alive (advisory)"


class TestAttemptCeiling:
    def test_lane_override(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", attempt_ceiling=3)
        seed_queue([make_task(attempts=2, unproductive_attempts=1)])
        view = _build()
        text = _text(view)
        assert (view.attempts, view.unproductive_attempts) == (2, 1)
        assert view.attempt_ceiling == 3
        assert _line(text, "attempts") == "attempts: 2"
        assert _line(text, "unproductive_attempts") == "unproductive_attempts: 1"
        assert _line(text, "attempt_ceiling") == "attempt_ceiling: 3"

    def test_lane_disables_ceiling(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", attempt_ceiling=False)
        seed_queue([make_task()])
        view = _build()
        assert view.attempt_ceiling is None
        assert _line(_text(view), "attempt_ceiling") == "attempt_ceiling: none"

    def test_global_fallback(self) -> None:
        seed_queue([make_task()])
        expected = load_orchestrator_config().global_attempt_ceiling
        assert _build().attempt_ceiling == expected


class TestPr:
    def test_hydrated_pr(self) -> None:
        hydrated = datetime(2026, 10, 15, 17, 30, tzinfo=UTC)
        seed_queue(
            [
                make_task(
                    status=QueueItemStatus.COMPLETED,
                    pr_url=_PR_URL,
                    pr_state=PrState(
                        state="OPEN", mergeable="MERGEABLE", hydrated_at=hydrated
                    ),
                )
            ]
        )
        view = _build()
        assert view.pr is not None
        assert view.pr.number == 123
        assert (view.pr.state, view.pr.mergeable) == ("OPEN", "MERGEABLE")
        assert view.pr.hydrated_at == hydrated
        assert _line(_text(view), "pr") == (
            f"pr: #123 {_PR_URL} state=OPEN mergeable=MERGEABLE"
            " hydrated=2026-10-15T17:30:00Z"
        )

    def test_unhydrated_pr(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.COMPLETED, pr_url=_PR_URL)])
        view = _build()
        assert view.pr is not None
        assert view.pr.hydrated_at is None
        assert view.pr.state is None
        assert _line(_text(view), "pr") == f"pr: #123 {_PR_URL} hydrated: no"

    def test_unparseable_pr_url_has_no_number(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.COMPLETED, pr_url="not-a-pr")])
        view = _build()
        assert view.pr is not None
        assert view.pr.number is None
        assert _line(_text(view), "pr") == "pr: - not-a-pr hydrated: no"

    def test_no_pr(self) -> None:
        seed_queue([make_task()])
        view = _build()
        assert view.pr is None
        assert _line(_text(view), "pr") == "pr: -"

    def test_no_subprocess_launched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([make_task(status=QueueItemStatus.COMPLETED, pr_url=_PR_URL)])

        def _boom(*_a: object, **_kw: object) -> object:
            msg = "the view must not launch a subprocess"
            raise AssertionError(msg)

        monkeypatch.setattr(subprocess, "run", _boom)
        monkeypatch.setattr(subprocess, "Popen", _boom)
        assert _build().pr is not None


class TestApproval:
    def test_plan_approval_renders_unrecorded_approver(self) -> None:
        approved = datetime(2026, 10, 15, 17, 40, tzinfo=UTC)
        seed_queue(
            [
                make_task(
                    plan_approved_at=approved,
                    plan_approved_fingerprint=_FINGERPRINT,
                )
            ]
        )
        view = _build()
        assert view.approval is not None
        assert view.approval.plan_approved_at == approved
        assert view.approval.plan_approved_fingerprint == _FINGERPRINT
        assert _line(_text(view), "approval") == (
            "approval: plan approved 2026-10-15T17:40:00Z; approver: not recorded;"
            " draft=3fa2c1d9e0b4"
        )

    def test_must_fix_override_renders_actor_reason_time(self) -> None:
        recorded = datetime(2026, 10, 15, 17, 45, tzinfo=UTC)
        override = MustFixOverride(
            actor="alice",
            reason="false positive",
            reviewed_sha="a" * 40,
            finding_ids=[("f", "x")],
            recorded_at=recorded,
        )
        seed_queue([make_task(must_fix_override=override)])
        view = _build()
        assert view.approval is not None
        assert view.approval.must_fix_override is not None
        assert view.approval.must_fix_override.actor == "alice"
        assert _line(_text(view), "approval") == (
            "approval: must-fix override by alice at 2026-10-15T17:45:00Z:"
            " false positive"
        )

    def test_scope_drift_grant(self) -> None:
        seed_queue(
            [
                make_task(
                    scope_drift_approved_extra_files=["a.py", "b.py"],
                    scope_drift_approved_head=_HEAD,
                )
            ]
        )
        view = _build()
        assert view.approval is not None
        assert view.approval.scope_drift_approved_extra_files == ["a.py", "b.py"]
        assert view.approval.scope_drift_approved_head == _HEAD
        assert _line(_text(view), "approval") == (
            "approval: scope-drift grant: files=a.py,b.py head=0123456789ab"
        )

    def test_no_approval(self) -> None:
        seed_queue([make_task()])
        view = _build()
        assert view.approval is None
        assert _line(_text(view), "approval") == (
            "approval: - (no row-side approval recorded; tracker-comment approvals"
            " are not mirrored)"
        )


class TestSkipReasonPrecedence:
    def test_row_backoff(self) -> None:
        until = _NOW + timedelta(minutes=5)
        seed_queue([make_task(next_eligible_at=until)])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "spawn_error_backoff"
        assert view.dispatch.source == "row"
        assert view.dispatch.backoff_until == until
        assert _line(_text(view), "skip_reason") == (
            "skip_reason: spawn_error_backoff (source=row, until 2026-10-15T18:05:00Z)"
        )

    def test_row_attempt_cap(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", attempt_ceiling=2)
        seed_queue([make_task(unproductive_attempts=2)])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "attempt_cap_blocked"
        assert view.dispatch.source == "row"
        assert _line(_text(view), "skip_reason") == (
            "skip_reason: attempt_cap_blocked"
            " (source=row, unproductive attempts at ceiling)"
        )

    def test_row_lane_circuit_paused(self) -> None:
        write_lane_override(_C, "default", paused=True, consecutive_spawn_errors=3)
        seed_queue([make_task()])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "lane_circuit_paused"
        assert view.dispatch.source == "row"
        assert view.dispatch.lane_paused is True
        # A breaker pause is already the reason: no extra [lane paused] tag.
        assert _line(_text(view), "skip_reason") == (
            "skip_reason: lane_circuit_paused"
            " (source=row, lane paused by its circuit breaker)"
        )

    def test_config_paused_lane_with_tripped_breaker(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", paused=True)
        write_lane_override(_C, "default", paused=None, consecutive_spawn_errors=3)
        seed_queue([make_task()])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "lane_circuit_paused"

    def test_operator_pause_without_tripped_breaker(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", paused=True)
        seed_queue([make_task()])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason is None
        assert view.dispatch.lane_paused is True
        assert "lane paused" in _line(_text(view), "skip_reason")

    def test_tripped_breaker_on_unpaused_lane(self) -> None:
        write_lane_override(_C, "default", paused=None, consecutive_spawn_errors=5)
        seed_queue([make_task()])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason is None
        assert view.dispatch.lane_paused is False

    def test_row_lane_circuit_paused_carries_a_legacy_lane_pause(self) -> None:
        write_lane_override(_C, "default", paused=True, consecutive_spawn_errors=3)
        seed_queue([make_task()])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.lane_pause is not None
        assert view.dispatch.lane_pause.source == "legacy"

    def test_per_ticket_event_beats_client_snapshot(self) -> None:
        seed_queue([make_task()])
        _tick(
            _NOW - timedelta(seconds=30),
            ticket_id=_T,
            claimed=0,
            skip_reason="worktree_occupied",
        )
        _tick(_NOW - timedelta(seconds=10), skip_reason="cap_full", cap=2)
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "worktree_occupied"
        assert view.dispatch.source == "event"
        assert view.dispatch.as_of == _NOW - timedelta(seconds=30)
        assert view.dispatch.stale is False
        assert _line(_text(view), "skip_reason") == (
            "skip_reason: worktree_occupied (source=event, 30s ago)"
        )

    def test_stale_per_ticket_event(self) -> None:
        seed_queue([make_task()])
        _tick(
            _NOW - timedelta(seconds=600),
            ticket_id=_T,
            claimed=0,
            skip_reason="worktree_occupied",
        )
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.source == "event"
        assert view.dispatch.stale is True
        assert "stale" in _line(_text(view), "skip_reason")

    def test_event_for_same_ticket_in_other_client_is_ignored(self) -> None:
        seed_queue([make_task()])
        with freeze_time(_NOW - timedelta(seconds=30)):
            record_event(
                OrchestratorEventType.DISPATCH_TICK,
                {
                    "client": "other",
                    "ticket_id": _T,
                    "skip_reason": "worktree_occupied",
                },
            )
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason is None
        assert view.dispatch.source == "none"

    def test_client_snapshot(self) -> None:
        seed_queue([make_task()])
        _tick(
            _NOW - timedelta(seconds=42),
            claimed=0,
            pending=1,
            running=1,
            cap=2,
            skip_reason="lane_cap_blocked",
            lanes={
                "default": {
                    "claimed": 0,
                    "pending": 1,
                    "running": 1,
                    "blocked": 0,
                    "signoff": 0,
                }
            },
            lane_occupants={"default": [{"ticket_id": "ACM-7", "status": "running"}]},
        )
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "lane_cap_blocked"
        assert view.dispatch.source == "last_tick"
        assert view.dispatch.stale is False
        assert view.dispatch.lane_stats is not None
        assert view.dispatch.lane_stats["running"] == 1
        assert view.dispatch.lane_occupants == [
            {"ticket_id": "ACM-7", "status": "running"}
        ]
        assert _line(_text(view), "skip_reason") == (
            "skip_reason: lane_cap_blocked (source=last_tick, 42s ago, client-wide;"
            " lane default: pending=1 running=1 blocked=0 signoff=0;"
            " occupants: ACM-7 (running))"
        )

    def test_stale_client_snapshot(self) -> None:
        seed_queue([make_task()])
        _tick(_NOW - timedelta(seconds=600), skip_reason="cap_full", cap=2)
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.source == "last_tick"
        assert view.dispatch.stale is True
        assert view.dispatch.lane_stats is None
        assert "stale" in _line(_text(view), "skip_reason")

    def test_nothing_known(self) -> None:
        seed_queue([make_task()])
        view = _build()
        assert view.dispatch is not None
        assert view.dispatch.skip_reason is None
        assert view.dispatch.source == "none"
        assert _line(_text(view), "skip_reason") == "skip_reason: -"

    @pytest.mark.parametrize(
        "status",
        [s for s in QueueItemStatus if s is not QueueItemStatus.PENDING],
    )
    def test_non_pending_rows_have_no_dispatch(self, status: QueueItemStatus) -> None:
        seed_queue(
            [make_task(status=status, next_eligible_at=_NOW + timedelta(hours=1))]
        )
        view = _build()
        assert view.dispatch is None
        assert _line(_text(view), "skip_reason") == "skip_reason: -"


class TestSessionLiveness:
    def _seed_running(self, **session_overrides: object) -> None:
        task = make_task(status=QueueItemStatus.RUNNING, session_id="sess-1")
        session = bound_session_for(task, surface_ref=_SURFACE, **session_overrides)
        seed_queue([task], [session])

    def test_in_roster(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_roster(monkeypatch, {_SURFACE})
        self._seed_running()
        view = _build()
        assert view.session is not None
        assert view.session.in_roster is True
        assert _line(_text(view), "session") == (
            "session: sess-1 (active, liveness=live, in_roster=yes)"
        )

    def test_missing_from_roster(self) -> None:
        self._seed_running()
        view = _build()
        assert view.session is not None
        assert view.session.in_roster is False
        assert "in_roster=no" in _line(_text(view), "session")

    def test_roster_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_roster(monkeypatch, None)
        self._seed_running()
        view = _build()
        assert view.session is not None
        assert view.session.in_roster is None
        line = _line(_text(view), "session")
        assert "in_roster=unknown" in line
        assert "roster: unreadable" in line

    def test_no_session_bound(self) -> None:
        seed_queue([make_task()])
        view = _build()
        assert view.session is None
        assert _line(_text(view), "session") == "session: -"

    def test_bound_session_pruned(self) -> None:
        seed_queue(
            [make_task(status=QueueItemStatus.BLOCKED_ON_USER, session_id="gone")]
        )
        view = _build()
        assert view.session is not None
        assert view.session.id == "gone"
        assert view.session.status is None
        assert view.last_result is None
        text = _text(view)
        assert _line(text, "session") == "session: gone (session not found)"
        assert _line(text, "details") == "details: - (session not found)"

    def test_attention_state_flows_through(self) -> None:
        self._seed_running(
            status=SessionStatus.ACTIVE,
            liveness_bucket=LivenessBucket.STALE_45M,
            liveness_attention_evidence_key="k",
        )
        view = _build()
        assert view.session is not None
        assert view.session.attention_state == "dead_session_paged"
        assert view.session.liveness_bucket == "stale_45m"
        assert _line(_text(view), "attention") == "attention: dead_session_paged"


class TestDuplicateRows:
    def test_other_rows_and_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        old = make_task(priority=1, created_at=_NOW - timedelta(hours=2))
        new = make_task(priority=7, created_at=_NOW - timedelta(hours=1))
        other_client = make_task(client="other")
        seed_queue([old, new, other_client])
        view = _build()
        assert view.priority == 7
        assert view.other_rows == 1
        assert "Warning" not in capsys.readouterr().err


class TestClientResolution:
    def _store(self) -> DevQueueStore:
        return DevQueueStore(
            tasks=[
                make_task(),
                make_task(ticket_id="ACM-2"),
                make_task(ticket_id="ACM-2", client="beta"),
                make_task(ticket_id="ACM-2", client="beta"),
            ]
        )

    def test_find_ticket_clients(self) -> None:
        assert find_ticket_clients(self._store(), "ACM-2") == [_C, "beta"]
        assert find_ticket_clients(self._store(), "nope") == []

    def test_single_client_auto_selected(self) -> None:
        assert resolve_view_client(self._store(), _T, None) == _C

    def test_override_wins(self) -> None:
        assert resolve_view_client(self._store(), _T, "beta") == "beta"

    def test_unknown_ticket(self) -> None:
        with pytest.raises(CwError, match="No dev-queue task found"):
            resolve_view_client(self._store(), "nope", None)

    def test_ambiguous_ticket(self) -> None:
        with pytest.raises(CwError, match="pass -c") as exc:
            resolve_view_client(self._store(), "ACM-2", None)
        assert _C in str(exc.value)
        assert "beta" in str(exc.value)


class TestClientMissingFromConfig:
    def test_view_still_renders(self) -> None:
        seed_queue([make_task(client="ghost")])
        view = _build(client="ghost")
        assert view.attempt_ceiling == load_orchestrator_config().global_attempt_ceiling
        assert view.dispatch is not None
        assert view.dispatch.lane_paused is None
        assert _line(_text(view), "client") == "client: ghost"


class TestReadOnce:
    def _spy(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        calls: list[int] = []
        real = cw.ticket_view.read_events

        def _spy(**kwargs: object) -> list[OrchestratorEvent]:
            calls.append(1)
            return real(**kwargs)

        monkeypatch.setattr("cw.ticket_view.read_events", _spy)
        return calls

    def test_pending_rows_share_one_event_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._spy(monkeypatch)
        ids = ["ACM-1", "ACM-2", "ACM-3"]
        seed_queue([make_task(ticket_id=t) for t in ids])
        sources = ViewSources()
        for ticket in ids:
            build_ticket_view(ticket, _C, now=_NOW, sources=sources)
        assert len(calls) == 1

    def test_terminal_rows_never_read_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._spy(monkeypatch)
        seed_queue(
            [
                make_task(status=QueueItemStatus.COMPLETED),
                make_task(ticket_id="ACM-2", status=QueueItemStatus.CANCELLED),
            ]
        )
        sources = ViewSources()
        build_ticket_view(_T, _C, now=_NOW, sources=sources)
        build_ticket_view("ACM-2", _C, now=_NOW, sources=sources)
        assert calls == []


def _event(at: datetime, **payload: object) -> OrchestratorEvent:
    return OrchestratorEvent(
        type=OrchestratorEventType.DISPATCH_TICK,
        payload={"client": _C, **payload},
        created_at=at,
    )


class TestClientTickSnapshots:
    def test_per_ticket_events_do_not_overwrite_client_snapshot(self) -> None:
        client_tick = _event(
            _NOW - timedelta(seconds=20), skip_reason="cap_full", cap=2
        )
        per_ticket = _event(
            _NOW - timedelta(seconds=5), ticket_id=_T, skip_reason="worktree_occupied"
        )
        snapshots = client_tick_snapshots([client_tick, per_ticket])
        assert snapshots[_C].skip_reason == "cap_full"
        assert snapshots[_C].tick_at == client_tick.created_at

    def test_matches_latest_tick_without_per_ticket_events(self) -> None:
        events = [
            _event(_NOW - timedelta(seconds=20), skip_reason="cap_full", cap=2),
            _event(_NOW - timedelta(seconds=5), skip_reason="none", cap=2),
        ]
        assert client_tick_snapshots(events) == _latest_tick_by_client(events)


def _views_for_every_status() -> list[TicketView]:
    tasks = [
        make_task(ticket_id=f"ACM-{index}", status=status)
        for index, status in enumerate(QueueItemStatus)
    ]
    seed_queue(tasks)
    sources = ViewSources()
    return [
        build_ticket_view(t.ticket_id, _C, now=_NOW, sources=sources) for t in tasks
    ]


class TestRenderer:
    def test_key_set_and_order_identical_for_every_status(self) -> None:
        for view in _views_for_every_status():
            text = _text(view)
            keys = [
                ln.split(":", 1)[0]
                for ln in text.splitlines()
                if not ln.startswith(" ")
            ]
            assert keys == _TEXT_KEYS, view.status
            assert "|" not in text

    def test_next_lines_are_indented_cw_commands(self) -> None:
        for view in _views_for_every_status():
            lines = _text(view).splitlines()
            after = lines[lines.index("next:") + 1 :]
            assert after
            assert all(ln.startswith("  cw ") for ln in after)
            assert len(after) == len(view.next)

    def test_first_lines(self) -> None:
        seed_queue([make_task(lane="default", priority=4)])
        text = _text(_build())
        assert text.splitlines()[:4] == [
            f"ticket: {_T}",
            f"client: {_C}",
            "status: pending",
            "stage: plan",
        ]
        assert _line(text, "priority") == "priority: 4"
        assert _line(text, "other_rows") == "other_rows: 0"
        assert _line(text, "disposition") == "disposition: -"


class TestJsonContract:
    def test_round_trip(self) -> None:
        for view in _views_for_every_status():
            assert TicketView.model_validate_json(view.model_dump_json()) == view
            assert view.schema_version == 1

    def test_field_set_matches_documented_contract(self) -> None:
        assert set(TicketView.model_fields) == _DOCUMENTED_FIELDS

    def test_extra_fields_are_forbidden(self) -> None:
        seed_queue([make_task()])
        payload = _build().model_dump(mode="json")
        payload["surprise"] = 1
        with pytest.raises(ValueError, match="surprise"):
            TicketView.model_validate(payload)


_OVERRIDE_CMD = (
    'cw dev-queue approve ACM-1 -c acme --override-must-fix --reason "<why>"'
)
_SHIP_CMD = "cw dev-queue requeue ACM-1 -c acme --stage finalize"
_SHIP_CAVEAT = (
    "if the ticket or lane has a finalize hold, operator signoff or"
    " scope_hint: large, this step is the human's."
)


def _must_fix_row(*, overridden: bool) -> TicketTask:
    return make_task(
        status=QueueItemStatus.BLOCKED_ON_USER,
        stage=Stage.REVIEW,
        session_id="sess-1",
        blocked_reason=CODEX_MUST_FIX_FINDINGS,
        must_fix_override=(
            MustFixOverride(
                actor="alice",
                reason="fp",
                reviewed_sha="a" * 40,
                finding_ids=[],
                recorded_at=_NOW,
            )
            if overridden
            else None
        ),
    )


def _write_orchestrator_config(text: str) -> None:
    path = orchestrator_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestResolveHumanOptIn:
    """R5 (a''): the config-resolved human opt-in ``show``/``tasks`` pass in."""

    @pytest.mark.parametrize(
        ("task_overrides", "lane_kwargs", "config", "expected"),
        [
            ({"hold_finalize": "manual"}, {}, OrchestratorConfig(), True),
            ({"signoff": "operator"}, {}, OrchestratorConfig(), True),
            ({}, {"finalize_gate": "manual"}, OrchestratorConfig(), True),
            ({}, {"signoff": "operator"}, OrchestratorConfig(), True),
            ({}, {}, OrchestratorConfig(default_finalize_gate="manual"), True),
            ({}, {}, OrchestratorConfig(default_signoff="operator"), True),
            ({}, {}, OrchestratorConfig(), False),
        ],
        ids=[
            "ticket_hold",
            "ticket_signoff",
            "lane_finalize_gate",
            "lane_signoff",
            "global_finalize_gate",
            "global_signoff",
            "nothing_armed",
        ],
    )
    def test_resolution(
        self,
        tmp_path: Path,
        task_overrides: dict[str, object],
        lane_kwargs: dict[str, str],
        config: OrchestratorConfig,
        expected: bool,
    ) -> None:
        write_lane_client(_C, tmp_path / "ws", **lane_kwargs)
        task = make_task(**task_overrides)
        client_cfg = get_effective_client(_C)
        assert resolve_human_opt_in(task, client_cfg, config) is expected

    def test_unconfigured_client_falls_through_to_the_global_default(self) -> None:
        task = make_task()
        armed = OrchestratorConfig(default_finalize_gate="manual")
        assert resolve_human_opt_in(task, None, armed) is True
        assert resolve_human_opt_in(task, None, OrchestratorConfig()) is False

    def test_another_lanes_gate_does_not_apply(self, tmp_path: Path) -> None:
        write_clients_yaml(
            ClientSpec(
                _C,
                tmp_path / "ws",
                lanes=[
                    {"name": DEFAULT_LANE},
                    {"name": "fast", "finalize_gate": "manual"},
                ],
            ),
            ensure_workspaces=True,
        )
        task = make_task(lane=DEFAULT_LANE)
        client_cfg = get_effective_client(_C)
        assert resolve_human_opt_in(task, client_cfg, OrchestratorConfig()) is False


class TestNextCommandsOptIn:
    @pytest.mark.parametrize("overridden", [False, True], ids=["rule6", "rule6a"])
    @pytest.mark.parametrize(
        "arm",
        ["lane_finalize_gate", "lane_signoff", "global_finalize_gate"],
    )
    def test_armed_opt_in_drops_the_finalize_jump(
        self, tmp_path: Path, overridden: bool, arm: str
    ) -> None:
        if arm == "lane_finalize_gate":
            write_lane_client(_C, tmp_path / "ws", finalize_gate="manual")
        elif arm == "lane_signoff":
            write_lane_client(_C, tmp_path / "ws", signoff="operator")
        else:
            _write_orchestrator_config("default_finalize_gate: manual\n")
        seed_queue([_must_fix_row(overridden=overridden)])
        view = _build()
        assert [c.command for c in view.next] == [_OVERRIDE_CMD]
        assert not [c for c in view.next if "--stage finalize" in c.command]
        assert _SHIP_CMD not in _text(view)

    @pytest.mark.parametrize("overridden", [False, True], ids=["rule6", "rule6a"])
    def test_nothing_armed_keeps_the_ship_step_with_its_caveat(
        self, overridden: bool
    ) -> None:
        seed_queue([_must_fix_row(overridden=overridden)])
        view = _build()
        ship = [c for c in view.next if c.command == _SHIP_CMD]
        assert len(ship) == 1
        assert ship[0].purpose.endswith(_SHIP_CAVEAT)
        assert f"  {_SHIP_CMD}   # {ship[0].purpose}" in _text(view)


# --- #2696: the lane pause cause and resume hint ------------------------------

_PAUSED_AT = _NOW - timedelta(minutes=5)


def _breaker_override(**updates: object) -> LaneConcurrencyOverride:
    base = LaneConcurrencyOverride(
        paused=True,
        paused_at=_PAUSED_AT,
        pause_source="circuit_breaker",
        pause_trigger="spawn_errors",
        consecutive_spawn_errors=3,
        recent_spawn_errors=["boom"],
        circuit_trip_streak=1,
        circuit_auto_resume_at=_PAUSED_AT + timedelta(minutes=15),
    )
    return base.model_copy(update=updates)


def _skip_reason_block(view: TicketView) -> list[str]:
    lines = _text(view).splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("skip_reason:"))
    end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith(" "))
    return lines[start:end]


class TestLanePause:
    @pytest.mark.parametrize(
        ("trigger", "wording"),
        [
            ("spawn_errors", "spawn errors"),
            ("worktree_errors", "worktree setup errors"),
        ],
    )
    def test_circuit_breaker_pause_lines_follow_the_skip_reason(
        self, trigger: str, wording: str
    ) -> None:
        write_lane_override(
            _C, "default", override=_breaker_override(pause_trigger=trigger)
        )
        seed_queue([make_task()])

        view = _build()

        assert view.dispatch is not None
        assert view.dispatch.lane_pause is not None
        assert view.dispatch.lane_pause.source == "circuit_breaker"
        assert view.dispatch.lane_pause.trigger == trigger
        assert _skip_reason_block(view) == [
            "skip_reason: lane_circuit_paused"
            " (source=row, lane paused by its circuit breaker)",
            "  paused since 2026-10-15T17:55:00Z (5m ago) by circuit_breaker"
            f" ({wording}, trip #1)",
            "    last spawn errors (oldest first):",
            "      - boom",
            "    auto-resume: 2026-10-15T18:10:00Z (in 10m)",
            "    resume: cw lane resume acme default",
        ]

    def test_operator_pause_without_tick_data(self) -> None:
        write_lane_override(
            _C,
            "default",
            override=LaneConcurrencyOverride(
                paused=True, paused_at=_PAUSED_AT, pause_source="operator"
            ),
        )
        seed_queue([make_task()])

        view = _build()

        assert _skip_reason_block(view) == [
            "skip_reason: - [lane paused]",
            "  paused since 2026-10-15T17:55:00Z (5m ago) by operator",
            "    auto-resume: no (operator-held)",
            "    resume: cw lane resume acme default",
        ]

    def test_declared_pause(self, tmp_path: Path) -> None:
        write_lane_client(_C, tmp_path / "ws", paused=True)
        seed_queue([make_task()])

        view = _build()

        assert view.dispatch is not None
        assert view.dispatch.lane_pause is not None
        assert view.dispatch.lane_pause.source == "declared"
        assert _skip_reason_block(view)[1] == "  paused: declared in clients.yaml"

    def test_unpaused_lane_has_no_lane_pause(self) -> None:
        seed_queue([make_task()])

        view = _build()

        assert view.dispatch is not None
        assert view.dispatch.lane_pause is None
        assert _skip_reason_block(view) == ["skip_reason: -"]

    def test_non_pending_row_has_no_dispatch(self) -> None:
        write_lane_override(_C, "default", override=_breaker_override())
        seed_queue([make_task(status=QueueItemStatus.RUNNING)])

        assert _build().dispatch is None

    def test_worktree_tripped_lane_reports_lane_circuit_paused(self) -> None:
        write_lane_override(
            _C,
            "default",
            override=LaneConcurrencyOverride(
                paused=True, consecutive_worktree_errors=9
            ),
        )
        seed_queue([make_task()])

        view = _build()

        assert view.dispatch is not None
        assert view.dispatch.skip_reason == "lane_circuit_paused"

    def test_disabled_auto_resume_is_reported(self) -> None:
        _write_orchestrator_config("lane_circuit_auto_resume_minutes: 0\n")
        write_lane_override(_C, "default", override=_breaker_override())
        seed_queue([make_task()])

        view = _build()

        assert view.dispatch is not None
        assert view.dispatch.lane_pause is not None
        assert view.dispatch.lane_pause.auto_resume_enabled is False
        assert (
            "    auto-resume: off (lane_circuit_auto_resume_minutes=0)"
            in _skip_reason_block(view)
        )
