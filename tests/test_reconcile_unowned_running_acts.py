"""Unit tests for cw.reconcile.unowned_running_acts and _text (#3328).

Part B of #2615: an unbound RUNNING row that cannot be adopted is either
parked or reverted (NO_OWNER: the claiming process is gone, the roster is
readable and homes no worker, and the claim context ties nothing), or paged
once as UNCONFIRMABLE and latched in ``advisory_note`` without any status
change. The text builders are tested here too (``TestTexts``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    ClientConfig,
    DevQueueStore,
    LaneConfig,
    LaunchMarker,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import unowned_running_acts as acts_module
from cw.reconcile._shared import _UNOWNED_UNCONFIRMED_NOTE_PREFIX
from cw.reconcile.unowned_running_acts import (
    UNOWNED_ACTS_MAX_PER_TICK,
    classify_claim_context,
    running_siblings,
)
from cw.reconcile.unowned_running_text import (
    ALL_CAUSES,
    CAUSE_NON_NATIVE,
    CAUSE_ROSTER_UNPROVEN,
    CAUSE_UNREADABLE_CONTEXT,
    CONTEXT_ATTEMPT_MISMATCH,
    CONTEXT_MISSING,
    CONTEXT_NO_ATTEMPT,
    CONTEXT_OTHER_CLAIM,
    CONTEXT_SESSION_ABSENT,
    NO_OWNER_PAGE_REASON,
    UNCONFIRMED_PAGE_REASON,
    backend_names_text,
    claim_age_seconds,
    context_clause,
    no_owner_blocked_reason,
    no_owner_breadcrumbs,
    unconfirmed_breadcrumbs,
    unconfirmed_evidence,
    unconfirmed_note,
    unconfirmed_note_head,
)
from tests._reconcile_helpers import (
    _CLAIM,
    _CLIENT,
    _DEAD_PID,
    _TICKET,
    EVENTS_DOC,
    RosterReadProbeDaemon,
    _attention_events,
    _claim_context,
    _edit_queue,
    _failing_record_event,
    _row,
    _seed,
    _session,
    _sweep,
    _worktree,
    _wrap_detect,
    dead_launch_marker,
    events_doc_bullet,
    make_client_cfg,
    mk_unowned_running_row,
    use_reconcile_daemon,
    write_unreadable_claim_context,
)
from tests._shared.daemon_fakes import CapturedEvent

_ACTS = "cw.reconcile.unowned_running_acts"
_OLD = _CLAIM + timedelta(minutes=5)
_PR = "https://github.com/acme/repo/pull/7"
_AUTO = OrchestratorConfig(reap_policy=ReapPolicy.AUTO)


@pytest.fixture
def client_cfg(tmp_path: Path) -> ClientConfig:
    return make_client_cfg(tmp_path)


@pytest.fixture(autouse=True)
def daemon(monkeypatch: pytest.MonkeyPatch) -> RosterReadProbeDaemon:
    """The sweep's daemon: an in-memory roster that records each read."""
    fake = RosterReadProbeDaemon()
    use_reconcile_daemon(monkeypatch, fake)
    return fake


@pytest.fixture
def events(capture_events: Callable[..., list[CapturedEvent]]) -> list[CapturedEvent]:
    """Every event the acts module records (not the seam's task.transition)."""
    return capture_events(_ACTS)


def _no_owner_row(
    client: ClientConfig, ticket_id: str = _TICKET, **row: object
) -> TicketTask:
    """An unbound RUNNING row whose claiming process is gone and context missing."""
    _worktree(client, ticket_id)
    fields: dict[str, object] = {
        "attempts": 1,
        "claimed_at": _CLAIM,
        "launch_marker": dead_launch_marker(),
        "pr_url": _PR,
        **row,
    }
    return mk_unowned_running_row(client=_CLIENT, ticket_id=ticket_id, **fields)


