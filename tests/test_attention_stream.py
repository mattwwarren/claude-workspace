"""Tests for ``cw.attention_stream``: the persistent, scoped attention stream (#2693).

A real inbox (``record_event`` under ``tmp_events_dir``), a fake clock for
``sleep``/``monotonic`` and a fixed ``now``. A run ends when the fake sleep
raises ``KeyboardInterrupt`` (the SIGINT path), so each test reads the lines
printed before the ``WATCH | exited: signal SIGINT`` line.
"""

from __future__ import annotations

import io
import os
import signal
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from cw.attention_stream import (
    EXIT_ERROR,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    AttentionStream,
)
from cw.attention_watch import ScopeSource, WatchScope, first_next_command
from cw.codex_review import CODEX_MUST_FIX_FINDINGS
from cw.config import orchestrator_config_file
from cw.dev_queue import FINALIZE_GATE_HELD_DISPOSITION
from cw.events import prune_events, record_event
from cw.events_follow import load_cursor_record, save_cursor_record
from cw.models import (
    DEFAULT_LANE,
    LaneConcurrencyOverride,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)
from cw.models.enums import PipelinePosition
from cw.models.ticket_view import NextCommand
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    CASES,
    bound_session_for,
    install_acme_client,
    make_blocked,
    make_override,
    make_task,
    seed_queue,
    write_lane_client,
    write_lane_override,
)
from tests.conftest import _FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import Session, TicketTask

_TERMINAL = PipelinePosition.TERMINAL
_OFF = PipelinePosition.OFF_PIPELINE

_NOW = datetime(2026, 10, 18, 14, 3, 0, tzinfo=UTC)
_NA = OrchestratorEventType.SESSION_NEEDS_ATTENTION
_PID = 4242
_FAST = "fast"
_SIGINT_LINE = "WATCH | exited: signal SIGINT"


@pytest.fixture(autouse=True)
def _acme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_events_dir: Path
) -> None:
    install_acme_client(tmp_path, monkeypatch, lanes=(DEFAULT_LANE, _FAST))


def _scope(
    *, lanes: frozenset[str] = frozenset(), tickets: frozenset[str] = frozenset()
) -> WatchScope:
    return WatchScope(
        client=ACME,
        lanes=lanes,
        tickets=tickets,
        all_lanes=False,
        source=ScopeSource.FLAGS if lanes or tickets else ScopeSource.CLIENT,
    )


def _page(ticket: str = ACME_TICKET, **payload: object) -> OrchestratorEvent:
    return record_event(
        _NA,
        {"client": ACME, "ticket_id": ticket, "paused_status": "blocked", **payload},
    )


class _Run:
    """One stream run's output, exit code and the clock it ran on."""

    def __init__(self, out: io.StringIO, code: int, clock: _FakeClock) -> None:
        self.lines = out.getvalue().splitlines()
        self.code = code
        self.clock = clock

    @property
    def body(self) -> list[str]:
        """Lines between the scope line and the exit line."""
        return [ln for ln in self.lines[1:] if not ln.startswith("WATCH | exited:")]


def _run(
    stream: AttentionStream,
    *,
    sleeps: int = 1,
    on_sleep: Callable[[int], None] | None = None,
    out: io.StringIO | None = None,
    err: io.StringIO | None = None,
) -> _Run:
    """Run *stream* until its *sleeps*-th sleep, calling *on_sleep* before that."""

    def tick(n: int) -> None:
        if on_sleep is not None:
            on_sleep(n)
        if n >= sleeps:
            raise KeyboardInterrupt

    clock = _FakeClock(on_sleep=tick)
    stream_out = out if out is not None else io.StringIO()
    code = stream.run(
        stream_out,
        err=err if err is not None else io.StringIO(),
        sleep=clock.sleep,
        clock=clock.monotonic,
        now_fn=lambda: _NOW,
    )
    return _Run(stream_out, code, clock)


def _stream(
    scope: WatchScope | None = None,
    *,
    stuck_after_minutes: int = 15,
    since: str | None = None,
    force: bool = False,
) -> AttentionStream:
    return AttentionStream(
        scope or _scope(),
        stuck_after_minutes=stuck_after_minutes,
        since=since,
        force=force,
        pid=_PID,
    )


