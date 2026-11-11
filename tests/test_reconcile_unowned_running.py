"""Unit tests for cw.reconcile.unowned_running (#2591, part 1: ADOPT).

A RUNNING row whose post-launch dev-queue stamp failed has no ``session_id``,
so neither the dispatch completion consumer nor the Stop-hook router can ever
match it. The sweep binds such a row to the recorded session its worktree's
``cw-context.json`` ties to this exact claim (same client, ticket and attempt,
and a session that did not start before the claim), writing exactly what
dispatch's own spawn-success stamp writes. Anything short of that proof leaves
the row alone.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    ClientConfig,
    CwState,
    DevQueueStore,
    LaneConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    StageExecutorConfig,
    StagePipelineConfig,
    UsageLimitAct,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import _deps, reconcile
from cw.reconcile.dirty_checks import DirtyChecks
from cw.reconcile.unowned_running import (
    UnownedCandidate,
    UnownedFindings,
    detect_unowned_running,
    run_unowned_running_recovery,
)
from cw.reconcile.unowned_running_text import (
    CAUSE_BOUND_ELSEWHERE,
    CAUSE_CONFLICTING_SESSION,
    CAUSE_DUPLICATE,
    CAUSE_NAIVE_DATETIME,
    CAUSE_NON_NATIVE,
    CAUSE_ROSTER_UNPROVEN,
    CAUSE_STALE_SESSION,
    CAUSE_UNREADABLE_CONTEXT,
    CONTEXT_ATTEMPT_MISMATCH,
    CONTEXT_MISSING,
    CONTEXT_NO_ATTEMPT,
    CONTEXT_OTHER_CLAIM,
    CONTEXT_SESSION_ABSENT,
    unconfirmed_note,
)
from tests._clients_yaml import write_clients_yaml
from tests._dirty_check_helpers import DIRTY_CHECKS_LOGGER, unavailable_records
from tests._reconcile_helpers import (
    _CLAIM,
    _CLIENT,
    _SID,
    _TICKET,
    EVENTS_DOC,
    _arrange,
    _attention_events,
    _claim_context,
    _edit_queue,
    _failing_record_event,
    _row,
    _seed,
    _session,
    _state_queue_snapshot,
    _sweep,
    _worktree,
    _wrap_detect,
    _write_agent_spawn_stamp,
    dead_launch_marker,
    events_doc_bullet,
    live_launch_marker,
    make_client_cfg,
    mk_unowned_running_row,
    use_reconcile_daemon,
    write_unreadable_claim_context,
)
from tests._shared.daemon_fakes import CapturedEvent
from tests._shared.session_seeding import _make_ticket_task
from tests._shared.stop_hook_context import _write_hook_context_file

_OTHER_TICKET = "GEN-2592"
# A naive timestamp, as a legacy or hand-edited state file can carry.
_NAIVE_START = (_CLAIM + timedelta(hours=1)).replace(tzinfo=None)
_MODULE = "cw.reconcile.unowned_running"
# Past the spawn grace: an UNKNOWN-verdict evidence cause signals at this age.
_OLD = _CLAIM + timedelta(minutes=5)


@pytest.fixture
def client_cfg(tmp_path: Path) -> ClientConfig:
    return make_client_cfg(tmp_path)


@pytest.fixture(autouse=True)
def daemon(monkeypatch: pytest.MonkeyPatch) -> FakeNativeDaemonClient:
    """The sweep's daemon: an in-memory roster, empty unless a test seeds it."""
    fake = FakeNativeDaemonClient()
    use_reconcile_daemon(monkeypatch, fake)
    return fake


