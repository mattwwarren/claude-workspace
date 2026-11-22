"""The pre-lock REVIEW tip sweep: bounds, deferral, drain and input failure (#2994).

The sweep is driven through scripted plans (one per event id) and a fake
``tolerates_review_move`` that advances an injected clock, so no git runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cw.dispatch import review_gates
from cw.dispatch import review_tip_sweep as sweep_mod
from cw.dispatch.review_gates import _should_gate_for_review_staleness
from cw.dispatch.review_tip_sweep import (
    _MAX_TIP_CHECKS_PER_TICK,
    _TIP_SWEEP_BUDGET_SECONDS,
    resolve_review_tip_sweep,
    review_tip_sweep_lock_free,
    tip_sweep_scope,
)
from cw.dispatch.review_tip_verdicts import (
    TipCheck,
    TipKey,
    installed_tip_verdict,
    precomputed_tip_verdicts,
)
from cw.exceptions import CwError
from cw.models import CwState, DevQueueStore, Stage
from tests._review_convergence_helpers import (
    confirmed_ledger,
    make_review_client,
    write_ledger,
)
from tests._shared.dispatch_fixtures import (
    SWEEP_CLIENT,
    stub_head,
    sweep_event,
    sweep_row,
)
from tests._shared.session_seeding import _make_daemon_session
from tests._worktree_helpers import FakeClock


def _event(event_id: int) -> Any:
    return SimpleNamespace(id=event_id)


def _check(event_id: int, *, sha: str | None = None) -> TipCheck:
    key = TipKey("c", f"T-{event_id}", f"r{sha or event_id}", f"h{event_id}")
    return TipCheck(
        key=key, client=cast("Any", None), worktree=Path("/wt"), task=cast("Any", None)
    )


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Plans per event id, and a fake checker that records and costs each call."""
    state: dict[str, Any] = {"plans": {}, "calls": [], "cost": 0.0, "clock": None}

    def plan_for(event: Any, store: Any, st: Any, clients: Any) -> Any:
        return state["plans"].get(event.id)

    def fake_tolerates(
        _client: Any, _wt: Path, *, reviewed_sha: str, head_sha: str, task: Any
    ) -> bool:
        state["calls"].append((reviewed_sha, head_sha))
        state["clock"].advance(state["cost"])
        return True

    monkeypatch.setattr(sweep_mod, "_plan_for_event", plan_for)
    monkeypatch.setattr(sweep_mod, "tolerates_review_move", fake_tolerates)
    return state


def _run(scripted: dict[str, Any], events: list[Any], *, clock: FakeClock) -> Any:
    scripted["clock"] = clock
    return resolve_review_tip_sweep(
        events, cast("Any", None), cast("Any", None), {}, clock=clock
    )