def _arm(scope: WatchScope | None = None) -> None:
    """A first arm over a non-empty inbox, so later runs resume from a cursor."""
    record_event(OrchestratorEventType.PR_REGISTERED, {"client": ACME})
    _run(_stream(scope))


def _record_of(scope: WatchScope) -> dict[str, object]:
    record = load_cursor_record(scope.consumer)
    assert record is not None
    return record


def _extra(scope: WatchScope) -> dict[str, object]:
    extra = _record_of(scope)["extra"]
    assert isinstance(extra, dict)
    return extra


class TestStart:
    def test_first_arm_starts_at_now(self) -> None:
        seeded = [_page(f"T-{n}") for n in range(3)]

        first = _run(_stream())

        assert first.body == []
        assert _record_of(_scope())["cursor"] == seeded[-1].id
        assert first.lines[0].endswith(f"| start=now | cursor={_scope().consumer}")

        later: list[OrchestratorEvent] = []
        second = _run(
            _stream(), sleeps=2, on_sleep=lambda _n: later.append(_page("T-new"))
        )
        assert len(second.body) == 1
        assert "#T-new" in second.body[0]
        assert second.lines[0].endswith(f"| start=resume | cursor={_scope().consumer}")

    def test_scope_line_is_first_and_since_mode(self) -> None:
        first = _page("T-1")
        _page("T-2")

        run = _run(_stream(since=first.id))

        assert run.lines[0] == (
            f"WATCH | scope | client={ACME} | lanes=* | tickets=- | source=client"
            f" | start=since | cursor={_scope().consumer}"
        )
        assert len(run.body) == 1
        assert "#T-2" in run.body[0]
        assert run.lines[-1] == _SIGINT_LINE
        assert run.code == EXIT_SIGINT

    def test_empty_inbox_first_arm_does_not_replay_after_restart(self) -> None:
        _run(_stream())
        assert _record_of(_scope())["cursor"] is None
        assert _extra(_scope())["armed_empty"] is True

        _page("T-gap-1")
        gap = _page("T-gap-2")
        rearm = _run(_stream())

        assert rearm.body == []
        assert "| start=now |" in rearm.lines[0]
        assert _record_of(_scope())["cursor"] == gap.id
        assert _extra(_scope())["armed_empty"] is False

    def test_empty_inbox_first_append_is_delivered(self) -> None:
        run = _run(_stream(), sleeps=2, on_sleep=lambda _n: _page("T-first"))
        assert len(run.body) == 1
        assert "#T-first" in run.body[0]


class TestResume:
    def test_restart_delivers_only_newer_events(self) -> None:
        _page("T-old")
        _arm()
        newer = _page("T-newer")

        run = _run(_stream())

        assert len(run.body) == 1
        assert "#T-newer" in run.body[0]
        assert _record_of(_scope())["cursor"] == newer.id

    def test_no_replay_across_restarts(self) -> None:
        _arm()
        _page("T-1")
        assert len(_run(_stream()).body) == 1
        assert _run(_stream()).body == []

    def test_filtered_batch_still_persists_cursor_and_latch(self) -> None:
        _arm()
        record_event(
            OrchestratorEventType.TICKET_REQUEUED,
            {"client": ACME, "ticket_id": ACME_TICKET},
        )
        other = _page("T-9", client="other")

        run = _run(_stream())

        assert run.body == []
        assert _record_of(_scope())["cursor"] == other.id

    def test_clear_only_batch_persists_cursor(self) -> None:
        _arm()
        tick = record_event(
            OrchestratorEventType.DISPATCH_TICK, {"client": ACME, "claimed": 0}
        )

        run = _run(_stream())

        assert run.body == []
        assert _record_of(_scope())["cursor"] == tick.id

    def test_latch_survives_a_restart(self) -> None:
        _arm()
        _page()
        assert len(_run(_stream()).body) == 1
        _page()
        assert _run(_stream()).body == []
        assert _extra(_scope())["latch"]


class _FailingStdout(io.StringIO):
    """A text stream whose writes start raising ``BrokenPipeError`` after *ok*."""

    def __init__(self, ok: int) -> None:
        super().__init__()
        self.ok = ok

    def write(self, s: str) -> int:
        if self.ok <= 0:
            raise BrokenPipeError
        self.ok -= 1
        return super().write(s)


