"""``cw dev-queue wait`` hand-offs, human gates, stuck-pending and ``--any`` (#2693).

The wait loop runs on a fake monotonic clock (``cw.cli.dev_queue.wait.time``
is patched); the ticket view's ``now`` is the real wall clock, so a dispatch
tick recorded during the test stays fresh. ``TestWaitState`` covers the pure
``_wait_state`` helpers.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from cw.cli.dev_queue._wait_state import (
    WAIT_PROBE_SECONDS,
    TicketWait,
    emit_wait_stuck,
    format_handoff_line,
    format_waiting_line,
)
from cw.dev_queue import FINALIZE_GATE_HELD_DISPOSITION
from cw.events import record_event
from cw.models import (
    DEFAULT_LANE,
    LaneConcurrencyOverride,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.models.ticket_view import DispatchSource, DispatchView
from cw.stuck_pending import PendingBlock, PendingTracker
from tests._reconcile_helpers import _fix_loop_pending_payload, _make_terminal_payload
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    invoke_dev_queue,
    make_blocked,
    make_task,
    seed_queue,
    write_lane_override,
)
from tests.conftest import _FakeClock

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

    from cw.models import TicketTask
    from cw.stuck_pending import PendingProbe
    from cw.ticket_view import ViewSources

_T2 = "ACM-2"
_RESUME = f"cw lane resume {ACME} {DEFAULT_LANE}"
_BREAKER_DETAIL = (
    "lane default circuit breaker tripped (spawn_errors); no auto-resume is scheduled"
)


@pytest.fixture(autouse=True)
def _acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_acme_client(tmp_path, monkeypatch, lanes=(DEFAULT_LANE,))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr("cw.cli.dev_queue.wait.time.sleep", fake.sleep)
    monkeypatch.setattr("cw.cli.dev_queue.wait.time.monotonic", fake.monotonic)
    return fake


def _wait(*args: str) -> Result:
    return invoke_dev_queue("wait", *args, "-c", ACME)


def _running_with(payload: dict[str, object], ticket: str = ACME_TICKET) -> None:
    task = make_task(
        ticket_id=ticket, status=QueueItemStatus.RUNNING, session_id="sess-1"
    )
    seed_queue([task], [bound_session_for(task, last_result=payload)])


def _stderr_lines(result: Result, prefix: str = "WAITING |") -> list[str]:
    return [ln for ln in result.stderr.splitlines() if ln.startswith(prefix)]


def _stuck_lane() -> None:
    write_lane_override(
        ACME,
        DEFAULT_LANE,
        override=LaneConcurrencyOverride(
            paused=True,
            pause_source="circuit_breaker",
            pause_trigger="spawn_errors",
            consecutive_spawn_errors=3,
            circuit_auto_resume_at=None,
        ),
    )


def _client_tick(code: str) -> None:
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": ACME,
            "claimed": 0,
            "pending": 1,
            "running": 0,
            "cap": 1,
            "skip_reason": code,
        },
    )


@pytest.mark.usefixtures("clock")
class TestHandoffs:
    def test_fix_loop_handoff_keeps_waiting(self) -> None:
        _running_with(_fix_loop_pending_payload())

        result = _wait(ACME_TICKET, "--timeout", "20")

        assert result.exit_code == 124, result.output
        assert _stderr_lines(result) == [
            f"WAITING | #{ACME_TICKET} | running: dispatch hand-off"
            " fix_loop_pending_dispatch (0s)"
        ]

    @pytest.mark.parametrize(
        "status", ["plan_pending_approval", "review_pending_approval"]
    )
    def test_scope_gated_handoff_exits_5_after_grace(self, status: str) -> None:
        _running_with(_make_terminal_payload(status, ACME_TICKET))

        result = _wait(ACME_TICKET, "--json")

        assert result.exit_code == 5, result.output
        doc = json.loads(result.stdout)
        assert doc["state"] == "stuck"
        assert doc["reason"] == "handoff_unrouted"
        assert doc["status"] == "running"
        assert doc["sentinel_status"] == status
        assert doc["session_id"] == "sess-1"
        assert doc["next_command"] is None
        assert doc["pr_url"] is None
        assert 300 <= doc["age_seconds"] < 300 + 5 + 1
        assert doc["detail"] == (
            f"dispatch has not routed the recorded {status} result for"
            f" {doc['age_seconds']}s; is `cw dev-queue run` alive?"
        )

    def test_handoff_text_mode(self) -> None:
        _running_with(_make_terminal_payload("review_pending_approval", ACME_TICKET))

        result = _wait(ACME_TICKET)

        assert result.exit_code == 5
        lines = result.stdout.splitlines()
        assert lines[0].startswith(
            f"Stuck: {ACME_TICKET} (running, 5m): handoff_unrouted - dispatch has"
        )
        assert not any(ln.startswith("Next:") for ln in lines)

    def test_handoff_on_a_routed_row_is_ignored(self) -> None:
        # Dispatch already moved the row off RUNNING: the stale sentinel on
        # the still-bound session neither exits 2 nor starts the grace clock.
        task = make_task(status=QueueItemStatus.PENDING, session_id="sess-1")
        payload = _make_terminal_payload("plan_pending_approval", ACME_TICKET)
        seed_queue([task], [bound_session_for(task, last_result=payload)])

        result = _wait(ACME_TICKET, "--timeout", "400")

        assert result.exit_code == 124


@pytest.mark.usefixtures("clock")
class TestGates:
    def test_settled_blocked_row_exits_2(self) -> None:
        seed_queue([make_blocked(stage=Stage.PLAN)])
        assert _wait(ACME_TICKET).exit_code == 2

    @pytest.mark.parametrize(
        "task",
        [
            make_blocked(
                stage=Stage.REVIEW, disposition=FINALIZE_GATE_HELD_DISPOSITION
            ),
            make_blocked(
                stage=Stage.REVIEW, scope_hint="large", disposition="approval_gate"
            ),
            make_task(
                status=QueueItemStatus.AWAITING_OPERATOR_SIGNOFF, stage=Stage.REVIEW
            ),
        ],
        ids=["finalize_hold", "scope_hint_large", "signoff"],
    )
    def test_human_kept_gate_exits_4(self, task: TicketTask) -> None:
        seed_queue([task])
        assert _wait(ACME_TICKET).exit_code == 4


class TestPending:
    def test_lane_cap_prints_one_throttled_waiting_line(
        self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cw.cli.dev_queue.wait as wait_mod

        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        _client_tick("lane_cap_blocked")
        probes: list[str] = []
        real = wait_mod.probe_pending

        def spy(
            ticket_id: str, client: str, *, now: datetime, sources: ViewSources
        ) -> PendingProbe | None:
            probes.append(ticket_id)
            return real(ticket_id, client, now=now, sources=sources)

        monkeypatch.setattr(wait_mod, "probe_pending", spy)

        result = _wait(ACME_TICKET, "--timeout", "100")

        assert result.exit_code == 124
        lines = _stderr_lines(result)
        assert len(lines) == 1
        assert lines[0].startswith(
            f"WAITING | #{ACME_TICKET} | pending: lane_cap_blocked (last_tick, "
        )
        assert lines[0].endswith("s ago)")
        assert len(probes) == 4  # t=0, 30, 60, 90

    def test_no_dispatch_signal_yet(self, clock: _FakeClock) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        result = _wait(ACME_TICKET, "--timeout", "10")
        assert _stderr_lines(result) == [
            f"WAITING | #{ACME_TICKET} | pending: no dispatch signal yet"
        ]

    def test_breaker_pause_exits_5_json(self, clock: _FakeClock) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        _stuck_lane()

        result = _wait(ACME_TICKET, "--json", "--stuck-after-minutes", "2")

        assert result.exit_code == 5, result.output
        doc = json.loads(result.stdout)
        assert doc == {
            "ticket_id": ACME_TICKET,
            "client": ACME,
            "status": "pending",
            "session_id": None,
            "state": "stuck",
            "sentinel_status": None,
            "pr_url": None,
            "reason": "lane_circuit_paused",
            "detail": _BREAKER_DETAIL,
            "age_seconds": doc["age_seconds"],
            "next_command": _RESUME,
        }
        assert 120 <= doc["age_seconds"] <= 120 + WAIT_PROBE_SECONDS
        assert clock.now - doc["age_seconds"] <= WAIT_PROBE_SECONDS
        assert _stderr_lines(result)[0].endswith(f"| {_BREAKER_DETAIL}")

    def test_leaving_pending_restarts_the_stuck_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        _stuck_lane()

        def flip(n: int) -> None:
            if n == 12:  # t=60: claimed
                seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id=None)])
            if n == 18:  # t=90: back to PENDING
                seed_queue([make_task(status=QueueItemStatus.PENDING)])

        fake = _FakeClock(on_sleep=flip)
        monkeypatch.setattr("cw.cli.dev_queue.wait.time.sleep", fake.sleep)
        monkeypatch.setattr("cw.cli.dev_queue.wait.time.monotonic", fake.monotonic)

        result = _wait(ACME_TICKET, "--stuck-after-minutes", "2", "--timeout", "200")

        assert result.exit_code == 124

    def test_breaker_pause_text_mode(self, clock: _FakeClock) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        _stuck_lane()

        result = _wait(ACME_TICKET, "--stuck-after-minutes", "1")

        assert result.exit_code == 5
        assert result.stdout.splitlines() == [
            f"Stuck: {ACME_TICKET} (pending, 1m): lane_circuit_paused"
            f" - {_BREAKER_DETAIL}",
            f"Next: {_RESUME}",
        ]

    def test_ssh_key_gate_has_no_next_command(self, clock: _FakeClock) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        _client_tick("ssh_key_gate")

        as_json = _wait(ACME_TICKET, "--json", "--stuck-after-minutes", "1")
        text = _wait(ACME_TICKET, "--stuck-after-minutes", "1")

        assert as_json.exit_code == 5
        assert json.loads(as_json.stdout)["next_command"] is None
        assert json.loads(as_json.stdout)["reason"] == "ssh_key_gate"
        assert text.exit_code == 5
        assert not any(ln.startswith("Next:") for ln in text.stdout.splitlines())

    def test_stuck_age_comes_from_the_monotonic_clock(self, clock: _FakeClock) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        _stuck_lane()

        result = _wait(ACME_TICKET, "--json")

        doc = json.loads(result.stdout)
        assert 900 <= doc["age_seconds"] <= 900 + WAIT_PROBE_SECONDS
        assert clock.now >= 900


@pytest.mark.usefixtures("clock")
class TestAny:
    @pytest.mark.parametrize(
        "args",
        [
            ("--any", ACME_TICKET, _T2),
            (ACME_TICKET, _T2, "--any"),
        ],
    )
    def test_first_ticket_to_exit_wins(self, args: tuple[str, ...]) -> None:
        first = make_task(status=QueueItemStatus.RUNNING, session_id=None)
        second = make_blocked(ticket_id=_T2, stage=Stage.PLAN, session_id=None)
        seed_queue([first, second])

        result = _wait(*args, "--json")

        assert result.exit_code == 2
        assert json.loads(result.stdout)["ticket_id"] == _T2

    def test_several_tickets_need_any(self) -> None:
        result = _wait(ACME_TICKET, _T2)
        assert result.exit_code == 2
        assert "Pass --any to wait on more than one ticket." in result.stderr

    def test_multi_ticket_timeout_message(self) -> None:
        seed_queue(
            [
                make_task(status=QueueItemStatus.RUNNING, session_id=None),
                make_task(
                    ticket_id=_T2, status=QueueItemStatus.RUNNING, session_id=None
                ),
            ]
        )

        text = _wait("--any", ACME_TICKET, _T2, "--timeout", "10")
        as_json = _wait("--any", ACME_TICKET, _T2, "--timeout", "10", "--json")

        assert text.exit_code == 124
        assert text.stdout.strip() == f"Timeout waiting for {ACME_TICKET}, {_T2} (>10s)"
        assert json.loads(as_json.stdout)["ticket_id"] == f"{ACME_TICKET},{_T2}"


class TestTimeout:
    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        captured: list[float] = []

        def fake_wait(ticket_id: str, client: str, *, timeout: float) -> object:
            captured.append(timeout)
            raise TimeoutError

        monkeypatch.setattr("cw.cli.dev_queue.wait.wait_for_terminal", fake_wait)
        return captured

    @pytest.mark.usefixtures("clock")
    def test_default_is_six_hours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([])
        captured = self._capture(monkeypatch)
        assert _wait(ACME_TICKET).exit_code == 124
        assert captured == [21600.0]

    @pytest.mark.usefixtures("clock")
    def test_zero_is_unbounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([])
        captured = self._capture(monkeypatch)
        _wait(ACME_TICKET, "--timeout", "0")
        assert captured == [float("inf")]

    def test_zero_never_hits_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id=None)])

        def stop(n: int) -> None:
            if n == 50:
                seed_queue([make_blocked(stage=Stage.PLAN, session_id=None)])

        fake = _FakeClock(on_sleep=stop)
        fake.now = 10.0**9
        monkeypatch.setattr("cw.cli.dev_queue.wait.time.sleep", fake.sleep)
        monkeypatch.setattr("cw.cli.dev_queue.wait.time.monotonic", fake.monotonic)

        assert _wait(ACME_TICKET, "--timeout", "0").exit_code == 2

    def test_help_strings(self) -> None:
        text = " ".join(invoke_dev_queue("wait", "--help").stdout.split())
        for expected in (
            "Seconds to wait (default: 21600 = 6 hours; 0 = no limit).",
            "With several TICKET_IDs, return as soon as the first one reaches an"
            " exit state. Without it, exactly one ticket is allowed.",
            "Exit 5 when a pending ticket's blocker will not clear on its own after"
            " this many minutes (default: 15).",
        ):
            assert expected in text, expected


_NOW = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)


def _dispatch(
    skip_reason: str | None,
    source: DispatchSource,
    as_of: datetime | None = None,
) -> DispatchView:
    return DispatchView(
        skip_reason=skip_reason,
        source=source,
        as_of=as_of,
        stale=False,
        backoff_until=None,
        lane_paused=False,
        lane_stats=None,
        lane_occupants=None,
    )


class TestWaitState:
    def test_probe_is_throttled(self) -> None:
        wait = TicketWait(ACME_TICKET, ACME, PendingTracker(60))
        assert wait.probe_due(0.0)
        assert not wait.probe_due(WAIT_PROBE_SECONDS - 1)
        assert wait.probe_due(WAIT_PROBE_SECONDS)

    def test_progress_changes_once_per_key(self) -> None:
        wait = TicketWait(ACME_TICKET, ACME, PendingTracker(60))
        assert wait.progress_changed("a")
        assert not wait.progress_changed("a")
        assert wait.progress_changed("b")

    def test_waiting_line_event_source_age(self) -> None:
        line, key = format_waiting_line(
            ACME_TICKET,
            _dispatch("cap_full", DispatchSource.EVENT, _NOW - timedelta(seconds=42)),
            None,
            now=_NOW,
        )
        assert line == f"WAITING | #{ACME_TICKET} | pending: cap_full (event, 42s ago)"
        assert key == "cap_full|event"

    def test_waiting_line_row_source_with_block(self) -> None:
        block = PendingBlock(code="lane_paused", detail="d", command=None)
        line, _key = format_waiting_line(
            ACME_TICKET, _dispatch(None, DispatchSource.NONE), block, now=_NOW
        )
        assert (
            line == f"WAITING | #{ACME_TICKET} | pending: lane_paused (row, 0s ago) | d"
        )

    def test_waiting_line_without_a_view(self) -> None:
        line, key = format_waiting_line(ACME_TICKET, None, None, now=_NOW)
        assert line == f"WAITING | #{ACME_TICKET} | pending: no dispatch signal yet"
        assert key == "-"

    def test_handoff_line(self) -> None:
        assert format_handoff_line("T", "review_pending_approval", 12.9) == (
            "WAITING | #T | running: dispatch hand-off review_pending_approval (12s)"
        )

    def test_emit_wait_stuck_json_and_text(self) -> None:
        def emit(output_json: bool) -> str:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                emit_wait_stuck(
                    ticket_id="T",
                    client=ACME,
                    status="pending",
                    session_id=None,
                    block=PendingBlock(
                        code="ssh_key_gate", detail="no key", command=None
                    ),
                    age_seconds=61.7,
                    sentinel_status=None,
                    output_json=output_json,
                )
            return buffer.getvalue()

        assert json.loads(emit(output_json=True))["age_seconds"] == 61
        assert (
            emit(output_json=False) == "Stuck: T (pending, 1m): ssh_key_gate - no key\n"
        )
