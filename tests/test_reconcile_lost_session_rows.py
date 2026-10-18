"""Tests for cw.reconcile.lost_session_rows (#2715, Sweep L).

A RUNNING row bound to a session with no record (the #2502 failed
``sessions.json`` write) is parked BLOCKED_ON_USER as
``running_row_session_lost``, keeping its ``session_id``; a RUNNING row with
no session at all is paged once past the spawn grace and never parked. A
wiped ``sessions.json`` parks nothing, and at most three rows park per tick.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

from cw.cli.queues import _dedup_terminal
from cw.config import load_state, save_state, state_file
from cw.dev_queue import load_dev_queue, requeue_ticket, save_dev_queue
from cw.events import read_events
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    SessionStatus,
    TicketTask,
)
from cw.reconcile import lost_session_rows, reconcile
from cw.reconcile.lost_session_rows import (
    NO_SESSION_ADVISORY_NOTE,
    SESSION_LOST_PARKS_MAX_PER_TICK,
    SESSIONLESS_PAGE_REASON,
    find_lost_session_rows,
    run_lost_session_recovery,
)
from tests._clients_yaml import write_clients_yaml
from tests._reconcile_helpers import (
    LockProbeDaemon,
    _failing_record_event,
    assert_no_parked_row_is_sessionless,
    use_reconcile_daemon,
)
from tests.conftest import _CANONICAL_ATTENTION_KEYS, _make_daemon_session

_CLIENT = "client-a"
_LOST = "running_row_session_lost"
_GHOST = "ghost-sess"
_PR_URL = "https://github.com/foo/bar/pull/7"


@pytest.fixture
def clients(tmp_path: Path) -> dict[str, ClientConfig]:
    return {
        _CLIENT: ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
        )
    }


def _running(
    ticket_id: str,
    *,
    session_id: str | None = _GHOST,
    claimed_at: datetime | None = None,
    attempts: int = 1,
    client: str = _CLIENT,
    **extra: object,
) -> TicketTask:
    return TicketTask.model_validate(
        {
            "ticket_id": ticket_id,
            "client": client,
            "status": QueueItemStatus.RUNNING,
            "session_id": session_id,
            "claimed_at": claimed_at,
            "attempts": attempts,
            **extra,
        }
    )


def _real_session(sid: str = "real-1", ticket: str = "OTHER") -> CwState:
    """A state holding one recorded (finished, surface-less) session.

    Terminal and surface-less so an empty roster is not outage-shaped.
    """
    return CwState(
        sessions=[
            _make_daemon_session(
                id=sid,
                name=f"{_CLIENT}/auto-dev/{ticket}",
                client=_CLIENT,
                status=SessionStatus.COMPLETED,
                surface_ref=None,
            )
        ]
    )


def _run(clients: dict[str, ClientConfig], *, now: datetime | None = None) -> list[str]:
    return run_lost_session_recovery(
        now=now or datetime.now(UTC), clients=clients, native_live=set()
    )


def _pages(paused_status: str) -> list[dict[str, object]]:
    return [
        e.payload
        for e in read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        if e.payload.get("paused_status") == paused_status
    ]


def _by_ticket() -> dict[str, TicketTask]:
    return {t.ticket_id: t for t in load_dev_queue().tasks}


class TestRecordMissingPark:
    def test_parks_and_keeps_the_evidence(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running("T-1", pr_url=_PR_URL)]))

        assert _run(clients) == ["T-1"]

        row = _by_ticket()["T-1"]
        assert row.status is QueueItemStatus.BLOCKED_ON_USER
        assert row.disposition == _LOST
        assert row.session_id == _GHOST
        assert row.pr_url == _PR_URL
        assert row.blocked_reason is not None
        assert _GHOST in row.blocked_reason
        assert row.unproductive_attempts == 0
        assert_no_parked_row_is_sessionless(load_dev_queue())

    def test_exact_events_in_order(self, clients: dict[str, ClientConfig]) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))

        _run(clients)

        events = read_events()
        assert [str(e.type) for e in events] == [
            str(OrchestratorEventType.TASK_TRANSITION),
            str(OrchestratorEventType.SESSION_NEEDS_ATTENTION),
        ]
        transition, page = events[0].payload, events[1].payload
        assert transition["old_status"] == QueueItemStatus.RUNNING
        assert transition["new_status"] == QueueItemStatus.BLOCKED_ON_USER
        assert transition["unproductive_charge"] is False
        assert transition["session_id"] == _GHOST
        assert set(page) == _CANONICAL_ATTENTION_KEYS
        assert page["session_id"] == _GHOST
        assert page["session_name"] == ""
        assert page["claude_session_id"] is None
        assert page["paused_status"] == _LOST
        assert page["crashed"] is False
        assert page["lane"] == "default"
        breadcrumbs = str(page["breadcrumbs"])
        assert "nothing was deleted" in breadcrumbs
        assert f"cw dev-queue requeue T-1 -c {_CLIENT}" in breadcrumbs
        assert "45 minutes" in breadcrumbs
        assert events[1].correlation_id == "T-1"

    def test_page_follows_the_queue_save(
        self, clients: dict[str, ClientConfig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))
        order: list[str] = []
        real_save = lost_session_rows.save_dev_queue
        real_record = lost_session_rows.record_event

        def _save(store: DevQueueStore) -> None:
            order.append("save")
            real_save(store)

        def _record(
            etype: OrchestratorEventType,
            payload: dict[str, object] | None = None,
            *,
            correlation_id: str | None = None,
        ) -> object:
            order.append("page")
            return real_record(etype, payload, correlation_id=correlation_id)

        monkeypatch.setattr(lost_session_rows, "save_dev_queue", _save)
        monkeypatch.setattr(lost_session_rows, "record_event", _record)

        _run(clients)

        assert order == ["save", "page"]

    def test_requeue_releases_without_a_charge(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        write_clients_yaml(clients[_CLIENT])
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))
        _run(clients)

        requeue_ticket("T-1", _CLIENT)

        row = _by_ticket()["T-1"]
        assert row.status is QueueItemStatus.PENDING
        assert row.unproductive_attempts == 0

    def test_second_tick_does_nothing(self, clients: dict[str, ClientConfig]) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))
        _run(clients)
        events_before = len(read_events())
        queue_before = load_dev_queue().model_dump()

        assert _run(clients) == []
        assert len(read_events()) == events_before
        assert load_dev_queue().model_dump() == queue_before

    @pytest.mark.parametrize(
        "row",
        [
            _running("T-1", fix_dispatch_session_id="fix-1"),
            _running("T-1", client="elsewhere"),
            _running("T-1", status=QueueItemStatus.PENDING),
        ],
        ids=["backstop-exempt", "client-out-of-scope", "not-running"],
    )
    def test_out_of_scope_rows_are_untouched(
        self, clients: dict[str, ClientConfig], row: TicketTask
    ) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[row]))
        before = load_dev_queue().model_dump()

        assert _run(clients) == []
        assert load_dev_queue().model_dump() == before

    def test_bound_row_whose_session_resolves_is_left_alone(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        save_state(_real_session(sid=_GHOST, ticket="T-1"))
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))

        assert _run(clients) == []
        assert _by_ticket()["T-1"].status is QueueItemStatus.RUNNING

    def test_row_restamped_between_detect_and_act_is_not_parked(
        self, clients: dict[str, ClientConfig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))
        real_find = lost_session_rows.find_lost_session_rows

        def _find_then_restamp(
            state: CwState,
            tasks: list[TicketTask],
            scope: dict[str, ClientConfig],
            *,
            now: datetime,
        ) -> lost_session_rows.LostSessionRows:
            found = real_find(state, tasks, scope, now=now)
            store = load_dev_queue()
            store.tasks[0].session_id = "real-1"
            save_dev_queue(store)
            return found

        monkeypatch.setattr(
            lost_session_rows, "find_lost_session_rows", _find_then_restamp
        )

        assert _run(clients) == []
        row = _by_ticket()["T-1"]
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == "real-1"
        assert _pages(_LOST) == []

    def test_no_clients_is_a_no_op(self) -> None:
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))

        assert _run({}) == []
        assert _by_ticket()["T-1"].status is QueueItemStatus.RUNNING

    def test_failed_page_is_logged_not_raised(
        self, clients: dict[str, ClientConfig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running("T-1")]))
        failures = _failing_record_event(
            monkeypatch,
            target="cw.reconcile.lost_session_rows.record_event",
            event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            fail_for=lambda _payload: True,
        )

        assert _run(clients) == ["T-1"]
        assert failures == [1]
        assert _by_ticket()["T-1"].status is QueueItemStatus.BLOCKED_ON_USER


class TestMassParkGuards:
    def test_missing_sessions_file_with_four_bound_rows_parks_nothing(
        self,
        clients: dict[str, ClientConfig],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        assert not state_file().exists()
        rows = [_running(f"T-{i}") for i in range(4)]
        rows.append(_running("NOSESS", session_id=None))
        save_dev_queue(DevQueueStore(tasks=rows))

        with caplog.at_level(logging.WARNING, logger="cw.reconcile.lost_session_rows"):
            assert _run(clients) == []

        warnings = [r for r in caplog.records if "wiped-state guard" in r.message]
        assert len(warnings) == 1
        statuses = {t.ticket_id: t.status for t in load_dev_queue().tasks}
        assert set(statuses.values()) == {QueueItemStatus.RUNNING}
        assert [p["ticket_id"] for p in _pages(SESSIONLESS_PAGE_REASON)] == ["NOSESS"]

    def test_empty_sessions_list_with_four_bound_rows_parks_nothing(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text('{"sessions": []}', encoding="utf-8")
        save_dev_queue(DevQueueStore(tasks=[_running(f"T-{i}") for i in range(4)]))

        assert _run(clients) == []

    def test_three_bound_rows_with_an_empty_state_all_park(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        """The #2502 shape: a failed first write leaves an empty state."""
        save_state(CwState())
        save_dev_queue(DevQueueStore(tasks=[_running(f"T-{i}") for i in range(3)]))

        assert sorted(_run(clients)) == ["T-0", "T-1", "T-2"]

    def test_exempt_rows_do_not_count_toward_the_wipe(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        save_state(CwState())
        rows = [
            _running("T-0"),
            _running("T-1"),
            _running("T-2", fix_dispatch_session_id="fix-2"),
            _running("T-3", fix_dispatch_session_id="fix-3"),
        ]
        save_dev_queue(DevQueueStore(tasks=rows))

        assert _run(clients) == ["T-0", "T-1"]

    def test_cap_parks_three_per_tick_in_queue_order(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        save_state(_real_session())
        save_dev_queue(DevQueueStore(tasks=[_running(f"T-{i}") for i in range(5)]))

        first = _run(clients)
        untouched = [
            t.model_dump()
            for t in load_dev_queue().tasks
            if t.ticket_id in {"T-3", "T-4"}
        ]
        second = _run(clients)
        third = _run(clients)

        assert first == ["T-0", "T-1", "T-2"]
        assert len(first) == SESSION_LOST_PARKS_MAX_PER_TICK
        assert all(row["status"] == QueueItemStatus.RUNNING for row in untouched)
        assert second == ["T-3", "T-4"]
        assert third == []

    def test_find_reports_wiped(self, clients: dict[str, ClientConfig]) -> None:
        found = find_lost_session_rows(
            CwState(),
            [_running(f"T-{i}") for i in range(4)],
            clients,
            now=datetime.now(UTC),
        )

        assert found.wiped is True
        assert len(found.parks) == 4


class TestSessionlessPage:
    @pytest.mark.parametrize(
        ("claimed_ago", "paged"),
        [
            (timedelta(seconds=29), False),
            (timedelta(seconds=30), True),
            (None, True),
        ],
        ids=["inside-grace", "exactly-30s", "unknown-claim-age"],
    )
    def test_spawn_grace(
        self,
        clients: dict[str, ClientConfig],
        claimed_ago: timedelta | None,
        paged: bool,
    ) -> None:
        now = datetime.now(UTC)
        claimed_at = None if claimed_ago is None else now - claimed_ago
        save_state(_real_session())
        save_dev_queue(
            DevQueueStore(
                tasks=[_running("T-1", session_id=None, claimed_at=claimed_at)]
            )
        )

        _run(clients, now=now)

        assert bool(_pages(SESSIONLESS_PAGE_REASON)) is paged

    def test_naive_claimed_at_fails_closed(
        self, clients: dict[str, ClientConfig], caplog: pytest.LogCaptureFixture
    ) -> None:
        naive = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
        save_dev_queue(
            DevQueueStore(tasks=[_running("T-1", session_id=None, claimed_at=naive)])
        )

        with caplog.at_level(logging.DEBUG, logger="cw.reconcile.lost_session_rows"):
            _run(clients)

        assert _pages(SESSIONLESS_PAGE_REASON) == []
        assert "naive claimed_at" in caplog.text

    def test_payload_latch_and_no_repeat(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        save_dev_queue(
            DevQueueStore(tasks=[_running("T-1", session_id=None, attempts=2)])
        )

        _run(clients)
        _run(clients)

        pages = _pages(SESSIONLESS_PAGE_REASON)
        assert len(pages) == 1
        page = pages[0]
        assert set(page) == _CANONICAL_ATTENTION_KEYS
        assert page["session_id"] == f"no-session:{_CLIENT}/T-1#2"
        assert page["session_name"] == ""
        assert page["claude_session_id"] is None
        assert page["crashed"] is False
        assert "Nothing was changed" in str(page["breadcrumbs"])
        assert "cw doctor --reap" in str(page["breadcrumbs"])
        row = _by_ticket()["T-1"]
        assert row.advisory_note == NO_SESSION_ADVISORY_NOTE
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id is None

    def test_live_same_ticket_session_is_named_and_reap_omitted(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        live = _make_daemon_session(
            id="live-1", name=f"{_CLIENT}/auto-dev/T-1", surface_ref="abcd1234"
        )
        save_state(CwState(sessions=[live]))
        save_dev_queue(DevQueueStore(tasks=[_running("T-1", session_id=None)]))

        run_lost_session_recovery(
            now=datetime.now(UTC), clients=clients, native_live={"abcd1234"}
        )

        breadcrumbs = str(_pages(SESSIONLESS_PAGE_REASON)[0]["breadcrumbs"])
        assert "live-1" in breadcrumbs
        assert "cw doctor --reap" not in breadcrumbs

    @pytest.mark.parametrize("later", [timedelta(hours=1), timedelta(days=3)])
    def test_never_parked(
        self, clients: dict[str, ClientConfig], later: timedelta
    ) -> None:
        now = datetime.now(UTC)
        save_dev_queue(
            DevQueueStore(tasks=[_running("T-1", session_id=None, claimed_at=now)])
        )

        with freeze_time(now + later):
            _run(clients, now=now + later)
            _run(clients, now=now + later)

        row = _by_ticket()["T-1"]
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id is None

    def test_failed_page_is_not_latched_and_retries(
        self, clients: dict[str, ClientConfig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_running("T-1", session_id=None)]))
        with monkeypatch.context() as patched:
            failures = _failing_record_event(
                patched,
                target="cw.reconcile.lost_session_rows.record_event",
                event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                fail_for=lambda _payload: True,
            )
            _run(clients)
        assert failures == [1]
        assert _by_ticket()["T-1"].advisory_note is None

        _run(clients)

        assert len(_pages(SESSIONLESS_PAGE_REASON)) == 1
        assert _by_ticket()["T-1"].advisory_note == NO_SESSION_ADVISORY_NOTE

    def test_row_changed_after_the_page_is_not_latched(
        self, clients: dict[str, ClientConfig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_running("T-1", session_id=None)]))
        real_record = lost_session_rows.record_event

        def _record_then_bind(
            etype: OrchestratorEventType,
            payload: dict[str, object] | None = None,
            *,
            correlation_id: str | None = None,
        ) -> None:
            real_record(etype, payload, correlation_id=correlation_id)
            store = load_dev_queue()
            store.tasks[0].session_id = "bound-now"
            save_dev_queue(store)

        monkeypatch.setattr(lost_session_rows, "record_event", _record_then_bind)

        _run(clients)

        assert _by_ticket()["T-1"].advisory_note is None

    def test_fresh_claim_pages_again_with_a_distinct_id(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        save_dev_queue(DevQueueStore(tasks=[_running("T-1", session_id=None)]))
        _run(clients)
        store = load_dev_queue()
        row = store.tasks[0]
        row.attempts += 1
        row.claimed_at = None
        row.advisory_note = None
        save_dev_queue(store)

        _run(clients)

        ids = [p["session_id"] for p in _pages(SESSIONLESS_PAGE_REASON)]
        assert ids == [f"no-session:{_CLIENT}/T-1#1", f"no-session:{_CLIENT}/T-1#2"]

    def test_two_sessionless_tickets_survive_terminal_dedup(
        self, clients: dict[str, ClientConfig]
    ) -> None:
        save_dev_queue(
            DevQueueStore(
                tasks=[
                    _running("T-1", session_id=None),
                    _running("T-2", session_id=None),
                ]
            )
        )

        _run(clients)

        events = read_events(
            event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION]
        )
        assert len(_dedup_terminal(events)) == 2


# ---------------------------------------------------------------------------
# Through reconcile()
# ---------------------------------------------------------------------------


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: CwState,
    tasks: list[TicketTask],
    roster: list[str] | None = None,
) -> LockProbeDaemon:
    write_clients_yaml(
        ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
        )
    )
    save_state(state)
    save_dev_queue(DevQueueStore(tasks=tasks))
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        lambda: [{"sessionId": f"{ref}-0000-4000"} for ref in roster or []],
    )
    monkeypatch.setattr(
        "cw.reconcile._deps.pr_is_merged_for_ticket",
        lambda _tid, **_kw: (False, True),
    )
    daemon = LockProbeDaemon()
    use_reconcile_daemon(monkeypatch, daemon)
    return daemon