class _DeadStdout(io.StringIO):
    """A text stream whose ``flush`` always raises ``BrokenPipeError``."""

    def flush(self) -> None:
        raise BrokenPipeError


class TestWriteFailure:
    @pytest.mark.parametrize("dead", [_DeadStdout, lambda: _FailingStdout(ok=0)])
    def test_write_failure_exits_before_cursor_advance(
        self, dead: Callable[[], io.StringIO]
    ) -> None:
        _arm()
        _page()
        before = _record_of(_scope())
        err = io.StringIO()

        run = _run(_stream(), out=dead(), err=err)

        assert run.code == EXIT_ERROR
        after = _record_of(_scope())
        assert after["cursor"] == before["cursor"]
        assert _extra(_scope())["latch"] == {}
        assert err.getvalue().strip() == (
            "WATCH | exited: stdout write failed (BrokenPipeError); cursor not advanced"
        )
        healthy = _run(_stream())
        assert len(healthy.body) == 1

    def test_failure_on_second_line_keeps_pre_batch_cursor(self) -> None:
        _arm()
        before = _record_of(_scope())["cursor"]
        _page("T-1")
        _page("T-2")

        # The scope line and the first event line are written; the second fails.
        run = _run(_stream(), out=_FailingStdout(ok=2))

        assert run.code == EXIT_ERROR
        assert _record_of(_scope())["cursor"] == before
        replay = _run(_stream())
        assert len(replay.body) == 2

    def test_cursor_is_saved_only_after_flush(self) -> None:
        _arm()
        before = _record_of(_scope())["cursor"]
        event = _page()
        seen_at_flush: list[object] = []

        class _Spy(io.StringIO):
            def flush(self) -> None:
                seen_at_flush.append(_record_of(_scope())["cursor"])
                super().flush()

        _run(_stream(), out=_Spy())

        assert before in seen_at_flush
        assert _record_of(_scope())["cursor"] == event.id


class TestFollow:
    def test_idle_emits_nothing(self) -> None:
        run = _run(_stream(), sleeps=20)
        assert run.body == []
        assert len(run.lines) == 2

    def test_survives_prune_mid_follow(self) -> None:
        _page("T-a")
        _page("T-b")

        def act(n: int) -> None:
            if n == 1:
                prune_events(keep=1, archive=False)
            if n == 2:
                _page("T-after")

        run = _run(_stream(), sleeps=3, on_sleep=act)

        assert len(run.body) == 1
        assert "#T-after" in run.body[0]

    def test_out_of_scope_lane_dropped(self) -> None:
        scope = _scope(lanes=frozenset({_FAST}))
        _arm(scope)
        _page("T-1", lane=DEFAULT_LANE)
        _page("T-2", lane=_FAST)

        run = _run(_stream(scope))

        assert len(run.body) == 1
        assert "#T-2" in run.body[0]

    def test_event_line_carries_the_rows_next_command(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING)])
        _arm()
        _page()

        run = _run(_stream())

        assert run.body[0].endswith(
            f"| next: cw dev-queue wait {ACME_TICKET} -c {ACME}"
        )


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


class TestStuckScan:
    def test_emits_once_per_stuck_row(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING, lane=DEFAULT_LANE)])
        _stuck_lane()

        run = _run(_stream(stuck_after_minutes=1), sleeps=200)

        assert run.body == [
            f"STUCK | 2026-10-18T14:03:00 | #{ACME_TICKET} | lane_circuit_paused"
            " | pending 1m | lane default circuit breaker tripped (spawn_errors);"
            " no auto-resume is scheduled | lane=default"
            f" | next: cw lane resume {ACME} default"
        ]

    def test_out_of_scope_row_never_scanned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING, lane=DEFAULT_LANE)])
        _stuck_lane()
        calls: list[object] = []
        monkeypatch.setattr(
            "cw.attention_stream.scan_pending",
            lambda rows, now: calls.append((rows, now)) or [],
        )

        run = _run(
            _stream(_scope(lanes=frozenset({_FAST})), stuck_after_minutes=1), sleeps=100
        )

        assert run.body == []
        assert calls == []

    def test_row_leaving_pending_is_forgotten(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING, lane=DEFAULT_LANE)])
        _stuck_lane()

        def act(n: int) -> None:
            if n == 1:
                seed_queue([make_task(status=QueueItemStatus.RUNNING, session_id="s")])

        run = _run(_stream(stuck_after_minutes=1), sleeps=200, on_sleep=act)

        assert run.body == []


