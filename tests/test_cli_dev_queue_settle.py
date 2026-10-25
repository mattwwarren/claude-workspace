"""``cw.cli.dev_queue._settle``: the row-lag settle approve/requeue run first (#2692).

The fake consume replaces ``_consume_pending_bounded`` (the one place the
bounded dev-queue lock is taken); time is patched on the module so the
bounded retry runs instantly. One real-seam test drives the actual
``cw.dispatch.loop`` read/apply halves end to end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import cw.cli.dev_queue._settle as settle_mod
from cw.auto_dev_result import INTERMEDIATE_ADVANCE_STATUSES
from cw.cli.dev_queue._settle import SettleOutcome, settle_lagging_row
from cw.dev_queue import (
    FINALIZE_GATE_HELD_DISPOSITION,
    load_dev_queue,
    save_dev_queue,
)
from cw.dev_queue.next_commands import human_gate_settled_notice
from cw.events import load_cursor, read_events, record_event
from cw.exceptions import LockTimeoutError
from cw.models import OrchestratorEventType, QueueItemStatus, Stage
from tests._cli_review_helpers import _enter_operator_machine
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    invoke_dev_queue,
    make_blocked,
    make_task,
    seed_queue,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import TicketTask

_SID = "sess-run"
_REVIEW_PENDING = {"status": "review_pending_approval"}


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch)
    _enter_operator_machine(monkeypatch, tmp_path)


class _Clock:
    """A monotonic clock that advances only when the settle loop sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(settle_mod.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(settle_mod.time, "sleep", fake.sleep)
    return fake


def _seed_running(
    last_result: dict[str, object] | None = _REVIEW_PENDING,
    **overrides: object,
) -> TicketTask:
    task = make_task(
        status=QueueItemStatus.RUNNING,
        stage=Stage.REVIEW,
        session_id=_SID,
        **overrides,
    )
    seed_queue([task], [bound_session_for(task, last_result=last_result)])
    return task


def _route_to(**fields: object) -> Callable[[str], bool]:
    """A fake consume that rewrites the RUNNING row with *fields*."""

    def _consume(_session_id: str) -> bool:
        store = load_dev_queue()
        row = store.tasks[0]
        for name, value in fields.items():
            setattr(row, name, value)
        save_dev_queue(store)
        return True

    return _consume


def _patch_consume(
    monkeypatch: pytest.MonkeyPatch, fake: Callable[[str], bool]
) -> list[str]:
    calls: list[str] = []

    def _spy(session_id: str) -> bool:
        calls.append(session_id)
        return fake(session_id)

    monkeypatch.setattr(settle_mod, "_consume_pending_bounded", _spy)
    return calls


class TestSettleLaggingRow:
    def test_routes_a_recorded_park(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        clock: _Clock,
    ) -> None:
        _seed_running()
        calls = _patch_consume(
            monkeypatch, _route_to(status=QueueItemStatus.BLOCKED_ON_USER)
        )
        outcome = settle_lagging_row(ACME_TICKET, ACME)
        assert outcome.routed is True
        assert outcome.row is not None
        assert outcome.row.status == QueueItemStatus.BLOCKED_ON_USER
        assert calls == [_SID]
        assert clock.sleeps == []
        assert capsys.readouterr().err.strip() == (
            f"row ACM-1 was RUNNING; session {_SID} had recorded"
            " review_pending_approval; routed -> blocked_on_user"
        )

    def test_consumed_but_still_running_stops_at_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        clock: _Clock,
    ) -> None:
        _seed_running()
        calls = _patch_consume(monkeypatch, lambda _sid: True)
        outcome = settle_lagging_row(ACME_TICKET, ACME)
        assert outcome.routed is False
        assert calls == [_SID]
        assert clock.sleeps == []
        assert capsys.readouterr().err.strip() == (
            f"session {_SID} recorded review_pending_approval but routing left the"
            " row RUNNING (stage mismatch?); see: cw dev-queue show ACM-1 -c acme"
        )

    def test_times_out_after_the_bounded_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        clock: _Clock,
    ) -> None:
        _seed_running()
        calls = _patch_consume(monkeypatch, lambda _sid: False)
        outcome = settle_lagging_row(ACME_TICKET, ACME)
        assert outcome.routed is False
        assert outcome.row is not None
        assert outcome.row.status == QueueItemStatus.RUNNING
        assert len(calls) == 11
        assert clock.sleeps == [1.0] * 10
        err = capsys.readouterr().err.strip()
        assert err == (
            f"session {_SID} recorded review_pending_approval but its completion"
            " was not routed within 10s (the worker may not have finished; the"
            " Stop hook emits completion); try: cw dev-queue wait ACM-1 -c acme"
        )
        assert "dispatch loop" not in err

    def test_lock_timeout_falls_through(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        clock: _Clock,
    ) -> None:
        _seed_running()

        def _locked(_sid: str) -> bool:
            msg = "dev-queue lock held"
            raise LockTimeoutError(
                msg, lock_name="dev_queue", lock_path=tmp_path / "x.lock", waited_s=1.0
            )

        _patch_consume(monkeypatch, _locked)
        outcome = settle_lagging_row(ACME_TICKET, ACME)
        assert outcome.routed is False
        assert outcome.row is not None
        assert outcome.row.status == QueueItemStatus.RUNNING
        assert capsys.readouterr().err.strip() == (
            f"could not take the dev-queue lock to route session {_SID}'s recorded"
            " review_pending_approval (dev-queue lock held); continuing with the"
            " row as it stands"
        )
        assert clock.sleeps == []

    @pytest.mark.parametrize(
        "seed",
        [
            pytest.param(lambda: _seed_running(last_result=None), id="no_result"),
            pytest.param(
                lambda: _seed_running(last_result={"status": "stage_complete"}),
                id="stage_complete",
            ),
            pytest.param(
                lambda: _seed_running(last_result={"status": ""}), id="empty_status"
            ),
            pytest.param(
                lambda: seed_queue([make_blocked(stage=Stage.REVIEW)]),
                id="not_running",
            ),
            pytest.param(
                lambda: seed_queue(
                    [make_task(status=QueueItemStatus.RUNNING, session_id="gone")]
                ),
                id="session_gone",
            ),
            pytest.param(lambda: seed_queue([]), id="missing_row"),
        ],
    )
    def test_no_settle_needed(
        self,
        seed: Callable[[], object],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        seed()
        calls = _patch_consume(monkeypatch, lambda _sid: True)
        outcome = settle_lagging_row(ACME_TICKET, ACME)
        assert outcome.routed is False
        assert calls == []
        assert capsys.readouterr().err == ""

    def test_missing_row_is_none(self) -> None:
        seed_queue([])
        assert settle_lagging_row(ACME_TICKET, ACME) == SettleOutcome(None, False)

    def test_unreadable_queue_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail() -> object:
            msg = "disk"
            raise OSError(msg)

        monkeypatch.setattr(settle_mod, "load_dev_queue", _fail)
        assert settle_lagging_row(ACME_TICKET, ACME) == SettleOutcome(None, False)

    def test_unreadable_state_is_no_settle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_running()

        def _fail() -> object:
            msg = "bad state"
            raise ValueError(msg)

        monkeypatch.setattr(settle_mod, "load_state", _fail)
        outcome = settle_lagging_row(ACME_TICKET, ACME)
        assert outcome.routed is False
        assert outcome.row is not None

    def test_row_vanishing_mid_settle_is_none(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        _seed_running()

        def _remove(_sid: str) -> bool:
            seed_queue([])
            return True

        _patch_consume(monkeypatch, _remove)
        assert settle_lagging_row(ACME_TICKET, ACME) == SettleOutcome(None, False)

    def test_exclusion_parity_with_wait(self) -> None:
        assert "stage_complete" in INTERMEDIATE_ADVANCE_STATUSES
        assert "shipped" not in INTERMEDIATE_ADVANCE_STATUSES
        assert "review_pending_approval" not in INTERMEDIATE_ADVANCE_STATUSES


class TestRealSeam:
    def test_routes_requested_completion_without_consuming_pending_prefix(
        self, tmp_path: Path
    ) -> None:
        """No patching: settle does not consume unrelated earlier events."""
        mine = make_task(
            status=QueueItemStatus.RUNNING, stage=Stage.REVIEW, session_id=_SID
        )
        other = make_task(
            ticket_id="ACM-2",
            status=QueueItemStatus.RUNNING,
            stage=Stage.REVIEW,
            session_id="sess-other",
        )
        seed_queue(
            [mine, other],
            [
                bound_session_for(mine, last_result=_REVIEW_PENDING),
                bound_session_for(other, last_result=_REVIEW_PENDING),
            ],
        )
        for task in (other, mine):
            record_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "ticket_id": task.ticket_id,
                    "session_id": task.session_id,
                    "client": ACME,
                },
            )
        outcome = settle_lagging_row(ACME_TICKET, ACME)
        assert outcome.routed is True
        assert outcome.row is not None
        assert outcome.row.status == QueueItemStatus.BLOCKED_ON_USER
        statuses = {t.ticket_id: t.status for t in load_dev_queue().tasks}
        assert statuses == {
            "ACM-1": QueueItemStatus.BLOCKED_ON_USER,
            "ACM-2": QueueItemStatus.RUNNING,
        }
        assert load_cursor("dispatch") is None

    def test_no_pending_event_for_this_session_takes_no_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_running()

        def _boom(*_a: object, **_kw: object) -> object:
            msg = "lock taken without a pending event"
            raise AssertionError(msg)

        monkeypatch.setattr(settle_mod, "dev_queue_lock", _boom)
        assert settle_mod._consume_pending_bounded(_SID) is False