class TestThroughReconcile:
    def test_park_escalates_after_45_minutes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _world(tmp_path, monkeypatch, state=_real_session(), tasks=[_running("T-1")])

        with freeze_time(now):
            reconcile()
        row = _by_ticket()["T-1"]
        assert row.status is QueueItemStatus.BLOCKED_ON_USER
        assert row.escalation_parked_at is not None

        with freeze_time(now + timedelta(minutes=46)):
            reconcile()

        escalations = read_events(
            event_types=[OrchestratorEventType.OPERATOR_ESCALATION]
        )
        assert [e.payload.get("ticket_id") for e in escalations] == ["T-1"]
        assert _by_ticket()["T-1"].status is QueueItemStatus.BLOCKED_ON_USER

    def test_live_unrecorded_worker_is_stopped_the_same_tick(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = _world(
            tmp_path, monkeypatch, state=_real_session(), tasks=[_running("T-1")]
        )
        unrecorded = daemon.seed_live_worker(tmp_path / "worktrees" / "dev-T-1")

        reconcile()

        assert daemon.stop_calls == [unrecorded]
        assert [lock for lock, _statuses in daemon.probes] == ["free"]
        assert _by_ticket()["T-1"].disposition == _LOST
        assert_no_parked_row_is_sessionless(load_dev_queue())

    def test_daemon_outage_early_return_runs_neither_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = _make_daemon_session(
            id="live-1", name=f"{_CLIENT}/auto-dev/OTHER", surface_ref="abcd1234"
        )
        _world(
            tmp_path,
            monkeypatch,
            state=CwState(sessions=[live]),
            tasks=[_running("T-1"), _running("T-2", session_id=None)],
            roster=[],
        )

        reconcile()

        assert {t.status for t in load_dev_queue().tasks} == {QueueItemStatus.RUNNING}
        assert _pages(SESSIONLESS_PAGE_REASON) == []

    def test_acceptance_matrix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        terminal_recent = _make_daemon_session(
            id="term-1",
            name=f"{_CLIENT}/auto-dev/T-TERM",
            status=SessionStatus.COMPLETED,
            surface_ref=None,
            completed_at=now,
        )
        surfaceless = _make_daemon_session(
            id="nosurf-1", name=f"{_CLIENT}/auto-dev/T-NOSURF", surface_ref=None
        )
        _world(
            tmp_path,
            monkeypatch,
            state=CwState(sessions=[terminal_recent, surfaceless]),
            tasks=[
                _running("T-LOST"),
                _running("T-TERM", session_id="term-1"),
                _running("T-NOSURF", session_id="nosurf-1"),
                _running("T-GRACE", session_id=None, claimed_at=now),
                _running("T-NOSESS", session_id=None),
            ],
        )

        with freeze_time(now):
            reconcile()

        rows = _by_ticket()
        assert rows["T-LOST"].status is QueueItemStatus.BLOCKED_ON_USER
        # Exempt rows are covered directly above: reconcile's post-lock
        # fix-dispatch pass owns a fix-held row.
        for ticket in ("T-TERM", "T-NOSURF", "T-GRACE", "T-NOSESS"):
            assert rows[ticket].status is QueueItemStatus.RUNNING, ticket
        assert [p["ticket_id"] for p in _pages(SESSIONLESS_PAGE_REASON)] == ["T-NOSESS"]
        assert load_state().sessions[0].status is SessionStatus.COMPLETED

    def test_bound_session_clears_the_latch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Self-heal: once bound, the mismatch stamper re-derives the note."""
        live = _make_daemon_session(
            id="live-1", name=f"{_CLIENT}/auto-dev/T-1", surface_ref="abcd1234"
        )
        _world(
            tmp_path,
            monkeypatch,
            state=CwState(sessions=[live]),
            tasks=[_running("T-1", session_id=None)],
            roster=["abcd1234"],
        )
        reconcile()
        assert _by_ticket()["T-1"].advisory_note == NO_SESSION_ADVISORY_NOTE
        store = load_dev_queue()
        store.tasks[0].session_id = "live-1"
        save_dev_queue(store)

        reconcile()

        assert _by_ticket()["T-1"].advisory_note is None