@pytest.fixture
def adopted(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    return capture_events(_MODULE, OrchestratorEventType.TASK_SESSION_ADOPTED)


class TestAdopt:
    def test_binds_row_to_claim_session(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        _arrange(client_cfg, spawn_error_count=2, next_eligible_at=_CLAIM)

        assert _sweep(client_cfg) == [_TICKET]

        row = _row()
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == _SID
        assert row.ever_spawned is True
        assert row.spawn_error_count == 0
        assert row.next_eligible_at is None
        assert row.attempts == 1
        assert adopted == [
            (
                OrchestratorEventType.TASK_SESSION_ADOPTED,
                {
                    "client": _CLIENT,
                    "ticket_id": _TICKET,
                    "lane": "default",
                    "session_id": _SID,
                    "session_name": f"{_CLIENT}/auto-dev/{_TICKET}",
                    "attempt": 1,
                    "claimed_at": _CLAIM.isoformat(),
                },
                _TICKET,
            )
        ]

    @pytest.mark.parametrize(
        "started_after_claim",
        [timedelta(0), timedelta(hours=6)],
        ids=["at_claim", "long_after_claim"],
    )
    def test_adopts_any_session_started_at_or_after_claim(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        started_after_claim: timedelta,
    ) -> None:
        _arrange(client_cfg, session=_session(started_at=_CLAIM + started_after_claim))

        assert _sweep(client_cfg) == [_TICKET]
        assert _row().session_id == _SID
        assert len(adopted) == 1

    def test_legacy_row_without_claimed_at_adopts(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        """A pre-v44 row has no claim instant, so the started_at guard is moot."""
        _arrange(
            client_cfg,
            claimed_at=None,
            session=_session(started_at=_CLAIM - timedelta(days=3)),
        )

        assert _sweep(client_cfg) == [_TICKET]
        assert _row().session_id == _SID
        assert adopted[0][1]["claimed_at"] is None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": SessionStatus.COMPLETED},
            {"status": SessionStatus.TIMED_OUT},
            {"status": SessionStatus.ACTIVE, "surface_ref": "0000abcd"},
            {"origin": SessionOrigin.USER},
        ],
        ids=["completed", "timed_out", "active", "user_origin"],
    )
    def test_adopts_whatever_the_session_status_or_origin(
        self, client_cfg: ClientConfig, overrides: dict[str, object]
    ) -> None:
        _arrange(client_cfg, session=_session(**overrides))

        assert _sweep(client_cfg) == [_TICKET]
        assert _row().session_id == _SID

    def test_consumes_the_spawn_markers_at_review(
        self, client_cfg: ClientConfig
    ) -> None:
        _arrange(
            client_cfg,
            stage=Stage.REVIEW,
            regressed_into_stage=Stage.REVIEW,
            scope_drift_approved_extra_files=["src/extra.py"],
            scope_drift_approved_head="abc123",
            hook_context_conflict_session_id="sess-old",
            pending_operator_comment=True,
        )

        _sweep(client_cfg)

        row = _row()
        assert row.regressed_into_stage is None
        assert row.scope_drift_approved_extra_files is None
        assert row.scope_drift_approved_head is None
        assert row.hook_context_conflict_session_id is None
        assert row.pending_operator_comment is False

    def test_keeps_pending_operator_comment_outside_review(
        self, client_cfg: ClientConfig
    ) -> None:
        _arrange(client_cfg, stage=Stage.IMPL, pending_operator_comment=True)

        _sweep(client_cfg)

        assert _row().pending_operator_comment is True

    def test_runs_no_git_and_leaves_stage_base_ref(
        self, client_cfg: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_git(*_args: object, **_kwargs: object) -> str:
            msg = "adoption must not run git"
            raise AssertionError(msg)

        monkeypatch.setattr("cw.dispatch.claim.claimed_row.git_output", _no_git)
        _arrange(client_cfg, stage_base_ref="base0")

        assert _sweep(client_cfg) == [_TICKET]
        assert _row().stage_base_ref == "base0"

    def test_row_bound_between_detect_and_act_is_left_alone(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dispatch's own retry stamp won the race: nothing is overwritten."""
        _arrange(client_cfg)

        def _bind(store: DevQueueStore) -> None:
            store.tasks[0].session_id = "sess-dispatch"

        _wrap_detect(monkeypatch, lambda: _edit_queue(_bind))

        assert _sweep(client_cfg) == []
        assert _row().session_id == "sess-dispatch"
        assert adopted == []

    def test_session_bound_elsewhere_between_detect_and_act(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The in-lock re-check refuses a session another row took meanwhile."""
        _arrange(client_cfg)

        def _take(store: DevQueueStore) -> None:
            store.tasks.append(
                _make_ticket_task(
                    ticket_id=_OTHER_TICKET,
                    client=_CLIENT,
                    status=QueueItemStatus.BLOCKED_ON_USER,
                    session_id=_SID,
                )
            )

        _wrap_detect(monkeypatch, lambda: _edit_queue(_take))

        assert _sweep(client_cfg) == []
        assert _row().session_id is None
        assert adopted == []

    def test_duplicate_running_row_between_detect_and_act(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The in-lock re-check refuses once the (client, ticket) is ambiguous."""
        original = _arrange(client_cfg)

        def _duplicate(store: DevQueueStore) -> None:
            store.tasks.append(
                _make_ticket_task(
                    ticket_id=_TICKET,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    attempts=1,
                )
            )

        _wrap_detect(monkeypatch, lambda: _edit_queue(_duplicate))

        assert _sweep(client_cfg) == []
        rows = [t for t in load_dev_queue().tasks if t.ticket_id == _TICKET]
        assert [t.session_id for t in rows] == [None, None]
        assert rows[0].created_at == original.created_at
        assert adopted == []

    def test_second_run_is_idempotent(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        _arrange(client_cfg)
        _sweep(client_cfg)
        before = _state_queue_snapshot()

        assert _sweep(client_cfg) == []

        assert _state_queue_snapshot() == before
        assert len(adopted) == 1

    def test_naive_started_at_not_adopted_and_sweep_continues(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A naive datetime never raises: the row is skipped, the next adopted."""
        _arrange(client_cfg, session=_session(started_at=_NAIVE_START))
        other_sid = "sess-2592"
        _claim_context(
            _worktree(client_cfg, _OTHER_TICKET),
            attempts=1,
            session_id=other_sid,
            ticket_id=_OTHER_TICKET,
        )
        _seed(_session(id=other_sid, name=f"{_CLIENT}/auto-dev/{_OTHER_TICKET}"))
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_OTHER_TICKET, attempts=1, claimed_at=_CLAIM
        )
        caplog.set_level(logging.DEBUG, logger=_MODULE)

        assert _sweep(client_cfg) == [_OTHER_TICKET]

        assert _row().session_id is None
        assert _row().ever_spawned is False
        assert _row(_OTHER_TICKET).session_id == other_sid
        assert [e[1]["ticket_id"] for e in adopted] == [_OTHER_TICKET]
        assert any(
            "naive" in r.getMessage() and _TICKET in r.getMessage()
            for r in caplog.records
            if r.name == _MODULE
        )

    def test_naive_claimed_at_not_adopted(self, client_cfg: ClientConfig) -> None:
        _arrange(client_cfg, claimed_at=_CLAIM.replace(tzinfo=None))

        assert _sweep(client_cfg) == []
        assert _row().session_id is None

    def test_failed_event_write_is_swallowed_and_bind_kept(
        self,
        client_cfg: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The bind is durable before the audit event; a failed write is logged."""
        failures = _failing_record_event(
            monkeypatch,
            target=f"{_MODULE}.record_event",
            event_type=OrchestratorEventType.TASK_SESSION_ADOPTED,
            fail_for=lambda _payload: True,
        )
        _arrange(client_cfg)
        caplog.set_level(logging.ERROR, logger=_MODULE)

        assert _sweep(client_cfg) == [_TICKET]

        assert failures == [1]
        assert _row().session_id == _SID
        assert any(
            r.exc_info is not None and _TICKET in r.getMessage()
            for r in caplog.records
            if r.name == _MODULE and r.levelno == logging.ERROR
        )


class TestAdoptNotApplicable:
    """Every shape short of the proof: no mutation, no event."""

    @staticmethod
    def _assert_untouched(client: ClientConfig, adopted: list[CapturedEvent]) -> None:
        before = _state_queue_snapshot()

        assert _sweep(client) == []

        assert _state_queue_snapshot() == before
        assert adopted == []

    def test_attempt_mismatch(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        """The context is an earlier claim's: this row has been re-claimed."""
        _claim_context(_worktree(client_cfg), attempts=1)
        _seed(_session())
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=2, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    def test_context_without_attempt(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        """A resume writes no attempt key: no proof of this claim."""
        _write_hook_context_file(
            _worktree(client_cfg), session_id=_SID, ticket_id=_TICKET, client=_CLIENT
        )
        _seed(_session())
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=1, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    def test_stamp_only_context(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        _write_agent_spawn_stamp(
            _worktree(client_cfg), unresolved_count=0, stamped_at=_CLAIM
        )
        _seed(_session())
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=1, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    def test_missing_context(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        _worktree(client_cfg)
        _seed(_session())
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=1, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    @pytest.mark.parametrize(
        "text", ["{not json", "[1, 2]"], ids=["malformed", "non_object"]
    )
    def test_unreadable_context(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent], text: str
    ) -> None:
        write_unreadable_claim_context(_worktree(client_cfg), text)
        _seed(_session())
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=1, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    @pytest.mark.parametrize(
        "status",
        [SessionStatus.COMPLETED, SessionStatus.TIMED_OUT],
        ids=["completed", "timed_out"],
    )
    def test_stale_prior_row_session_not_adopted(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        status: SessionStatus,
    ) -> None:
        """Ambiguity 1, option (b): an earlier row's context matches attempt 1.

        The new row for the same ticket restarted at ``attempts == 1`` and
        crashed before its own launch wrote a context, so the worktree still
        holds the earlier row's. Its session started before this claim, so
        nothing ties it to this row.
        """
        _arrange(
            client_cfg,
            session=_session(status=status, started_at=_CLAIM - timedelta(hours=1)),
            stage=Stage.REVIEW,
            regressed_into_stage=Stage.REVIEW,
            scope_drift_approved_extra_files=["src/extra.py"],
            scope_drift_approved_head="abc123",
            hook_context_conflict_session_id="sess-old",
            pending_operator_comment=True,
        )

        self._assert_untouched(client_cfg, adopted)

        row = _row()
        assert row.session_id is None
        assert row.ever_spawned is False
        assert row.regressed_into_stage is Stage.REVIEW
        assert row.scope_drift_approved_extra_files == ["src/extra.py"]
        assert row.hook_context_conflict_session_id == "sess-old"
        assert row.pending_operator_comment is True

    @pytest.mark.parametrize(
        ("ticket_id", "client"),
        [(_OTHER_TICKET, _CLIENT), (_TICKET, "client-b")],
        ids=["ticket", "client"],
    )
    def test_context_names_another_claim(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        ticket_id: str,
        client: str,
    ) -> None:
        _claim_context(
            _worktree(client_cfg), attempts=1, ticket_id=ticket_id, client=client
        )
        _seed(_session())
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=1, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"name": f"{_CLIENT}/auto-dev/{_OTHER_TICKET}"},
            {"name": f"{_CLIENT}/impl"},
            {"client": "client-b"},
        ],
        ids=["other_ticket", "not_task_spawned", "other_client"],
    )
    def test_conflicting_session(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        overrides: dict[str, object],
    ) -> None:
        _arrange(client_cfg, session=_session(**overrides))

        self._assert_untouched(client_cfg, adopted)

    def test_context_session_absent_from_state(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        """The sessions.json write failed: part 2 of #2591, not adoption."""
        _claim_context(_worktree(client_cfg), attempts=1)
        save_state(CwState())
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=1, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    def test_context_session_already_bound_to_another_row(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        _arrange(client_cfg)
        _edit_queue(
            lambda store: store.tasks.append(
                _make_ticket_task(
                    ticket_id=_OTHER_TICKET,
                    client=_CLIENT,
                    status=QueueItemStatus.RUNNING,
                    session_id=_SID,
                )
            )
        )

        self._assert_untouched(client_cfg, adopted)

    def test_duplicate_running_rows(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        _arrange(client_cfg)
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_TICKET, attempts=1, claimed_at=_CLAIM
        )

        self._assert_untouched(client_cfg, adopted)

    @pytest.mark.parametrize(
        "names", [[], ["client-b"]], ids=["no_clients", "dangling_client"]
    )
    def test_client_out_of_scope(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        tmp_path: Path,
        names: list[str],
    ) -> None:
        _arrange(client_cfg)
        scoped = {
            name: ClientConfig(name=name, workspace_path=tmp_path / name)
            for name in names
        }
        before = _state_queue_snapshot()

        assert (
            run_unowned_running_recovery(
                now=_OLD, clients=scoped, config=OrchestratorConfig(), native_live=set()
            )
            == []
        )

        assert _state_queue_snapshot() == before
        assert adopted == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"fix_dispatch_session_id": "sess-fix"},
            {
                "usage_limit_act": UsageLimitAct(
                    session_id=_SID,
                    branch="park",
                    started_at=_CLAIM,
                    reset_at=None,
                    until=_CLAIM + timedelta(hours=1),
                )
            },
        ],
        ids=["fix_dispatch_held", "usage_limit_act"],
    )
    def test_backstop_exempt_row(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        overrides: dict[str, object],
    ) -> None:
        _arrange(client_cfg, **overrides)

        self._assert_untouched(client_cfg, adopted)

    @pytest.mark.parametrize(
        "status",
        [QueueItemStatus.PENDING, QueueItemStatus.BLOCKED_ON_USER],
        ids=["pending", "blocked_on_user"],
    )
    def test_non_running_row(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        status: QueueItemStatus,
    ) -> None:
        _arrange(client_cfg, status=status)

        self._assert_untouched(client_cfg, adopted)

    def test_already_bound_row(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        _arrange(client_cfg, session_id=_SID, ever_spawned=True)

        self._assert_untouched(client_cfg, adopted)


class TestNoOpAndInvariants:
    def test_detect_is_pure(self, client_cfg: ClientConfig) -> None:
        row = _arrange(client_cfg)
        before = _state_queue_snapshot()

        found = detect_unowned_running(
            load_dev_queue(),
            load_state(),
            {_CLIENT: client_cfg},
            now=_OLD,
            daemon=FakeNativeDaemonClient(),
            native_live=set(),
        )

        assert _state_queue_snapshot() == before
        assert found.no_owner == []
        assert found.signals == []
        assert found.adopts == [
            UnownedCandidate(
                ticket_id=_TICKET,
                client=_CLIENT,
                lane="default",
                created_at=row.created_at,
                attempts=1,
                claimed_at=_CLAIM,
                session_id=_SID,
                session_name=f"{_CLIENT}/auto-dev/{_TICKET}",
            )
        ]

    @staticmethod
    def _live_session(
        client: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[FakeNativeDaemonClient, Session]:
        """An ACTIVE session whose worker is live on the daemon roster."""
        write_clients_yaml(client)
        daemon = FakeNativeDaemonClient()
        short_id = daemon.seed_live_worker(_worktree(client))
        monkeypatch.setattr(
            "cw.reconcile._deps.get_native_daemon_client", lambda: daemon
        )
        monkeypatch.setattr(
            "cw.reconcile.core._claude_agents_json",
            lambda: [{"sessionId": f"{short_id}-6b3a-401b-bc3a-0d61b5b7a6ac"}],
        )
        session = _session(
            status=SessionStatus.ACTIVE,
            surface_ref=short_id,
            started_at=datetime.now(UTC),
        )
        return daemon, session

    def test_reconcile_adopts_under_sessions_lock(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The sweep runs inside reconcile(); the autouse lock tracer fails the
        test on any in-lock subprocess or lock-order violation."""
        daemon, session = self._live_session(client_cfg, monkeypatch)
        _arrange(
            client_cfg,
            claimed_at=session.started_at - timedelta(seconds=30),
            session=session,
        )

        reconcile()

        row = _row()
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == _SID
        assert len(adopted) == 1
        assert daemon.stop_calls == []
        assert daemon.spawn_calls == []

    def test_healthy_bound_row_is_untouched(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daemon, session = self._live_session(client_cfg, monkeypatch)
        _arrange(
            client_cfg,
            claimed_at=session.started_at - timedelta(seconds=30),
            session=session,
            session_id=_SID,
            ever_spawned=True,
        )
        before = load_dev_queue().model_dump_json()

        reconcile()

        assert load_dev_queue().model_dump_json() == before
        assert adopted == []
        assert daemon.stop_calls == []

    def test_naive_session_does_not_abort_the_reconcile_tick(
        self,
        client_cfg: ClientConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Operator note 1: the guard fails closed and later sweeps still run."""
        write_clients_yaml(client_cfg)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        _arrange(client_cfg, session=_session(started_at=_NAIVE_START))
        from cw.reconcile import locked_sweeps

        real_revert = locked_sweeps.revert_timed_out_tasks
        ran: list[str] = []

        def _record(dirty_checks: DirtyChecks | None = None) -> list[str]:
            ran.append("revert_timed_out_tasks")
            return real_revert(dirty_checks)

        monkeypatch.setattr(locked_sweeps, "revert_timed_out_tasks", _record)

        reconcile()

        assert ran == ["revert_timed_out_tasks"]
        assert _row().session_id is None

    def test_session_bound_in_lock_by_unowned_recovery_defers(
        self,
        client_cfg: ClientConfig,
        adopted: list[CapturedEvent],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """M9 (#2548): the row is bound to its COMPLETED session in-lock, after
        the dirty-check pre-pass ran, so the session was never captured. Its
        backstop defers a tick (row stays RUNNING and bound, nothing paged);
        the next tick captures the now-bound row and reverts it."""
        write_clients_yaml(client_cfg)
        monkeypatch.setattr("cw.reconcile.core._claude_agents_json", list)
        _arrange(client_cfg)
        checked: list[str] = []

        def _clean(_client: str, path: object) -> str | None:
            checked.append(str(path))
            return None

        monkeypatch.setattr(
            "cw.reconcile._shared.worktree_dirty_reason_by_path", _clean
        )

        with caplog.at_level(logging.WARNING, logger=DIRTY_CHECKS_LOGGER):
            reconcile()

        assert len(adopted) == 1
        row = _row()
        assert row.status is QueueItemStatus.RUNNING
        assert row.session_id == _SID
        session = next(s for s in load_state().sessions if s.id == _SID)
        assert session.status is SessionStatus.COMPLETED
        assert session.reap_reason is None
        assert _attention_events("m9", _TICKET) == []
        push = _deps.fire_push_notification
        assert isinstance(push, MagicMock)
        push.assert_not_called()
        assert len(unavailable_records(caplog, _SID)) == 1
        assert checked == []

        assert reconcile().reverted_ticket_ids == [_TICKET]

        assert _row().status is QueueItemStatus.PENDING
        assert len(checked) == 1


class TestAdoptClearsPartBState:
    def test_adopt_clears_launch_marker(self, client_cfg: ClientConfig) -> None:
        _arrange(client_cfg, launch_marker=live_launch_marker())

        assert _sweep(client_cfg) == [_TICKET]

        assert _row().launch_marker is None

    def test_adopt_clears_unconfirmed_note(self, client_cfg: ClientConfig) -> None:
        _arrange(client_cfg, advisory_note=unconfirmed_note(CAUSE_ROSTER_UNPROVEN))

        assert _sweep(client_cfg) == [_TICKET]

        assert _row().advisory_note is None

    def test_adopt_keeps_another_note(self, client_cfg: ClientConfig) -> None:
        _arrange(client_cfg, advisory_note="?no_session")

        assert _sweep(client_cfg) == [_TICKET]

        assert _row().advisory_note == "?no_session"

    def test_non_utf8_context_does_not_abort_the_sweep(
        self, client_cfg: ClientConfig, adopted: list[CapturedEvent]
    ) -> None:
        """A non-UTF-8 context once raised UnicodeDecodeError out of the tick."""
        context = _worktree(client_cfg, _OTHER_TICKET) / HOOK_CONTEXT_RELATIVE_PATH
        context.parent.mkdir(parents=True)
        context.write_bytes(b"\xff\xfe\x00")
        mk_unowned_running_row(
            client=_CLIENT, ticket_id=_OTHER_TICKET, attempts=1, claimed_at=_CLAIM
        )
        _arrange(client_cfg)

        assert _sweep(client_cfg, now=_OLD) == [_TICKET]

        assert _row(_OTHER_TICKET).session_id is None
        assert len(adopted) == 1


_Builder = Callable[..., None]


def _build_duplicate(client: ClientConfig, **row: object) -> None:
    _arrange(client, **row)
    mk_unowned_running_row(
        client=_CLIENT,
        ticket_id=_TICKET,
        attempts=1,
        claimed_at=row.get("claimed_at", _CLAIM),
        launch_marker=row.get("launch_marker"),
    )


def _build_bound_elsewhere(client: ClientConfig, **row: object) -> None:
    _arrange(client, **row)
    _edit_queue(
        lambda store: store.tasks.append(
            _make_ticket_task(
                ticket_id=_OTHER_TICKET,
                client=_CLIENT,
                status=QueueItemStatus.BLOCKED_ON_USER,
                session_id=_SID,
            )
        )
    )


def _build_conflicting(client: ClientConfig, **row: object) -> None:
    _arrange(
        client, session=_session(name=f"{_CLIENT}/auto-dev/{_OTHER_TICKET}"), **row
    )


def _build_stale(client: ClientConfig, **row: object) -> None:
    _arrange(client, session=_session(started_at=_CLAIM - timedelta(hours=1)), **row)


def _build_naive(client: ClientConfig, **row: object) -> None:
    _arrange(client, session=_session(started_at=_NAIVE_START), **row)


def _unbound(**row: object) -> None:
    fields: dict[str, object] = {"attempts": 1, "claimed_at": _CLAIM, **row}
    mk_unowned_running_row(client=_CLIENT, ticket_id=_TICKET, **fields)


def _build_unreadable(client: ClientConfig, **row: object) -> None:
    write_unreadable_claim_context(_worktree(client), "{not json")
    _unbound(**row)


def _build_missing(client: ClientConfig, **row: object) -> None:
    _worktree(client)
    _unbound(**row)


def _build_no_attempt(client: ClientConfig, **row: object) -> None:
    _write_hook_context_file(
        _worktree(client), session_id=_SID, ticket_id=_TICKET, client=_CLIENT
    )
    _unbound(**row)


def _build_attempt_mismatch(client: ClientConfig, **row: object) -> None:
    _claim_context(_worktree(client), attempts=1)
    _seed(_session())
    _unbound(**{**row, "attempts": 2})


def _build_other_claim(client: ClientConfig, **row: object) -> None:
    _claim_context(_worktree(client), attempts=1, ticket_id=_OTHER_TICKET)
    _unbound(**row)


def _build_session_absent(client: ClientConfig, **row: object) -> None:
    _claim_context(_worktree(client), attempts=1)
    _unbound(**row)


_EVIDENCE_BUILDERS: dict[str, _Builder] = {
    CAUSE_DUPLICATE: _build_duplicate,
    CAUSE_BOUND_ELSEWHERE: _build_bound_elsewhere,
    CAUSE_CONFLICTING_SESSION: _build_conflicting,
    CAUSE_STALE_SESSION: _build_stale,
    CAUSE_NAIVE_DATETIME: _build_naive,
    CAUSE_UNREADABLE_CONTEXT: _build_unreadable,
}
_NO_TIE_BUILDERS: dict[str, _Builder] = {
    CONTEXT_MISSING: _build_missing,
    CONTEXT_NO_ATTEMPT: _build_no_attempt,
    CONTEXT_ATTEMPT_MISMATCH: _build_attempt_mismatch,
    CONTEXT_OTHER_CLAIM: _build_other_claim,
    CONTEXT_SESSION_ABSENT: _build_session_absent,
}
_ALL_BUILDERS = {**_EVIDENCE_BUILDERS, **_NO_TIE_BUILDERS}


def _detect(
    client: ClientConfig,
    *,
    now: datetime = _OLD,
    daemon: FakeNativeDaemonClient | None = None,
    native_live: set[str] | None = None,
) -> UnownedFindings:
    return detect_unowned_running(
        load_dev_queue(),
        load_state(),
        {_CLIENT: client},
        now=now,
        daemon=daemon if daemon is not None else FakeNativeDaemonClient(),
        native_live=native_live or set(),
    )


def _causes(found: UnownedFindings) -> set[str]:
    return {s.cause for s in found.signals}


def _assert_silent(found: UnownedFindings) -> None:
    assert found.adopts == []
    assert found.no_owner == []
    assert found.signals == []


def _non_native_client(tmp_path: Path, *, lane_level: bool) -> ClientConfig:
    codex = StagePipelineConfig(
        executors={Stage.REVIEW: StageExecutorConfig(backend="codex")}
    )
    if lane_level:
        return ClientConfig(
            name=_CLIENT,
            workspace_path=tmp_path / "ws",
            worktree_base=tmp_path / "worktrees",
            lanes=[
                LaneConfig(name="default"),
                LaneConfig(name="fast", pipeline=codex),
            ],
        )
    return ClientConfig(
        name=_CLIENT,
        workspace_path=tmp_path / "ws",
        worktree_base=tmp_path / "worktrees",
        pipeline=codex,
    )


class TestDecisionTable:
    """Detect-level and pure: which outcome each verdict and evidence shape gets."""

    @pytest.mark.parametrize("shape", sorted(_ALL_BUILDERS))
    def test_in_flight_waits_for_every_cause(
        self, client_cfg: ClientConfig, shape: str
    ) -> None:
        _ALL_BUILDERS[shape](client_cfg, launch_marker=live_launch_marker())

        _assert_silent(_detect(client_cfg))

    @pytest.mark.parametrize("shape", sorted(_NO_TIE_BUILDERS))
    def test_unknown_never_releases(self, client_cfg: ClientConfig, shape: str) -> None:
        _NO_TIE_BUILDERS[shape](client_cfg)

        _assert_silent(_detect(client_cfg))

    @pytest.mark.parametrize("cause", sorted(_EVIDENCE_BUILDERS))
    def test_unknown_old_claim_signals_the_evidence_cause(
        self, client_cfg: ClientConfig, cause: str
    ) -> None:
        _EVIDENCE_BUILDERS[cause](client_cfg)

        found = _detect(client_cfg)

        assert found.no_owner == []
        assert found.adopts == []
        assert _causes(found) == {cause}

    @pytest.mark.parametrize("cause", sorted(_EVIDENCE_BUILDERS))
    def test_unknown_young_claim_is_silent(
        self, client_cfg: ClientConfig, cause: str
    ) -> None:
        _EVIDENCE_BUILDERS[cause](client_cfg)

        _assert_silent(_detect(client_cfg, now=_CLAIM + timedelta(seconds=5)))

    def test_unknown_naive_claim_is_silent(self, client_cfg: ClientConfig) -> None:
        _build_duplicate(client_cfg, claimed_at=_CLAIM.replace(tzinfo=None))

        _assert_silent(_detect(client_cfg))

    def test_unknown_claim_of_unknown_age_counts_as_old(
        self, client_cfg: ClientConfig
    ) -> None:
        _build_duplicate(client_cfg, claimed_at=None)

        found = _detect(client_cfg, now=_CLAIM)

        assert _causes(found) == {CAUSE_DUPLICATE}

    @pytest.mark.parametrize("cause", sorted(_EVIDENCE_BUILDERS))
    def test_owner_gone_signals_without_debounce(
        self, client_cfg: ClientConfig, cause: str
    ) -> None:
        _EVIDENCE_BUILDERS[cause](client_cfg, launch_marker=dead_launch_marker())

        found = _detect(client_cfg, now=_CLAIM + timedelta(seconds=1))

        assert found.no_owner == []
        assert _causes(found) == {cause}

    @pytest.mark.parametrize("state", sorted(_NO_TIE_BUILDERS))
    def test_owner_gone_no_tie_is_a_no_owner_candidate(
        self, client_cfg: ClientConfig, state: str
    ) -> None:
        _NO_TIE_BUILDERS[state](client_cfg, launch_marker=dead_launch_marker())

        found = _detect(client_cfg, now=_CLAIM + timedelta(seconds=1))

        assert found.signals == []
        assert [c.context_state for c in found.no_owner] == [state]
        candidate = found.no_owner[0]
        assert candidate.launch_marker == dead_launch_marker()
        assert candidate.homes_snapshot == frozenset()
        assert candidate.worktree == _worktree(client_cfg)

    def test_recycled_pid_counts_as_gone(self, client_cfg: ClientConfig) -> None:
        _build_missing(client_cfg, launch_marker=dead_launch_marker(recycled=True))

        assert len(_detect(client_cfg).no_owner) == 1

    def test_roster_unreadable_waits(self, client_cfg: ClientConfig) -> None:
        _build_missing(client_cfg, launch_marker=dead_launch_marker())
        daemon = FakeNativeDaemonClient()
        daemon.roster_unreadable = True

        _assert_silent(_detect(client_cfg, daemon=daemon))

    def test_recorded_live_session_homed_waits(self, client_cfg: ClientConfig) -> None:
        _build_missing(client_cfg, launch_marker=dead_launch_marker())
        _seed(
            _session(
                id="sess-live",
                status=SessionStatus.ACTIVE,
                worktree_path=_worktree(client_cfg),
            )
        )

        _assert_silent(_detect(client_cfg))

    def test_unrecorded_roster_worker_homed_waits(
        self, client_cfg: ClientConfig
    ) -> None:
        _build_missing(client_cfg, launch_marker=dead_launch_marker())
        daemon = FakeNativeDaemonClient()
        daemon.seed_live_worker(_worktree(client_cfg))

        _assert_silent(_detect(client_cfg, daemon=daemon))

    def test_unnormalizable_path_waits(
        self, client_cfg: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _build_missing(client_cfg, launch_marker=dead_launch_marker())

        def _loop(_path: Path) -> Path:
            msg = "symlink loop"
            raise OSError(msg)

        monkeypatch.setattr("cw.reconcile.unowned_running_acts._normalize_path", _loop)

        _assert_silent(_detect(client_cfg))

    @pytest.mark.parametrize("lane_level", [False, True], ids=["client", "lane"])
    def test_non_native_client_signals(self, tmp_path: Path, lane_level: bool) -> None:
        client = _non_native_client(tmp_path, lane_level=lane_level)
        _build_missing(client, launch_marker=dead_launch_marker())

        found = _detect(client)

        assert found.no_owner == []
        assert _causes(found) == {CAUSE_NON_NATIVE}
        assert "(codex)" in found.signals[0].evidence

    def test_roster_unproven_by_agents_listing(self, client_cfg: ClientConfig) -> None:
        _build_missing(client_cfg, launch_marker=dead_launch_marker())

        found = _detect(client_cfg, native_live={"0000abcd"})

        assert found.no_owner == []
        assert _causes(found) == {CAUSE_ROSTER_UNPROVEN}

    def test_roster_unproven_by_a_bound_live_row(
        self, client_cfg: ClientConfig
    ) -> None:
        _build_missing(client_cfg, launch_marker=dead_launch_marker())
        _seed(
            _session(
                id="sess-bound",
                name=f"{_CLIENT}/auto-dev/{_OTHER_TICKET}",
                status=SessionStatus.ACTIVE,
                surface_ref="0000abcd",
            )
        )
        mk_unowned_running_row(
            client=_CLIENT,
            ticket_id=_OTHER_TICKET,
            attempts=1,
            claimed_at=_CLAIM,
            session_id="sess-bound",
            ever_spawned=True,
        )

        found = _detect(client_cfg)

        assert found.no_owner == []
        assert _causes(found) == {CAUSE_ROSTER_UNPROVEN}

    def test_unreadable_context_directory(self, client_cfg: ClientConfig) -> None:
        (_worktree(client_cfg) / HOOK_CONTEXT_RELATIVE_PATH).mkdir(parents=True)
        _unbound(launch_marker=dead_launch_marker())

        found = _detect(client_cfg)

        assert found.no_owner == []
        assert _causes(found) == {CAUSE_UNREADABLE_CONTEXT}

    def test_unreadable_context_permission_error(
        self, client_cfg: ClientConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Monkeypatched, because ``chmod`` is a no-op when the suite runs as root."""
        _build_session_absent(client_cfg, launch_marker=dead_launch_marker())
        real_read_text = Path.read_text

        def _read_text(
            self: Path,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> str:
            if self.name == HOOK_CONTEXT_RELATIVE_PATH.name:
                raise PermissionError(self)
            return real_read_text(
                self, encoding=encoding, errors=errors, newline=newline
            )

        monkeypatch.setattr(Path, "read_text", _read_text)

        found = _detect(client_cfg)

        assert found.no_owner == []
        assert _causes(found) == {CAUSE_UNREADABLE_CONTEXT}

    def test_unreadable_context_non_utf8(self, client_cfg: ClientConfig) -> None:
        context = _worktree(client_cfg) / HOOK_CONTEXT_RELATIVE_PATH
        context.parent.mkdir(parents=True)
        context.write_bytes(b"\xff\xfe\x00")
        _unbound(launch_marker=dead_launch_marker())

        assert _causes(_detect(client_cfg)) == {CAUSE_UNREADABLE_CONTEXT}

    @pytest.mark.parametrize("shape", [CONTEXT_MISSING, CAUSE_DUPLICATE])
    def test_foreign_pid_namespace_honours_no_verdict(
        self, client_cfg: ClientConfig, monkeypatch: pytest.MonkeyPatch, shape: str
    ) -> None:
        _ALL_BUILDERS[shape](client_cfg, launch_marker=dead_launch_marker())
        monkeypatch.setattr(
            "cw.launch_marker.in_initial_pid_namespace", lambda **_kw: False
        )

        _assert_silent(_detect(client_cfg))

    def test_latched_cause_is_skipped(self, client_cfg: ClientConfig) -> None:
        _build_duplicate(
            client_cfg,
            launch_marker=dead_launch_marker(),
            advisory_note=unconfirmed_note(CAUSE_DUPLICATE),
        )

        found = _detect(client_cfg)

        # Only the second (unlatched) duplicate row is signalled.
        assert len(found.signals) == 1
        assert found.signals[0].created_at == load_dev_queue().tasks[1].created_at

    def test_other_cause_latch_does_not_skip(self, client_cfg: ClientConfig) -> None:
        _build_missing(
            client_cfg,
            launch_marker=dead_launch_marker(),
            advisory_note=unconfirmed_note(CAUSE_DUPLICATE),
        )

        found = _detect(client_cfg, native_live={"0000abcd"})

        assert _causes(found) == {CAUSE_ROSTER_UNPROVEN}

    def test_same_cause_latch_skips_a_changed_count(
        self, client_cfg: ClientConfig
    ) -> None:
        """R10: the latch keys on the cause, so a new live count stays silent."""
        _build_missing(
            client_cfg,
            launch_marker=dead_launch_marker(),
            advisory_note=unconfirmed_note(CAUSE_ROSTER_UNPROVEN),
        )

        _assert_silent(_detect(client_cfg, native_live={"0000abcd", "0000abce"}))

    def test_detect_writes_nothing(self, client_cfg: ClientConfig) -> None:
        _build_duplicate(client_cfg, launch_marker=dead_launch_marker())
        before = _state_queue_snapshot()

        _detect(client_cfg)

        assert _state_queue_snapshot() == before


def _events_section(heading: str) -> str:
    text = EVENTS_DOC.read_text(encoding="utf-8")
    start = text.index(f"### `{heading}`")
    end = text.find("\n### ", start + 1)
    return text[start : end if end != -1 else len(text)]


class TestEventDocumented:
    def test_session_adopted_section_lists_every_payload_key(self) -> None:
        section = _events_section(OrchestratorEventType.TASK_SESSION_ADOPTED.value)

        for key in (
            "client",
            "ticket_id",
            "lane",
            "session_id",
            "session_name",
            "attempt",
            "claimed_at",
        ):
            assert f"`{key}`" in section


class TestDocsNameValidCommands:
    """The rewritten ``spawn_post_launch_failed`` bullet names real commands."""

    @staticmethod
    def _bullet() -> str:
        return events_doc_bullet('- `"spawn_post_launch_failed"`')

    def test_bullet_names_cancel_then_requeue_from_cancelled(self) -> None:
        bullet = " ".join(self._bullet().split())

        assert "`task.session_adopted`" in bullet
        assert "`cw dev-queue cancel <ticket> -c <client>`" in bullet
        assert "`cw dev-queue requeue <ticket> -c <client> --from-cancelled`" in bullet
        assert not re.search(r"`cw dev-queue requeue <ticket> -c <client>`", bullet)

    @pytest.mark.parametrize("command", ["cancel", "requeue"])
    def test_named_commands_exist(self, command: str) -> None:
        result = CliRunner().invoke(main, ["dev-queue", command, "--help"])

        assert result.exit_code == 0, result.output
        if command == "requeue":
            assert "--from-cancelled" in result.output
