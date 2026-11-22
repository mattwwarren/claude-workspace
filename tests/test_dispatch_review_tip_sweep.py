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

from cw.dispatch import review_tip_sweep as sweep_mod
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
)
from cw.exceptions import CwError


class FakeClock:
    """A monotonic clock the fake checker advances by its cost."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _event(event_id: int) -> Any:
    return SimpleNamespace(id=event_id)


def _check(event_id: int, *, sha: str | None = None) -> TipCheck:
    key = TipKey("c", f"T-{event_id}", f"r{sha or event_id}", f"h{event_id}")
    return TipCheck(key=key, client=cast("Any", None), worktree=Path("/wt"))


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Plans per event id, and a fake checker that records and costs each call."""
    state: dict[str, Any] = {"plans": {}, "calls": [], "cost": 0.0, "clock": None}

    def plan_for(event: Any, store: Any, st: Any, clients: Any) -> Any:
        return state["plans"].get(event.id)

    def fake_tolerates(
        _client: Any, _wt: Path, *, reviewed_sha: str, head_sha: str
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
    events = [_event(0), _event(1)]
    scripted["plans"] = {e.id: _check(e.id) for e in events}
    # The first check alone costs more than the whole budget.
    scripted["cost"] = _TIP_SWEEP_BUDGET_SECONDS * 10
    sweep = _run(scripted, events, clock=FakeClock())
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


def test_rows_that_decide_from_reads_cost_no_budget(scripted: dict[str, Any]) -> None:
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
    scripted["plans"] = {e.id: _check(e.id) for e in checks}
    scripted["plans"][100] = True
    scripted["plans"][101] = _check(101)
    # [checks x cap, plain, extra]: plain needs no check and precedes the cut
    # at extra, so it is applied; extra is the first row the cap holds back.
    sweep = _run(scripted, [*checks, plain, extra], clock=FakeClock())
    assert sweep.events == [*checks, plain]
    assert sweep.deferred == 1


def test_unreadable_input_yields_no_sweep_and_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom() -> None:
        msg = "corrupt"
        raise ValueError(msg)

    monkeypatch.setattr(sweep_mod, "load_dev_queue", boom)
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        assert review_tip_sweep_lock_free([_event(0)]) is None
    assert "applying every event inline" in caplog.text


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


def test_a_verdict_miss_under_an_active_scope_warns_once(
    scripted: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    check = _check(0)
    sweep = resolve_review_tip_sweep([], cast("Any", None), cast("Any", None), {})
    assert sweep.verdicts == {}
    with caplog.at_level(logging.WARNING, logger="cw.dispatch"):
        from cw.dispatch.review_tip_verdicts import precomputed_tip_verdicts

        with precomputed_tip_verdicts({}):
            assert installed_tip_verdict(check.key) is None
    assert caplog.text.count("no precomputed verdict") == 1
