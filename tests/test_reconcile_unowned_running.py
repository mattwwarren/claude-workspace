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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cw.config import load_state, save_state
from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    ClientConfig,
    CwState,
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    Session,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
    UsageLimitAct,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.reconcile import reconcile
from cw.reconcile.unowned_running import (
    UnownedCandidate,
    detect_unowned_running,
    run_unowned_running_recovery,
)
from cw.worktree import worktree_path_for
from tests._clients_yaml import write_clients_yaml
from tests._reconcile_helpers import (
    _failing_record_event,
    _state_queue_snapshot,
    _write_agent_spawn_stamp,
    mk_unowned_running_row,
    write_unreadable_claim_context,
)
from tests.conftest import (
    CapturedEvent,
    _make_daemon_session,
    _make_ticket_task,
    _write_hook_context_file,
)

_CLIENT = "client-a"
_TICKET = "GEN-2591"
_OTHER_TICKET = "GEN-2592"
_SID = "sess-2591"
_CLAIM = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
# A naive timestamp, as a legacy or hand-edited state file can carry.
_NAIVE_START = (_CLAIM + timedelta(hours=1)).replace(tzinfo=None)
_MODULE = "cw.reconcile.unowned_running"


@pytest.fixture
def client_cfg(tmp_path: Path) -> ClientConfig:
    return ClientConfig(
        name=_CLIENT,
        workspace_path=tmp_path / "ws",
        worktree_base=tmp_path / "worktrees",
    )


@pytest.fixture
def adopted(
    capture_events: Callable[..., list[CapturedEvent]],
) -> list[CapturedEvent]:
    return capture_events(_MODULE, OrchestratorEventType.TASK_SESSION_ADOPTED)


def _worktree(client: ClientConfig, ticket_id: str = _TICKET) -> Path:
    wt = worktree_path_for(client, f"{client.feature_branch_prefix}/{ticket_id}")
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _claim_context(
    wt: Path,
    *,
    attempts: int,
    session_id: str = _SID,
    ticket_id: str = _TICKET,
    client: str = _CLIENT,
) -> None:
    """The context a dispatch spawn writes for a claim, through the real writer."""
    _write_hook_context_file(
        wt,
        session_id=session_id,
        ticket_id=ticket_id,
        client=client,
        task=_make_ticket_task(ticket_id=ticket_id, client=client, attempts=attempts),
    )


def _session(**overrides: object) -> Session:
    """The claim's recorded session: a task-spawned name, started after the claim."""
    fields: dict[str, object] = {
        "id": _SID,
        "name": f"{_CLIENT}/auto-dev/{_TICKET}",
        "client": _CLIENT,
        "status": SessionStatus.COMPLETED,
        "surface_ref": None,
        "started_at": _CLAIM + timedelta(seconds=5),
    }
    fields.update(overrides)
    return _make_daemon_session(**fields)


def _seed(*sessions: Session) -> None:
    state = load_state()
    state.sessions.extend(sessions)
    save_state(state)


def _arrange(
    client: ClientConfig,
    *,
    attempts: int = 1,
    claimed_at: datetime | None = _CLAIM,
    session: Session | None = None,
    **row_overrides: object,
) -> TicketTask:
    """A claim whose stamp failed: context written, session recorded, row unbound."""
    _claim_context(_worktree(client), attempts=attempts)
    _seed(session if session is not None else _session())
    return mk_unowned_running_row(
        client=_CLIENT,
        ticket_id=_TICKET,
        attempts=attempts,
        claimed_at=claimed_at,
        **row_overrides,
    )


def _sweep(client: ClientConfig) -> list[str]:
    return run_unowned_running_recovery(clients={_CLIENT: client})


def _row(ticket_id: str = _TICKET) -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == ticket_id)


def _wrap_detect(monkeypatch: pytest.MonkeyPatch, between: Callable[[], None]) -> None:
    """Run *between* after the real detect returned and before the act."""
    import cw.reconcile.unowned_running as mod

    real = mod.detect_unowned_running

    def _detect(
        store: DevQueueStore, state: CwState, clients: dict[str, ClientConfig]
    ) -> list[UnownedCandidate]:
        found = real(store, state, clients)
        between()
        return found

    monkeypatch.setattr(mod, "detect_unowned_running", _detect)


def _edit_queue(mutate: Callable[[DevQueueStore], None]) -> None:
    store = load_dev_queue()
    mutate(store)
    save_dev_queue(store)


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

        assert run_unowned_running_recovery(clients=scoped) == []

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
            load_dev_queue(), load_state(), {_CLIENT: client_cfg}
        )

        assert _state_queue_snapshot() == before
        assert found == [
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
        from cw.reconcile import core

        real_revert = core.revert_timed_out_tasks
        ran: list[str] = []

        def _record() -> list[str]:
            ran.append("revert_timed_out_tasks")
            return real_revert()

        monkeypatch.setattr(core, "revert_timed_out_tasks", _record)

        reconcile()

        assert ran == ["revert_timed_out_tasks"]
        assert _row().session_id is None