def test_tick_over_the_cap_checks_at_most_the_cap_and_holds_the_rest(
    scripted: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    events = [_event(i) for i in range(_MAX_TIP_CHECKS_PER_TICK + 3)]
    scripted["plans"] = {e.id: _check(e.id) for e in events}
    with caplog.at_level(logging.INFO, logger="cw.dispatch"):
        sweep = _run(scripted, events, clock=FakeClock())
    assert len(scripted["calls"]) == _MAX_TIP_CHECKS_PER_TICK
    assert sweep.events == events[:_MAX_TIP_CHECKS_PER_TICK]
    assert sweep.deferred == 3
    assert len(sweep.verdicts) == _MAX_TIP_CHECKS_PER_TICK
    assert "held for a later tick" in caplog.text


def test_a_slow_check_spends_the_budget_and_defers(scripted: dict[str, Any]) -> None:
    events = [_event(i) for i in range(6)]
    scripted["plans"] = {e.id: _check(e.id) for e in events}
    scripted["cost"] = 12.0
    sweep = _run(scripted, events, clock=FakeClock())
    # t = 0, 12, 24 are under the 30 s budget; at 36 the sweep stops.
    assert len(scripted["calls"]) == 3
    assert sweep.events == events[:3]

    scripted["calls"].clear()
    scripted["cost"] = _TIP_SWEEP_BUDGET_SECONDS + 1.0
    sweep = _run(scripted, events, clock=FakeClock())
    assert len(scripted["calls"]) == 1
    assert sweep.events == events[:1]


def test_the_first_checkable_row_is_checked_even_when_the_clock_is_spent(
    scripted: dict[str, Any],
) -> None:
    """The progress rule: the clock is past the budget BEFORE the first check.

    A clock that only advances after check 1 cannot tell ``checked >= 1`` from
    no guard at all, so this one reads 0.0 once (``started``) and the spent
    budget on every later call.
    """
    readings = iter([0.0])

    def spent_clock() -> float:
        return next(readings, _TIP_SWEEP_BUDGET_SECONDS + 1.0)

    events = [_event(0), _event(1)]
    scripted["plans"] = {e.id: _check(e.id) for e in events}
    scripted["clock"] = FakeClock()  # the fake checker's cost sink; cost is 0
    sweep = resolve_review_tip_sweep(
        events, cast("Any", None), cast("Any", None), {}, clock=spent_clock
    )
    assert len(scripted["calls"]) == 1
    assert sweep.events == events[:1]
    assert sweep.deferred == 1


def test_successive_ticks_drain_the_backlog_in_order_exactly_once(
    scripted: dict[str, Any],
) -> None:
    total = 2 * _MAX_TIP_CHECKS_PER_TICK + 5
    pending = [_event(i) for i in range(total)]
    scripted["plans"] = {e.id: _check(e.id) for e in pending}
    processed: list[int] = []
    ticks = 0
    while pending:
        sweep = _run(scripted, pending, clock=FakeClock())
        assert sweep.events, "every tick applies at least one event"
        processed.extend(e.id for e in sweep.events)
        pending = pending[len(sweep.events) :]
        ticks += 1
    assert processed == list(range(total))
    assert ticks == 3


def test_a_final_plan_costs_no_budget(scripted: dict[str, Any]) -> None:
    plain = [_event(i) for i in range(_MAX_TIP_CHECKS_PER_TICK + 5)]
    checkable = _event(999)
    scripted["plans"] = {e.id: True for e in plain} | {999: _check(999)}
    sweep = _run(scripted, [*plain, checkable], clock=FakeClock())
    assert scripted["calls"] == [("r999", "h999")]
    assert sweep.events == [*plain, checkable]
    assert sweep.deferred == 0


def test_a_repeated_key_costs_one_check(scripted: dict[str, Any]) -> None:
    events = [_event(0), _event(1), _event(2)]
    same = _check(0)
    scripted["plans"] = {0: same, 1: same, 2: same}
    sweep = _run(scripted, events, clock=FakeClock())
    assert len(scripted["calls"]) == 1
    assert sweep.events == events
    assert sweep.verdicts == {same.key: True}


def test_a_deferred_prefix_keeps_an_earlier_no_check_event_and_holds_a_later_one(
    scripted: dict[str, Any],
) -> None:
    cap = _MAX_TIP_CHECKS_PER_TICK
    checks = [_event(i) for i in range(cap)]
    plain = _event(100)
    extra = _event(101)
    plain_after = _event(102)
    scripted["plans"] = {e.id: _check(e.id) for e in checks}
    scripted["plans"][100] = True
    scripted["plans"][101] = _check(101)
    scripted["plans"][102] = True
    # [checks x cap, plain, extra, plain_after]: plain needs no check and
    # precedes the cut at extra, so it is applied; extra is the first row the
    # cap holds back, and plain_after waits behind it (a linear cursor cannot
    # skip an event, even one that needs no check).
    sweep = _run(scripted, [*checks, plain, extra, plain_after], clock=FakeClock())
    assert sweep.events == [*checks, plain]
    assert sweep.deferred == 2


def test_unreadable_input_yields_no_sweep_and_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom() -> None:
        msg = "corrupt"
        raise ValueError(msg)

    monkeypatch.setattr(sweep_mod, "load_dev_queue", boom)
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        assert review_tip_sweep_lock_free([_event(0)]) is None
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert "applying every event inline" in record.getMessage()


@pytest.mark.parametrize(
    ("loader", "exc"),
    [
        ("load_state", OSError("disk")),
        ("load_effective_clients", CwError("bad clients")),
    ],
)
def test_other_unreadable_inputs_also_yield_none(
    monkeypatch: pytest.MonkeyPatch, loader: str, exc: Exception
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise exc

    monkeypatch.setattr(sweep_mod, "load_dev_queue", lambda: None)
    monkeypatch.setattr(sweep_mod, "load_state", lambda: None)
    monkeypatch.setattr(sweep_mod, "load_effective_clients", dict)
    monkeypatch.setattr(sweep_mod, loader, boom)
    assert review_tip_sweep_lock_free([_event(0)]) is None


def test_tip_sweep_scope_installs_verdicts_only_inside_the_block(
    scripted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _check(0)
    scripted["plans"] = {0: check}
    scripted["clock"] = FakeClock()
    sweep = resolve_review_tip_sweep(
        [_event(0)], cast("Any", None), cast("Any", None), {}, clock=scripted["clock"]
    )
    monkeypatch.setattr(sweep_mod, "review_tip_sweep_lock_free", lambda _e: sweep)

    with tip_sweep_scope([_event(0)]) as batch:
        assert batch == sweep.events
        assert installed_tip_verdict(check.key) is True
    assert installed_tip_verdict(check.key) is None


def test_tip_sweep_scope_on_a_none_sweep_yields_every_event_and_no_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sweep_mod, "review_tip_sweep_lock_free", lambda _e: None)
    events = [_event(0), _event(1)]
    key = TipKey("c", "T", "r", "h")
    with tip_sweep_scope(events) as batch:
        assert batch == events
        assert installed_tip_verdict(key) is None


# ---------------------------------------------------------------------------
# The REAL planner: rows, sessions, ledgers; only the git predicate is faked.
# ---------------------------------------------------------------------------

CLEAN = "c" * 40
HEAD = "d" * 40
_GATE_STATUS = "review_pending_approval"


class _World:
    """Rows, sessions, worktrees and events for one tick, built from real inputs."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.tasks: list[Any] = []
        self.sessions: list[Any] = []
        self.events: list[Any] = []
        self.clients = {SWEEP_CLIENT: make_review_client(tmp_path, name=SWEEP_CLIENT)}

    def add(
        self,
        index: int,
        *,
        reviewed: str | None = CLEAN,
        review: bool = True,
        ledger: bool = True,
        stage: Stage = Stage.REVIEW,
        status: str = _GATE_STATUS,
    ) -> str:
        """One RUNNING row with a session, a stamped worktree and its event."""
        ticket = f"T-{index}"
        worktree = self.tmp_path / f"wt{index}"
        worktree.mkdir()
        if ledger:
            write_ledger(worktree, confirmed_ledger(sha=CLEAN))
        review_block: dict[str, object] = {"agents_run": 3}
        if reviewed is not None:
            review_block["reviewed_sha"] = reviewed
        last_result: dict[str, object] = {"status": status}
        if review:
            last_result["review"] = review_block
        self.tasks.append(
            sweep_row(worktree, ticket_id=ticket, session_id=f"s{index}", stage=stage)
        )
        self.sessions.append(
            _make_daemon_session(
                id=f"s{index}",
                name=f"{SWEEP_CLIENT}/auto-dev/{ticket}",
                client=SWEEP_CLIENT,
                last_result=last_result,
            )
        )
        self.events.append(sweep_event(ticket, session_id=f"s{index}"))
        return ticket

    def sweep(self, *, clock: FakeClock | None = None) -> Any:
        return resolve_review_tip_sweep(
            self.events,
            DevQueueStore(tasks=self.tasks),
            CwState(sessions=self.sessions),
            self.clients,
            clock=clock or FakeClock(),
        )


class _Checker:
    """A fake ``tolerates_review_move`` for both binding sites, costing *cost* s."""

    def __init__(self, clock: FakeClock, *, cost: float = 0.0) -> None:
        self.clock = clock
        self.cost = cost
        self.calls: list[Path] = []

    def __call__(
        self,
        _client: object,
        worktree: Path,
        *,
        reviewed_sha: str,
        head_sha: str,
        task: object,
    ) -> bool:
        self.calls.append(worktree)
        self.clock.advance(self.cost)
        return worktree.name != "wt1"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    stub_head(monkeypatch, HEAD)
    return _World(tmp_path)


def _install_checker(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock, *, cost: float = 0.0
) -> _Checker:
    checker = _Checker(clock, cost=cost)
    monkeypatch.setattr(sweep_mod, "tolerates_review_move", checker)
    monkeypatch.setattr(review_gates, "tolerates_review_move", checker)
    return checker


def test_verdict_keys_carry_reviewed_and_head_shas(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    _install_checker(monkeypatch, clock)
    ticket = world.add(0)

    sweep = world.sweep(clock=clock)

    assert sweep.verdicts == {TipKey(SWEEP_CLIENT, ticket, CLEAN, HEAD): True}


def test_unmatched_events_pass_through(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No RUNNING row, a reconcile-owned event and a legacy event cost no check."""
    clock = FakeClock()
    checker = _install_checker(monkeypatch, clock)
    world.add(0)
    orphan = sweep_event("T-nobody", session_id="s-nobody")
    crashed = sweep_event("T-0", session_id="s0")
    crashed.payload["crashed"] = True
    legacy = sweep_event("T-0")  # no session_id: cannot disambiguate a row
    world.events = [orphan, crashed, legacy]

    sweep = world.sweep(clock=clock)

    assert sweep.events == [orphan, crashed, legacy]
    assert sweep.deferred == 0
    assert sweep.verdicts == {}
    assert checker.calls == []


@pytest.mark.parametrize(
    ("kind", "row_kwargs", "client_kwargs"),
    [
        pytest.param("same-sha", {"reviewed": HEAD}, {}, id="reviewed-is-head"),
        pytest.param("no-ledger", {"ledger": False}, {}, id="no-ledger"),
        pytest.param("no-review", {"review": False}, {}, id="no-review-block"),
        pytest.param("no-sha", {"reviewed": None}, {}, id="missing-reviewed-sha"),
        pytest.param("finalize", {"stage": Stage.FINALIZE}, {}, id="finalize-stage"),
        pytest.param("status", {"status": "blocked"}, {}, id="non-gate-status"),
        pytest.param("blocking", {}, {"convergence": "blocking"}, id="blocking-mode"),
        pytest.param(
            "strict", {}, {"default_merge_tolerance": False}, id="strict-opt-out"
        ),
    ],
)
def test_rows_that_decide_from_reads_cost_no_budget(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    row_kwargs: dict[str, Any],
    client_kwargs: dict[str, Any],
) -> None:
    clock = FakeClock()
    checker = _install_checker(monkeypatch, clock)
    world.clients = {
        SWEEP_CLIENT: make_review_client(
            world.tmp_path, name=SWEEP_CLIENT, **client_kwargs
        )
    }
    plain = _MAX_TIP_CHECKS_PER_TICK + 5
    for index in range(plain):
        world.add(index, **row_kwargs)
    # A client-level scenario leaves no checkable row to put at the tail.
    if not client_kwargs:
        world.add(plain)

    sweep = world.sweep(clock=clock)

    assert sweep.events == world.events
    assert sweep.deferred == 0
    assert len(checker.calls) == (0 if client_kwargs else 1), kind


def test_a_verdict_is_what_the_in_lock_gate_then_returns(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real planner -> sweep -> installed verdict -> gate, with no inline git."""
    clock = FakeClock()
    checker = _install_checker(monkeypatch, clock)
    for index in range(3):
        world.add(index)
    sweep = world.sweep(clock=clock)
    assert len(checker.calls) == 3

    def no_inline(*_a: object, **_k: object) -> bool:
        pytest.fail("the locked gate ran the tip check inline")

    monkeypatch.setattr(review_gates, "tolerates_review_move", no_inline)
    with precomputed_tip_verdicts(sweep.verdicts):
        gated = {
            task.ticket_id: _should_gate_for_review_staleness(
                task,
                world.sessions[i].last_result,
                world.clients,
            )
            for i, task in enumerate(world.tasks)
        }

    # The fake tolerates every worktree but wt1, so only T-1 gates.
    assert gated == {"T-0": False, "T-1": True, "T-2": False}


def test_a_normal_tick_has_zero_verdict_misses(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sweep and the gate derive one key, so the inline fallback never runs."""
    clock = FakeClock()
    checker = _install_checker(monkeypatch, clock)
    for index in range(_MAX_TIP_CHECKS_PER_TICK):
        world.add(index)
    sweep = world.sweep(clock=clock)
    assert sweep.deferred == 0
    swept_calls = len(checker.calls)

    with (
        caplog.at_level(logging.WARNING, logger="cw.dispatch"),
        precomputed_tip_verdicts(sweep.verdicts),
    ):
        for i, task in enumerate(world.tasks):
            _should_gate_for_review_staleness(
                task, world.sessions[i].last_result, world.clients
            )

    assert "no precomputed verdict" not in caplog.text
    assert len(checker.calls) == swept_calls == _MAX_TIP_CHECKS_PER_TICK


def test_deferral_logs_one_info_line_with_exact_format(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    _install_checker(monkeypatch, clock)
    for index in range(_MAX_TIP_CHECKS_PER_TICK + 2):
        world.add(index)

    with caplog.at_level(logging.INFO, logger="cw.dispatch"):
        world.sweep(clock=clock)

    assert caplog.messages == [
        "review_tip: 2 completion event(s) held for a later tick "
        f"({_MAX_TIP_CHECKS_PER_TICK} tip check(s) run, cap "
        f"{_MAX_TIP_CHECKS_PER_TICK}, budget {_TIP_SWEEP_BUDGET_SECONDS:.0f}s); "
        "their rows stay RUNNING and are examined next tick"
    ]


def test_no_deferral_logs_nothing(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    _install_checker(monkeypatch, clock)
    world.add(0)

    with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
        world.sweep(clock=clock)

    assert caplog.messages == []