_HUMAN_GATES: dict[str, dict[str, object]] = {
    "signoff_status": {"status": QueueItemStatus.AWAITING_OPERATOR_SIGNOFF},
    "finalize_gate_held": {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": FINALIZE_GATE_HELD_DISPOSITION,
    },
    "scope_hint_large_approval_gate": {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": "approval_gate",
    },
    "scope_hint_large_plan_pending": {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "disposition": "plan_pending_approval",
        "stage": Stage.PLAN,
    },
}


def _seed_for(case_id: str) -> None:
    overrides: dict[str, object] = {}
    if case_id.startswith("scope_hint_large"):
        overrides["scope_hint"] = "large"
    _seed_running(**overrides)


def _spy_approve(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _spy(*args: object, **_kw: object) -> object:
        calls.append(str(args[0]))
        msg = "approve_ticket must not run"
        raise AssertionError(msg)

    monkeypatch.setattr("cw.cli.dev_queue.approve.approve_ticket", _spy)
    return calls


class TestSettleThenApproveStop:
    """R8: approve stops after a settle that routed the row into a human gate."""

    @pytest.mark.parametrize("case_id", sorted(_HUMAN_GATES))
    def test_approve_stops_when_settle_routed_into_a_human_gate(
        self, case_id: str, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        _seed_for(case_id)
        _patch_consume(monkeypatch, _route_to(**_HUMAN_GATES[case_id]))
        approve_calls = _spy_approve(monkeypatch)
        result = invoke_dev_queue("approve", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 1, result.output
        row = load_dev_queue().tasks[0]
        assert f"Error: {human_gate_settled_notice(row)}" in result.stderr
        assert f"routed -> {row.status.value}" in result.stderr
        assert approve_calls == []
        assert row.plan_approved_at is None
        for name, value in _HUMAN_GATES[case_id].items():
            assert getattr(row, name) == value
        approved = read_events(event_types=[OrchestratorEventType.TICKET_APPROVED])
        assert approved == []

    def test_ordinary_gate_is_released_in_the_same_call(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        _seed_running()
        _patch_consume(monkeypatch, _route_to(status=QueueItemStatus.BLOCKED_ON_USER))
        result = invoke_dev_queue("approve", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 0, result.output
        assert "Approved ACM-1 (acme): review -> finalize" in result.stdout

    def test_already_parked_human_gate_releases_without_settle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = make_blocked(
            stage=Stage.REVIEW, disposition=FINALIZE_GATE_HELD_DISPOSITION
        )
        seed_queue([task], [bound_session_for(task)])
        calls = _patch_consume(monkeypatch, lambda _sid: True)
        result = invoke_dev_queue("approve", ACME_TICKET, "-c", ACME)
        assert result.exit_code == 0, result.output
        assert calls == []
        assert load_dev_queue().tasks[0].stage == Stage.FINALIZE

    @pytest.mark.parametrize(
        "args",
        [
            ("--scope-drift", "a.py"),
            ("--override-must-fix", "--reason", "x"),
        ],
        ids=["scope_drift", "override_must_fix"],
    )
    def test_other_branches_settle_but_refuse_with_their_own_text(
        self, args: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        _seed_running()
        calls = _patch_consume(
            monkeypatch,
            _route_to(
                status=QueueItemStatus.BLOCKED_ON_USER,
                disposition=FINALIZE_GATE_HELD_DISPOSITION,
            ),
        )
        result = invoke_dev_queue("approve", ACME_TICKET, "-c", ACME, *args)
        assert result.exit_code == 1
        assert calls == [_SID]
        assert "just routed" not in result.stderr
        assert "Cannot" in result.stderr


class TestLiveSessionNotes:
    def test_roster_read_error_yields_no_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cw.native_daemon import FakeNativeDaemonClient

        fake = FakeNativeDaemonClient()

        def _fail() -> set[str] | None:
            msg = "roster gone"
            raise OSError(msg)

        monkeypatch.setattr(fake, "list_live_session_short_ids_fail_closed", _fail)
        assert settle_mod.live_session_notes(ACME_TICKET, ACME, fake) == []

    def test_no_live_session_yields_no_note(self) -> None:
        from cw.native_daemon import FakeNativeDaemonClient

        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        assert (
            settle_mod.live_session_notes(ACME_TICKET, ACME, FakeNativeDaemonClient())
            == []
        )