def _owner(pid: int) -> None:
    save_cursor_record(_scope().consumer, None, {"owner_pid": pid, "latch": {}})


def _kill_raising(exc: type[OSError] | None) -> Callable[[int, int], None]:
    def kill(_pid: int, _sig: int) -> None:
        if exc is not None:
            raise exc

    return kill


class TestPidAdvisory:
    def test_live_foreign_pid_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _owner(999)
        monkeypatch.setattr("cw.attention_stream.os.kill", _kill_raising(None))

        run = _run(_stream())

        assert run.code == EXIT_ERROR
        assert run.lines[1] == (
            "WATCH | exited: another watcher (pid 999) owns cursor"
            f" {_scope().consumer}; pass --force if it is stale"
            " (a recycled pid cannot be detected)"
        )
        assert _extra(_scope())["owner_pid"] == 999

    def test_permission_error_counts_as_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _owner(999)
        monkeypatch.setattr(
            "cw.attention_stream.os.kill", _kill_raising(PermissionError)
        )
        assert _run(_stream()).code == EXIT_ERROR

    def test_force_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _owner(999)
        monkeypatch.setattr("cw.attention_stream.os.kill", _kill_raising(None))

        run = _run(_stream(force=True))

        assert run.code == EXIT_SIGINT
        assert _extra(_scope())["owner_pid"] == _PID

    def test_dead_pid_is_taken_over(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _owner(999)
        monkeypatch.setattr(
            "cw.attention_stream.os.kill", _kill_raising(ProcessLookupError)
        )

        assert _run(_stream()).code == EXIT_SIGINT
        assert _extra(_scope())["owner_pid"] == _PID

    def test_own_pid_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _owner(_PID)
        monkeypatch.setattr("cw.attention_stream.os.kill", _kill_raising(None))
        assert _run(_stream()).code == EXIT_SIGINT

    def test_recycled_pid_looks_alive_and_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Documented limitation (D5): an unrelated live process holding the
        # recorded pid is indistinguishable from the old watcher.
        _owner(os.getppid())
        assert _run(_stream()).code == EXIT_ERROR


class TestExit:
    def test_fatal_error_prints_exit_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> list[str]:
            msg = "inbox exploded"
            raise ValueError(msg)

        stream = _stream()
        monkeypatch.setattr(stream, "step", boom)

        run = _run(stream)

        assert run.code == EXIT_ERROR
        assert run.lines[-1] == "WATCH | exited: ValueError: inbox exploded"

    def test_fatal_error_with_dead_stdout_goes_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> list[str]:
            msg = "no rows"
            raise LookupError(msg)

        stream = _stream()
        monkeypatch.setattr(stream, "step", boom)
        out = _FailingStdout(ok=1)
        err = io.StringIO()

        run = _run(stream, out=out, err=err)

        assert run.code == EXIT_ERROR
        assert err.getvalue().strip() == ("WATCH | exited: LookupError: no rows")

    def test_sigterm_prints_exit_line_and_exits_zero(self) -> None:
        run = _run(
            _stream(),
            sleeps=5,
            on_sleep=lambda _n: os.kill(os.getpid(), signal.SIGTERM),
        )

        assert run.code == EXIT_SIGTERM
        assert run.lines[-1] == "WATCH | exited: signal SIGTERM"

    def test_sigint_exit_line(self) -> None:
        run = _run(_stream())
        assert run.code == EXIT_SIGINT
        assert run.lines[-1] == _SIGINT_LINE

    def test_runs_off_the_main_thread_without_signal_handlers(self) -> None:
        results: list[_Run] = []
        worker = threading.Thread(target=lambda: results.append(_run(_stream())))
        worker.start()
        worker.join(timeout=30)

        assert results[0].code == EXIT_SIGINT

    def test_previous_signal_handlers_are_restored(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        _run(_stream())
        assert signal.getsignal(signal.SIGTERM) is before


def test_stuck_line_timestamp_uses_now_fn() -> None:
    seed_queue([make_task(status=QueueItemStatus.PENDING, lane=DEFAULT_LANE)])
    _stuck_lane()
    later = _NOW + timedelta(hours=1)
    clock = _FakeClock(
        on_sleep=lambda n: (
            (_ for _ in ()).throw(KeyboardInterrupt) if n >= 100 else None
        )
    )
    out = io.StringIO()

    _stream(stuck_after_minutes=1).run(
        out,
        err=io.StringIO(),
        sleep=clock.sleep,
        clock=clock.monotonic,
        now_fn=lambda: later,
    )

    assert "STUCK | 2026-10-18T15:03:00 |" in out.getvalue()


# --- #2760: the stream never surfaces a human gate's releasing verb ----------

_WITHHELD = f"HUMAN GATE: cw dev-queue show {ACME_TICKET} -c {ACME}"
_SHOW = f"cw dev-queue show {ACME_TICKET} -c {ACME}"
_SHIP = f"cw dev-queue requeue {ACME_TICKET} -c {ACME} --stage finalize"
_OVERRIDE = (
    f'cw dev-queue approve {ACME_TICKET} -c {ACME} --override-must-fix --reason "<why>"'
)
_REVIEW_PENDING: dict[str, object] = {"status": "review_pending_approval"}
_HUMAN_CASE_IDS = sorted(c for c in CASES if c.startswith("rule0_"))


def _large_final(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "stage": Stage.FINALIZE,
        "scope_hint": "large",
        "disposition": "review_pending_approval",
    }
    kwargs.update(overrides)
    return make_blocked(**kwargs)


def _gate(task: TicketTask) -> Session:
    return bound_session_for(task, last_result=_REVIEW_PENDING)


def _bare(verb: str) -> Callable[..., list[NextCommand]]:
    command = NextCommand(
        command=f"cw dev-queue {verb} {ACME_TICKET} -c {ACME}", purpose="x"
    )
    return lambda *_a, **_kw: [command]


class TestFirstNextCommandFacts:
    def test_rule9_terminal_is_the_plain_requeue(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE)
        session = bound_session_for(task, last_result=_REVIEW_PENDING)
        assert first_next_command(task, session, pipeline_position=_TERMINAL) == _SHIP

    @pytest.mark.parametrize("case_id", _HUMAN_CASE_IDS)
    def test_human_cases_are_withheld(self, case_id: str) -> None:
        case = CASES[case_id]
        got = first_next_command(case.task, case.session())
        assert got == _WITHHELD
        assert " approve " not in f"{got} "
        assert " requeue " not in f"{got} "

    def test_terminal_and_off_gate_rows_are_withheld(self) -> None:
        large = _large_final()
        gate = bound_session_for(large, last_result=_REVIEW_PENDING)
        assert first_next_command(large, gate, pipeline_position=_TERMINAL) == _WITHHELD
        review = _large_final(stage=Stage.REVIEW)
        off_gate = bound_session_for(review, last_result={"status": "stage_complete"})
        assert first_next_command(review, off_gate) == _WITHHELD

    def test_off_pipeline_rows_lead_with_show(self) -> None:
        held = make_blocked(
            stage=Stage.FINALIZE, disposition=FINALIZE_GATE_HELD_DISPOSITION
        )
        plain = make_blocked(stage=Stage.FINALIZE)
        for task in (held, plain):
            assert first_next_command(task, None, pipeline_position=_OFF) == _SHOW

    @pytest.mark.parametrize(
        ("verb", "withheld"), [("approve", True), ("requeue", True), ("show", False)]
    )
    def test_state_derived_withhold_without_the_flag(
        self, monkeypatch: pytest.MonkeyPatch, verb: str, withheld: bool
    ) -> None:
        monkeypatch.setattr("cw.attention_watch.next_commands", _bare(verb))
        human = CASES["rule0_finalize_gate_held"]
        got = first_next_command(human.task, human.session())
        bare = f"cw dev-queue {verb} {ACME_TICKET} -c {ACME}"
        assert got == (_WITHHELD if withheld else bare)

    def test_bare_approve_on_a_plain_row_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.attention_watch.next_commands", _bare("approve"))
        plain = CASES["rule9_review_gate"]
        got = first_next_command(plain.task, plain.session())
        assert got == f"cw dev-queue approve {ACME_TICKET} -c {ACME}"

    def test_must_fix_override_follows_the_opt_in(self) -> None:
        task = make_blocked(
            stage=Stage.REVIEW,
            blocked_reason=CODEX_MUST_FIX_FINDINGS,
            must_fix_override=make_override(),
        )
        assert first_next_command(task, None, operator_opt_in=False) == _SHIP
        assert first_next_command(task, None, operator_opt_in=True) == _OVERRIDE


def _stream_line(task: TicketTask, session: Session | None = None) -> str:
    seed_queue([task], [session] if session is not None else [])
    _arm()
    _page()
    run = _run(_stream())
    assert len(run.body) == 1
    return run.body[0]


def _must_fix_rows() -> list[TicketTask]:
    overridden = make_blocked(
        stage=Stage.REVIEW,
        blocked_reason=CODEX_MUST_FIX_FINDINGS,
        must_fix_override=make_override(),
    )
    plain = make_blocked(stage=Stage.REVIEW, blocked_reason=CODEX_MUST_FIX_FINDINGS)
    return [overridden, plain]


class TestStreamHumanGate:
    def test_human_gate_row_is_withheld(self) -> None:
        case = CASES["rule0_finalize_gate_held"]
        line = _stream_line(case.task, case.session())
        assert line.endswith(f"| next: {_WITHHELD}")

    def test_large_finalize_row_is_withheld(self) -> None:
        task = _large_final()
        line = _stream_line(task, _gate(task))
        assert line.endswith(f"| next: {_WITHHELD}")

    def test_rule9_finalize_row_gets_the_plain_requeue(self) -> None:
        task = make_blocked(stage=Stage.FINALIZE)
        line = _stream_line(task, _gate(task))
        assert line.endswith(f"| next: {_SHIP}")

    def test_state_derived_withhold_through_the_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cw.attention_watch.next_commands", _bare("approve"))
        line = _stream_line(CASES["rule0_finalize_gate_held"].task)
        assert line.endswith(f"| next: {_WITHHELD}")

    @pytest.mark.parametrize(
        "arm", ["lane_finalize_gate", "lane_signoff", "global", "malformed"]
    )
    @pytest.mark.parametrize("row", [0, 1])
    def test_armed_opt_in_drops_the_ship_step(
        self, tmp_path: Path, arm: str, row: int
    ) -> None:
        if arm == "lane_finalize_gate":
            write_lane_client(
                ACME,
                tmp_path / "ws",
                lanes=(DEFAULT_LANE, _FAST),
                finalize_gate="manual",
            )
        elif arm == "lane_signoff":
            write_lane_client(
                ACME, tmp_path / "ws", lanes=(DEFAULT_LANE, _FAST), signoff="operator"
            )
        else:
            orchestrator_config_file().parent.mkdir(parents=True, exist_ok=True)
            orchestrator_config_file().write_text(
                "default_finalize_gate: manual\n"
                if arm == "global"
                else "default_finalize_gate: [not, valid\n"
            )
        line = _stream_line(_must_fix_rows()[row])
        assert line.endswith(f"| next: {_OVERRIDE}")
        assert "--stage finalize" not in line

    def test_nothing_armed_keeps_the_ship_step(self) -> None:
        overridden = _must_fix_rows()[0]
        assert _stream_line(overridden).endswith(f"| next: {_SHIP}")

    def test_nothing_armed_plain_must_fix_leads_with_override(self) -> None:
        plain = _must_fix_rows()[1]
        assert _stream_line(plain).endswith(f"| next: {_OVERRIDE}")

    def test_unreadable_client_config_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(_name: str) -> object:
            msg = "disk"
            raise OSError(msg)

        monkeypatch.setattr("cw.ticket_view.get_effective_client", _fail)
        task = make_blocked(stage=Stage.FINALIZE)
        line = _stream_line(task, _gate(task))
        assert line.endswith(f"| next: cw dev-queue approve {ACME_TICKET} -c {ACME}")