def _populated(daemon: FakeNativeDaemonClient, tmp_path: Path) -> None:
    """Seed an unrelated worker so the roster is readable and non-empty (R13)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    daemon.seed_live_worker(elsewhere)


def _types(events: list[CapturedEvent]) -> list[OrchestratorEventType]:
    return [e[0] for e in events]


def _proposals(events: list[CapturedEvent]) -> list[dict[str, object]]:
    return [e[1] for e in events if e[0] is OrchestratorEventType.SESSION_REAP_PROPOSED]


def _pages(events: list[CapturedEvent]) -> list[dict[str, object]]:
    return [
        e[1] for e in events if e[0] is OrchestratorEventType.SESSION_NEEDS_ATTENTION
    ]


class TestNoOwner:
    def test_signal_only_parks(
        self, client_cfg: ClientConfig, events: list[CapturedEvent]
    ) -> None:
        _no_owner_row(client_cfg, unproductive_attempts=1)

        assert _sweep(client_cfg, now=_OLD) == []

        row = _row()
        assert row.status is QueueItemStatus.BLOCKED_ON_USER
        assert row.disposition == "unowned_running_row"
        assert row.blocked_reason == (
            f"no worker owns this RUNNING row (claiming process {_DEAD_PID} is gone)"
        )
        assert row.unproductive_attempts == 1
        assert row.pr_url == _PR
        assert row.session_id is None
        assert row.ever_spawned is False
        assert _types(events) == [
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        ]
        assert _proposals(events)[0]["proposed_action"] == "park_blocked_on_user"

    def test_auto_reverts_without_a_page(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        daemon: RosterReadProbeDaemon,
        tmp_path: Path,
    ) -> None:
        _populated(daemon, tmp_path)
        _no_owner_row(client_cfg, unproductive_attempts=1)

        _sweep(client_cfg, now=_OLD, config=_AUTO)

        row = _row()
        assert row.status is QueueItemStatus.PENDING
        assert row.unproductive_attempts == 1
        assert _types(events) == [OrchestratorEventType.SESSION_REAP_PROPOSED]
        assert _proposals(events)[0]["proposed_action"] == "revert_task"

    def test_lane_signal_only_overrides_global_auto(
        self, tmp_path: Path, daemon: RosterReadProbeDaemon
    ) -> None:
        client = ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
            lanes=[LaneConfig(name="default", reap_policy=ReapPolicy.SIGNAL_ONLY)],
        )
        _populated(daemon, tmp_path)
        _no_owner_row(client)

        _sweep(client, now=_OLD, config=_AUTO)

        assert _row().status is QueueItemStatus.BLOCKED_ON_USER

    def test_reap_proposed_payload(
        self, client_cfg: ClientConfig, events: list[CapturedEvent]
    ) -> None:
        _no_owner_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        assert next(e[2] for e in events if e[1] is _proposals(events)[0]) == _TICKET
        assert _proposals(events) == [
            {
                "session_id": "",
                "session_name": None,
                "client": _CLIENT,
                "ticket_id": _TICKET,
                "lane": "default",
                "proposed_action": "park_blocked_on_user",
                "reason": "unowned_running_row",
                "evidence": {
                    "roster_readable": True,
                    "worktree_path": str(_worktree(client_cfg)),
                    "context_state": "missing",
                    "launch_owner_pid": _DEAD_PID,
                    "launch_owner_alive": False,
                    "claim_age_seconds": 300.0,
                },
            }
        ]

    def test_proposal_precedes_the_transition(self, client_cfg: ClientConfig) -> None:
        _no_owner_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        recorded = read_events(
            event_types=[
                OrchestratorEventType.SESSION_REAP_PROPOSED,
                OrchestratorEventType.TASK_TRANSITION,
            ]
        )
        assert [e.type for e in recorded] == [
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            OrchestratorEventType.TASK_TRANSITION,
        ]

    def test_park_page_has_the_canonical_keys(
        self, client_cfg: ClientConfig, events: list[CapturedEvent]
    ) -> None:
        _no_owner_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        page = _pages(events)[0]
        assert set(page) == {
            "session_id",
            "session_name",
            "client",
            "ticket_id",
            "claude_session_id",
            "paused_status",
            "breadcrumbs",
            "crashed",
            "lane",
        }
        assert page["session_id"] == f"no-owner:{_CLIENT}/{_TICKET}#1"
        assert page["session_name"] == ""
        assert page["claude_session_id"] is None
        assert page["paused_status"] == "unowned_running_row"
        assert page["crashed"] is False
        assert page["lane"] == "default"
        assert page["breadcrumbs"] == no_owner_breadcrumbs(
            client=_CLIENT,
            ticket=_TICKET,
            wt=str(_worktree(client_cfg)),
            pid=_DEAD_PID,
            context="cw-context.json is missing",
        )

    def test_stops_nothing_and_keeps_the_worktree(
        self, client_cfg: ClientConfig, daemon: RosterReadProbeDaemon
    ) -> None:
        _no_owner_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        assert daemon.stop_calls == []
        assert _worktree(client_cfg).is_dir()

    def test_cap_oldest_claim_first(
        self, client_cfg: ClientConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        claims: dict[str, datetime | None] = {
            "GEN-1": _CLAIM + timedelta(minutes=2),
            "GEN-2": None,
            "GEN-3": _CLAIM,
            "GEN-4": _CLAIM.replace(tzinfo=None),
            "GEN-5": _CLAIM + timedelta(minutes=1),
        }
        for ticket_id, claimed_at in claims.items():
            _no_owner_row(client_cfg, ticket_id, claimed_at=claimed_at)
        caplog.set_level(logging.INFO, logger=_ACTS)

        _sweep(client_cfg, now=_OLD)

        parked = {
            t.ticket_id
            for t in load_dev_queue().tasks
            if t.status is QueueItemStatus.BLOCKED_ON_USER
        }
        assert UNOWNED_ACTS_MAX_PER_TICK == 3
        assert parked == {"GEN-2", "GEN-4", "GEN-3"}
        assert any("cap" in r.getMessage() for r in caplog.records)

    def test_cap_logs_the_rows_left_waiting(
        self, client_cfg: ClientConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        for minute in range(5):
            _no_owner_row(
                client_cfg,
                f"GEN-{minute + 1}",
                claimed_at=_CLAIM + timedelta(minutes=minute),
            )
        caplog.set_level(logging.INFO, logger=_ACTS)

        _sweep(client_cfg, now=_OLD + timedelta(minutes=5))

        messages = [r.getMessage() for r in caplog.records]
        assert (
            "unowned_running: per-tick act cap reached; 2 row(s) wait a tick"
            in messages
        )

    def test_waiting_rows_do_not_consume_the_act_cap(
        self,
        client_cfg: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#3462: the cap counts acts, not attempts. Three older rows that
        wait (their claim changed since detect) leave the newest one a slot."""
        for minute in range(4):
            _no_owner_row(
                client_cfg,
                f"GEN-{minute + 1}",
                claimed_at=_CLAIM + timedelta(minutes=minute),
            )

        def _reattempt_oldest_three(store: DevQueueStore) -> None:
            for task in store.tasks[:3]:
                task.attempts += 1

        _wrap_detect(monkeypatch, lambda: _edit_queue(_reattempt_oldest_three))
        caplog.set_level(logging.INFO, logger=_ACTS)

        _sweep(client_cfg, now=_OLD + timedelta(minutes=4))

        statuses = {t.ticket_id: t.status for t in load_dev_queue().tasks}
        assert statuses == {
            "GEN-1": QueueItemStatus.RUNNING,
            "GEN-2": QueueItemStatus.RUNNING,
            "GEN-3": QueueItemStatus.RUNNING,
            "GEN-4": QueueItemStatus.BLOCKED_ON_USER,
        }
        waits = [
            r.getMessage()
            for r in caplog.records
            if "the row changed since detect" in r.getMessage()
        ]
        assert len(waits) == 3

    @pytest.mark.parametrize(
        ("target", "error"),
        [
            ("_mutate", CwError("transition refused")),
            ("save_dev_queue", OSError("disk full")),
            ("load_dev_queue", json.JSONDecodeError("Expecting value", "", 0)),
        ],
        ids=["mutate-cwerror", "save-oserror", "load-jsondecodeerror"],
    )
    def test_failed_mutation_does_not_abort_the_sweep(
        self,
        client_cfg: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        target: str,
        error: Exception,
    ) -> None:
        """#3461: a failure parking one row is logged and retried next tick;
        the sweep still acts on the next candidate."""
        _no_owner_row(client_cfg, "GEN-1", claimed_at=_CLAIM)
        _no_owner_row(client_cfg, "GEN-2", claimed_at=_CLAIM + timedelta(minutes=1))
        real = getattr(acts_module, target)
        calls: list[int] = []

        def _fail_first(*args: object, **kwargs: object) -> object:
            calls.append(1)
            if len(calls) == 1:
                raise error
            return real(*args, **kwargs)

        monkeypatch.setattr(acts_module, target, _fail_first)
        caplog.set_level(logging.INFO, logger=_ACTS)

        _sweep(client_cfg, now=_OLD + timedelta(minutes=1))

        assert _row("GEN-1").status is QueueItemStatus.RUNNING
        assert _row("GEN-2").status is QueueItemStatus.BLOCKED_ON_USER
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            m.startswith(f"unowned_running: {_CLIENT}/GEN-1 not acted on:")
            and m.endswith("retrying next tick")
            for m in warnings
        )

    def test_running_sibling_appearing_before_the_act_waits(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#3463: a second RUNNING row for the same ticket that lands between
        detect and the act makes the act wait."""
        _no_owner_row(client_cfg)
        _wrap_detect(
            monkeypatch,
            lambda: mk_unowned_running_row(
                client=_CLIENT,
                ticket_id=_TICKET,
                attempts=1,
                claimed_at=_CLAIM,
                session_id="sess-sibling",
            ),
        )
        caplog.set_level(logging.INFO, logger=_ACTS)

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert _proposals(events) == []
        assert any(
            r.getMessage().endswith("not acted on: the row changed since detect")
            for r in caplog.records
        )

    @staticmethod
    def _remove(store: DevQueueStore) -> None:
        store.tasks.clear()

    @staticmethod
    def _move_status(store: DevQueueStore) -> None:
        store.tasks[0].status = QueueItemStatus.PENDING

    @pytest.mark.parametrize(
        "vanish", ["_remove", "_move_status"], ids=["removed", "status_moved"]
    )
    def test_row_no_longer_running_waits(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        vanish: str,
    ) -> None:
        """#3463: a row removed or moved out of RUNNING since detect is left
        alone."""
        _no_owner_row(client_cfg)
        _wrap_detect(monkeypatch, lambda: _edit_queue(getattr(self, vanish)))
        caplog.set_level(logging.INFO, logger=_ACTS)

        _sweep(client_cfg, now=_OLD)

        assert events == []
        assert any(
            r.getMessage().endswith("not acted on: the row is no longer RUNNING")
            for r in caplog.records
        )

    def test_roster_is_re_read_under_dev_queue_lock(
        self, client_cfg: ClientConfig, daemon: RosterReadProbeDaemon
    ) -> None:
        _no_owner_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.BLOCKED_ON_USER
        # Detect: the snapshot and live_home_reason's read, both lock-free.
        assert daemon.reads[:2] == [False, False]
        # The act: every read is under dev_queue_lock.
        assert daemon.reads[2:]
        assert all(daemon.reads[2:])

    @pytest.mark.parametrize("change", ["unreadable", "changed", "homed"])
    def test_in_lock_roster_change_waits(
        self,
        client_cfg: ClientConfig,
        daemon: RosterReadProbeDaemon,
        events: list[CapturedEvent],
        tmp_path: Path,
        change: str,
    ) -> None:
        _no_owner_row(client_cfg)
        wt = _worktree(client_cfg)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        def _on_read(fake: RosterReadProbeDaemon, _n: int) -> None:
            # Only the act's first in-lock read changes; detect saw an empty roster.
            if not fake.reads[-1] or fake.reads.count(True) > 1:
                return
            if change == "unreadable":
                fake.roster_unreadable = True
            else:
                fake.seed_live_worker(elsewhere if change == "changed" else wt)

        daemon.on_read = _on_read

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert events == []

    def test_auto_never_reverts_on_an_empty_roster(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """R13: an empty roster matching an empty snapshot proves nothing."""
        _no_owner_row(client_cfg)
        caplog.set_level(logging.INFO, logger=_ACTS)

        _sweep(client_cfg, now=_OLD, config=_AUTO)

        assert _row().status is QueueItemStatus.RUNNING
        assert events == []
        assert any("R13" in r.getMessage() for r in caplog.records)

    def test_signal_only_parks_on_an_empty_roster(
        self, client_cfg: ClientConfig
    ) -> None:
        _no_owner_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.BLOCKED_ON_USER

    @staticmethod
    def _bind(store: DevQueueStore) -> None:
        store.tasks[0].session_id = "sess-late"

    @staticmethod
    def _reattempt(store: DevQueueStore) -> None:
        store.tasks[0].attempts = 2

    @staticmethod
    def _reclaim(store: DevQueueStore) -> None:
        store.tasks[0].claimed_at = _CLAIM + timedelta(seconds=1)

    @staticmethod
    def _remark(store: DevQueueStore) -> None:
        store.tasks[0].launch_marker = LaunchMarker(pid=_DEAD_PID + 1, start_time_ns=1)

    @staticmethod
    def _exempt(store: DevQueueStore) -> None:
        store.tasks[0].fix_dispatch_session_id = "sess-fix"

    @pytest.mark.parametrize(
        "mutate", ["_bind", "_reattempt", "_reclaim", "_remark", "_exempt"]
    )
    def test_row_changed_between_detect_and_act(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
        mutate: str,
    ) -> None:
        _no_owner_row(client_cfg)
        edit: Callable[[DevQueueStore], None] = getattr(self, mutate)
        _wrap_detect(monkeypatch, lambda: _edit_queue(edit))

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert _proposals(events) == []

    def test_context_that_now_ties_waits(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _no_owner_row(client_cfg)

        def _tie() -> None:
            _claim_context(_worktree(client_cfg), attempts=1)
            _seed(_session())

        _wrap_detect(monkeypatch, _tie)

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert _proposals(events) == []

    def test_session_absent_recorded_meanwhile_waits(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _no_owner_row(client_cfg)
        _claim_context(_worktree(client_cfg), attempts=1)
        _wrap_detect(monkeypatch, lambda: _seed(_session()))

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert _proposals(events) == []

    def test_session_absent_renamed_meanwhile_waits(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _no_owner_row(client_cfg)
        wt = _worktree(client_cfg)
        _claim_context(wt, attempts=1)
        _wrap_detect(
            monkeypatch,
            lambda: _claim_context(wt, attempts=1, session_id="sess-other"),
        )

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert _proposals(events) == []

    def test_recorded_live_session_homed_meanwhile_waits(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The roster is unchanged, but state now records a live session there."""
        _no_owner_row(client_cfg)
        wt = _worktree(client_cfg)
        _wrap_detect(
            monkeypatch,
            lambda: _seed(
                _session(id="sess-live", status=SessionStatus.ACTIVE, worktree_path=wt)
            ),
        )

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert _proposals(events) == []

    def test_session_absent_still_absent_parks(
        self, client_cfg: ClientConfig, events: list[CapturedEvent]
    ) -> None:
        _no_owner_row(client_cfg)
        _claim_context(_worktree(client_cfg), attempts=1)

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.BLOCKED_ON_USER
        evidence = _proposals(events)[0]["evidence"]
        assert isinstance(evidence, dict)
        assert evidence["context_state"] == "session_absent"

    def test_pid_namespace_flipped_between_detect_and_act(
        self,
        client_cfg: ClientConfig,
        events: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _no_owner_row(client_cfg)
        _wrap_detect(
            monkeypatch,
            lambda: monkeypatch.setattr(
                "cw.launch_marker.in_initial_pid_namespace", lambda **_kw: False
            ),
        )

        _sweep(client_cfg, now=_OLD)

        assert _row().status is QueueItemStatus.RUNNING
        assert _proposals(events) == []

    def test_second_run_is_idempotent(
        self, client_cfg: ClientConfig, events: list[CapturedEvent]
    ) -> None:
        _no_owner_row(client_cfg)
        _sweep(client_cfg, now=_OLD)
        count = len(events)
        before = load_dev_queue().model_dump_json()

        _sweep(client_cfg, now=_OLD)

        assert len(events) == count
        assert load_dev_queue().model_dump_json() == before

    def test_failed_proposal_skips_the_act(
        self,
        client_cfg: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """ADOPT-0006 invariant 3: no proposal, no act; the next tick retries."""
        failures = _failing_record_event(
            monkeypatch,
            target=f"{_ACTS}.record_event",
            event_type=OrchestratorEventType.SESSION_REAP_PROPOSED,
            fail_for=lambda _payload: True,
        )
        _no_owner_row(client_cfg)
        caplog.set_level(logging.WARNING, logger=_ACTS)

        _sweep(client_cfg, now=_OLD)

        assert failures == [1]
        assert _row().status is QueueItemStatus.RUNNING
        assert any(
            r.levelno == logging.WARNING and "reap_proposed" in r.getMessage()
            for r in caplog.records
        )

    def test_failed_page_keeps_the_park(
        self,
        client_cfg: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        failures = _failing_record_event(
            monkeypatch,
            target=f"{_ACTS}.record_event",
            event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            fail_for=lambda _payload: True,
        )
        _no_owner_row(client_cfg)
        caplog.set_level(logging.WARNING, logger=_ACTS)

        _sweep(client_cfg, now=_OLD)

        assert failures == [1]
        assert _row().status is QueueItemStatus.BLOCKED_ON_USER
        assert any("page" in r.getMessage() for r in caplog.records)


class TestRunningSiblings:
    """#3460: the one sibling predicate detect and the acts share."""

    @staticmethod
    def _task(
        ticket_id: str = _TICKET,
        client: str = _CLIENT,
        status: QueueItemStatus = QueueItemStatus.RUNNING,
    ) -> TicketTask:
        return TicketTask(ticket_id=ticket_id, client=client, status=status)

    def test_returns_other_running_rows_for_same_client_and_ticket(self) -> None:
        row, first, second = self._task(), self._task(), self._task()
        store = DevQueueStore(tasks=[row, first, second])

        siblings = running_siblings(store, row)

        assert len(siblings) == 2
        assert siblings[0] is first
        assert siblings[1] is second

    def test_excludes_self_other_ticket_other_client_and_non_running_rows(
        self,
    ) -> None:
        row = self._task()
        store = DevQueueStore(
            tasks=[
                row,
                self._task(ticket_id="GEN-OTHER"),
                self._task(client="client-other"),
                self._task(status=QueueItemStatus.PENDING),
                self._task(status=QueueItemStatus.BLOCKED_ON_USER),
            ]
        )

        assert running_siblings(store, row) == []

    def test_empty_when_alone(self) -> None:
        row = self._task()

        assert running_siblings(DevQueueStore(tasks=[row]), row) == []


class TestClassifyClaimContext:
    @staticmethod
    def _row() -> TicketTask:
        return TicketTask(
            ticket_id=_TICKET,
            client=_CLIENT,
            status=QueueItemStatus.RUNNING,
            attempts=1,
        )

    @pytest.mark.parametrize(
        ("context", "state"),
        [
            ({"attempt": True, "ticket_id": _TICKET, "client": _CLIENT}, "no_attempt"),
            (
                {"attempt": 1, "ticket_id": _TICKET, "client": _CLIENT},
                "no_attempt",
            ),
            (
                {
                    "attempt": 1,
                    "ticket_id": _TICKET,
                    "client": _CLIENT,
                    "session_id": 42,
                },
                "no_attempt",
            ),
            ({"attempt": 1, "ticket_id": "GEN-0", "client": _CLIENT}, "other_claim"),
            (
                {"attempt": 2, "ticket_id": _TICKET, "client": _CLIENT},
                "attempt_mismatch",
            ),
            (
                {
                    "attempt": 1,
                    "ticket_id": _TICKET,
                    "client": _CLIENT,
                    "session_id": "s",
                },
                "ok",
            ),
        ],
        ids=[
            "bool_attempt",
            "no_session_id",
            "non_string_session_id",
            "other_ticket",
            "other_attempt",
            "ties",
        ],
    )
    def test_states(self, context: dict[str, object], state: str) -> None:
        assert classify_claim_context(context, self._row()).state == state


def _unreadable_row(client: ClientConfig, **row: object) -> TicketTask:
    """An unbound row whose context cannot be read and whose owner is gone."""
    write_unreadable_claim_context(_worktree(client), "{not json")
    fields: dict[str, object] = {
        "attempts": 1,
        "claimed_at": _CLAIM,
        "launch_marker": dead_launch_marker(),
        **row,
    }
    return mk_unowned_running_row(client=_CLIENT, ticket_id=_TICKET, **fields)


def _unconfirmed_pages(ticket_id: str = _TICKET) -> list[dict[str, object]]:
    return _attention_events(
        "unowned-acts-test", ticket_id, paused_status="unowned_running_unconfirmed"
    )


class TestSignal:
    def test_one_page_and_one_note_per_claim_and_cause(
        self, client_cfg: ClientConfig
    ) -> None:
        _unreadable_row(client_cfg)

        _sweep(client_cfg, now=_OLD)
        _sweep(client_cfg, now=_OLD)

        pages = _unconfirmed_pages()
        assert len(pages) == 1
        assert pages[0]["session_id"] == (
            f"unconfirmed:{_CLIENT}/{_TICKET}#1:{CAUSE_UNREADABLE_CONTEXT}"
        )
        assert _row().advisory_note == unconfirmed_note(CAUSE_UNREADABLE_CONTEXT)

    def test_page_text(self, client_cfg: ClientConfig) -> None:
        _unreadable_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        wt = _worktree(client_cfg)
        assert _unconfirmed_pages()[0]["breadcrumbs"] == unconfirmed_breadcrumbs(
            client=_CLIENT,
            ticket=_TICKET,
            claimed_at=_CLAIM,
            evidence=f"{wt}/.claude/cw-context.json exists but cannot be read as JSON",
        )

    def test_changed_cause_re_pages(self, client_cfg: ClientConfig) -> None:
        _unreadable_row(client_cfg)
        _sweep(client_cfg, now=_OLD)
        (_worktree(client_cfg) / HOOK_CONTEXT_RELATIVE_PATH).unlink()

        _sweep(client_cfg, now=_OLD, native_live={"0000abcd"})

        assert len(_unconfirmed_pages()) == 2
        assert _row().advisory_note == unconfirmed_note(CAUSE_ROSTER_UNPROVEN)

    def test_changed_live_count_does_not_re_page(
        self, client_cfg: ClientConfig
    ) -> None:
        """R10: the stored note carries no count, so a new count is the same key."""
        _no_owner_row(client_cfg)

        _sweep(client_cfg, now=_OLD, native_live={"0000abcd"})
        _sweep(client_cfg, now=_OLD, native_live={"0000abcd", "0000abce"})

        assert len(_unconfirmed_pages()) == 1
        assert _row().status is QueueItemStatus.RUNNING

    def test_changed_duplicate_count_does_not_re_page(
        self, client_cfg: ClientConfig
    ) -> None:
        for _ in range(2):
            _unreadable_row(client_cfg)
        _sweep(client_cfg, now=_OLD)
        assert len(_unconfirmed_pages()) == 2
        mk_unowned_running_row(
            client=_CLIENT,
            ticket_id=_TICKET,
            attempts=1,
            claimed_at=_CLAIM,
            launch_marker=dead_launch_marker(),
        )

        _sweep(client_cfg, now=_OLD)

        assert len(_unconfirmed_pages()) == 3

    def test_failed_page_leaves_the_row_unlatched(
        self, client_cfg: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failures = _failing_record_event(
            monkeypatch,
            target=f"{_ACTS}.record_event",
            event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            fail_for=lambda _payload: True,
        )
        _unreadable_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        assert failures == [1]
        assert _row().advisory_note is None

    def test_row_changed_after_the_page_is_not_latched(
        self,
        client_cfg: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _record_and_reclaim(
            _etype: OrchestratorEventType,
            _payload: dict[str, object] | None = None,
            *,
            correlation_id: str | None = None,
        ) -> None:
            del correlation_id

            def _reclaim(store: DevQueueStore) -> None:
                store.tasks[0].claimed_at = _CLAIM + timedelta(seconds=1)

            _edit_queue(_reclaim)

        monkeypatch.setattr(f"{_ACTS}.record_event", _record_and_reclaim)
        _unreadable_row(client_cfg)

        _sweep(client_cfg, now=_OLD)

        assert _row().advisory_note is None

    def test_only_the_note_changes(self, client_cfg: ClientConfig) -> None:
        _unreadable_row(client_cfg)
        before = _row().model_dump()

        _sweep(client_cfg, now=_OLD)

        after = _row().model_dump()
        assert after.pop("advisory_note") == unconfirmed_note(CAUSE_UNREADABLE_CONTEXT)
        before.pop("advisory_note")
        assert after == before
        assert _row().status is QueueItemStatus.RUNNING

    def test_adopt_clears_the_note(self, client_cfg: ClientConfig) -> None:
        _unreadable_row(client_cfg)
        _sweep(client_cfg, now=_OLD)
        assert _row().advisory_note is not None
        _claim_context(_worktree(client_cfg), attempts=1)
        _seed(_session())

        assert _sweep(client_cfg, now=_OLD) == [_TICKET]

        assert _row().advisory_note is None
        assert _row().launch_marker is None


_T_CLAIM = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

# The ticket's evidence table, rendered with the facts below.
_FACTS: dict[str, str] = {
    "client": "client-a",
    "ticket": "GEN-1",
    "sid": "sess-1",
    "wt": "/w/GEN-1",
    "other_created": "2026-10-07T08:00:00+00:00",
    "s_client": "client-b",
    "s_ticket": "GEN-2",
    "started": "2026-10-07T08:30:00+00:00",
    "names": "aider, codex",
    "reason": "3 RUNNING row(s) in scope are bound",
}
_EVIDENCE_EXPECTED: dict[str, str] = {
    "duplicate": (
        "another RUNNING row exists for client-a/GEN-1 (created "
        "2026-10-07T08:00:00+00:00), so no session can be matched to this one"
    ),
    "bound_elsewhere": (
        "cw-context.json names session sess-1, which another row already owns"
    ),
    "conflicting_session": (
        "cw-context.json names session sess-1, but that session belongs to "
        "client-b/GEN-2, not client-a/GEN-1"
    ),
    "stale_session": (
        "cw-context.json names session sess-1, which started at "
        "2026-10-07T08:30:00+00:00 before this claim"
    ),
    "naive_datetime": (
        "a timestamp on session sess-1 or on the claim carries no timezone, so "
        "their order cannot be checked"
    ),
    "unreadable_context": (
        "/w/GEN-1/.claude/cw-context.json exists but cannot be read as JSON"
    ),
    "non_native": (
        "client client-a configures a non-claude-native executor (aider, "
        "codex), whose workers this check cannot see on the daemon roster"
    ),
    "roster_unproven": (
        "the daemon roster lists no workers while 3 RUNNING row(s) in scope "
        "are bound, so an absent worker is not evidence of a dead one"
    ),
}


class TestTexts:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (CONTEXT_MISSING, "cw-context.json is missing"),
            (CONTEXT_NO_ATTEMPT, "cw-context.json records no attempt"),
            (
                CONTEXT_ATTEMPT_MISMATCH,
                "cw-context.json records attempt 1, not this claim's attempt 2",
            ),
            (CONTEXT_OTHER_CLAIM, "cw-context.json names another ticket or client"),
            (
                CONTEXT_SESSION_ABSENT,
                "cw-context.json names session sess-1, which is not in sessions.json",
            ),
        ],
    )
    def test_context_clause_rows(self, state: str, expected: str) -> None:
        assert (
            context_clause(
                state, claim_attempt=2, context_attempt=1, session_id="sess-1"
            )
            == expected
        )

    def test_no_owner_breadcrumb_verbatim(self) -> None:
        text = no_owner_breadcrumbs(
            client="client-a",
            ticket="GEN-1",
            wt="/w/GEN-1",
            pid=4242,
            context="cw-context.json is missing",
        )

        assert text == (
            "No worker owns this RUNNING row: the process that claimed it (pid "
            "4242) is gone, the daemon roster is readable and lists no worker "
            "homed on /w/GEN-1, and cw-context.json is missing. The row was "
            "parked BLOCKED_ON_USER; nothing was stopped, reverted or deleted. "
            "Release it with `cw dev-queue requeue GEN-1 -c client-a` (it "
            "refuses while a recorded live session exists or the daemon roster "
            "is unreadable) or with `cw doctor --reap`."
        )

    def test_blocked_reason_verbatim(self) -> None:
        assert no_owner_blocked_reason(4242) == (
            "no worker owns this RUNNING row (claiming process 4242 is gone)"
        )

    @pytest.mark.parametrize("cause", sorted(_EVIDENCE_EXPECTED))
    def test_evidence_rows_verbatim(self, cause: str) -> None:
        assert unconfirmed_evidence(cause, **_FACTS) == _EVIDENCE_EXPECTED[cause]

    def test_every_cause_has_an_evidence_row(self) -> None:
        assert set(ALL_CAUSES) == set(_EVIDENCE_EXPECTED)

    @pytest.mark.parametrize(
        ("claimed_at", "claimed"),
        [
            (_T_CLAIM, "2026-10-07T09:00:00+00:00"),
            (None, "unknown (row predates schema v44)"),
        ],
        ids=["claimed", "pre_v44"],
    )
    def test_unconfirmed_breadcrumb_verbatim(
        self, claimed_at: datetime | None, claimed: str
    ) -> None:
        text = unconfirmed_breadcrumbs(
            client="client-a", ticket="GEN-1", claimed_at=claimed_at, evidence="E"
        )

        assert text == (
            f"Ownership of RUNNING row client-a/GEN-1 (claimed {claimed}) cannot "
            "be confirmed: E. cw changed nothing; the row stays RUNNING and "
            "keeps its lane slot. If you confirm no worker is running for it, "
            "release it with `cw dev-queue cancel GEN-1 -c client-a` and then "
            "`cw dev-queue requeue GEN-1 -c client-a --from-cancelled`; that "
            "requeue refuses while a recorded live session exists or the daemon "
            "roster is unreadable."
        )

    @pytest.mark.parametrize("cause", ALL_CAUSES)
    def test_unconfirmed_text_never_names_doctor_reap(self, cause: str) -> None:
        text = unconfirmed_breadcrumbs(
            client="client-a",
            ticket="GEN-1",
            claimed_at=_T_CLAIM,
            evidence=unconfirmed_evidence(cause, **_FACTS),
        )

        assert "cw doctor --reap" not in text

    def test_prefix_fits_the_reason_cell(self) -> None:
        assert len(_UNOWNED_UNCONFIRMED_NOTE_PREFIX) == 20

    def test_names_sorted_and_deduplicated(self) -> None:
        assert backend_names_text(["codex", "aider", "codex"]) == "aider, codex"

    @pytest.mark.parametrize("cause", ALL_CAUSES)
    def test_stored_note_carries_no_count(self, cause: str) -> None:
        note = unconfirmed_note(cause)

        assert note.startswith(unconfirmed_note_head(cause))
        assert note.startswith(f"?unowned_unconfirmed: {cause}: ")
        assert not re.search(r"\d", note)

    def test_non_native_note_head(self) -> None:
        assert unconfirmed_note_head(CAUSE_NON_NATIVE) == (
            "?unowned_unconfirmed: non_native: "
        )

    @pytest.mark.parametrize(
        ("claimed_at", "expected"),
        [
            (_T_CLAIM, 90.0),
            (None, None),
            (_T_CLAIM.replace(tzinfo=None), None),
        ],
        ids=["aware", "none", "naive"],
    )
    def test_claim_age_seconds(
        self, claimed_at: datetime | None, expected: float | None
    ) -> None:
        now = datetime(2026, 10, 7, 9, 1, 30, tzinfo=UTC)

        assert claim_age_seconds(claimed_at, now) == expected


def _flat(text: str) -> str:
    return " ".join(text.split())


def _reap_proposed_section() -> str:
    text = EVENTS_DOC.read_text(encoding="utf-8")
    start = text.index("### `session.reap_proposed`")
    end = text.find("\n### ", start + 1)
    return text[start : end if end != -1 else len(text)]


class TestEventDocumented:
    @pytest.mark.parametrize(
        ("status", "session_id"),
        [
            (NO_OWNER_PAGE_REASON, "no-owner:<client>/<ticket>#<attempts>"),
            (
                UNCONFIRMED_PAGE_REASON,
                "unconfirmed:<client>/<ticket>#<attempts>:<cause>",
            ),
        ],
    )
    def test_paused_status_bullets(self, status: str, session_id: str) -> None:
        bullet = _flat(events_doc_bullet(f'- `"{status}"`'))

        assert f"`{session_id}`" in bullet
        assert "canonical nine keys" in bullet.lower()
        assert "No push notification" in bullet

    def test_unconfirmed_bullet_names_every_cause(self) -> None:
        bullet = _flat(events_doc_bullet(f'- `"{UNCONFIRMED_PAGE_REASON}"`'))

        for cause in ALL_CAUSES:
            assert f"`{cause}`" in bullet

    def test_reap_proposed_documents_the_no_owner_evidence(self) -> None:
        section = _flat(_reap_proposed_section())

        assert '`reason` is `"unowned_running_row"`' in section
        assert '`session_id` is `""`' in section
        for key in (
            "roster_readable",
            "worktree_path",
            "context_state",
            "launch_owner_pid",
            "launch_owner_alive",
            "claim_age_seconds",
        ):
            assert f"`evidence.{key}`" in section
        for state in (
            CONTEXT_MISSING,
            CONTEXT_NO_ATTEMPT,
            CONTEXT_ATTEMPT_MISMATCH,
            CONTEXT_OTHER_CLAIM,
            CONTEXT_SESSION_ABSENT,
        ):
            assert f"`{state}`" in section
        assert "acts on nothing" in section


class TestDocsNameValidCommands:
    """The new bullets name real commands, in the order that makes them valid."""

    def test_unconfirmed_bullet_names_cancel_then_requeue_from_cancelled(
        self,
    ) -> None:
        bullet = _flat(events_doc_bullet(f'- `"{UNCONFIRMED_PAGE_REASON}"`'))

        cancel = bullet.index("`cw dev-queue cancel <ticket> -c <client>`")
        requeue = bullet.index(
            "`cw dev-queue requeue <ticket> -c <client> --from-cancelled`"
        )
        assert cancel < requeue
        assert "cw doctor --reap" not in bullet

    def test_no_owner_bullet_names_requeue_and_doctor_reap(self) -> None:
        bullet = _flat(events_doc_bullet(f'- `"{NO_OWNER_PAGE_REASON}"`'))

        assert "`cw dev-queue requeue <ticket> -c <client>`" in bullet
        assert "`cw doctor --reap` (class 5)" in bullet

    @pytest.mark.parametrize(
        ("args", "flag"),
        [
            (["dev-queue", "cancel"], None),
            (["dev-queue", "requeue"], "--from-cancelled"),
            (["doctor"], "--reap"),
        ],
        ids=["cancel", "requeue", "doctor_reap"],
    )
    def test_named_commands_exist(self, args: list[str], flag: str | None) -> None:
        result = CliRunner().invoke(main, [*args, "--help"])

        assert result.exit_code == 0, result.output
        if flag is not None:
            assert flag in result.output
