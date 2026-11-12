"""``_accumulate_task_cost`` with usage and priced estimates (#3063, RFC 0018 C4).

The existing ``TestAccumulateTaskCost`` in ``tests/test_dispatch.py`` keeps
guarding the unchanged ``cost_usd``, ``last_result`` and both-absent paths.
"""

from __future__ import annotations

import pytest

from cw.config import save_state
from cw.dispatch.routing.cost import _accumulate_task_cost
from cw.models import CwState, QueueItemStatus, TicketTask
from cw.models.usage import SessionUsage, TokenTotals, UsageState
from tests._shared.session_seeding import _make_daemon_session, _make_ticket_task

_SID = "sid-cost"


def _usage(input_tokens: int, output_tokens: int) -> SessionUsage:
    return SessionUsage(
        state=UsageState.RECORDED,
        by_model={
            "m": TokenTotals(
                input_tokens=input_tokens, output_tokens=output_tokens, turns=1
            )
        },
    )


def _task(**overrides: object) -> TicketTask:
    return _make_ticket_task(
        ticket_id="GEN-1",
        client="test-client",
        status=QueueItemStatus.RUNNING,
        session_id=_SID,
        **overrides,
    )


def _save(**session_overrides: object) -> None:
    save_state(CwState(sessions=[_make_daemon_session(id=_SID, **session_overrides)]))


class TestEstimateNeverDoubleCounts:
    def test_cost_usd_wins_over_a_breakdown(self) -> None:
        _save(cost_usd=1.5, cost_breakdown={"m": 9.0})
        task = _task()

        _accumulate_task_cost(task, _SID)

        assert task.total_cost_usd == pytest.approx(1.5)

    def test_last_result_cost_wins_over_a_breakdown(self) -> None:
        _save(last_result={"cost_usd": 2.0}, cost_breakdown={"m": 9.0})
        task = _task()

        _accumulate_task_cost(task, _SID)

        assert task.total_cost_usd == pytest.approx(2.0)

    def test_breakdown_sum_counts_when_no_reported_cost(self) -> None:
        _save(cost_breakdown={"a": 0.25, "b": 0.5})
        task = _task()

        _accumulate_task_cost(task, _SID)

        assert task.total_cost_usd == pytest.approx(0.75)

    def test_breakdown_adds_to_an_existing_total(self) -> None:
        _save(cost_breakdown={"a": 0.25})
        task = _task(total_cost_usd=1.0)

        _accumulate_task_cost(task, _SID)

        assert task.total_cost_usd == pytest.approx(1.25)

    def test_cost_usd_is_never_written_by_accumulation(self) -> None:
        _save(cost_breakdown={"a": 0.25})
        task = _task()

        _accumulate_task_cost(task, _SID)

        assert task.total_cost_usd is not None


class TestTokenAccumulation:
    def test_none_goes_to_n_then_n_plus_m(self) -> None:
        task = _task()
        _save(usage=_usage(100, 10))
        _accumulate_task_cost(task, _SID)
        assert task.total_tokens == 110

        _save(usage=_usage(200, 20))
        _accumulate_task_cost(task, _SID)
        assert task.total_tokens == 330

    def test_a_session_without_usage_leaves_tokens_unchanged(self) -> None:
        _save()
        task = _task(total_tokens=5)

        _accumulate_task_cost(task, _SID)

        assert task.total_tokens == 5

    def test_unavailable_usage_leaves_tokens_unchanged(self) -> None:
        _save(usage=SessionUsage(state=UsageState.UNAVAILABLE))
        task = _task()

        _accumulate_task_cost(task, _SID)

        assert task.total_tokens is None

    def test_priced_and_unpriced_sessions_both_add_tokens(self) -> None:
        task = _task()
        _save(usage=_usage(10, 1), cost_breakdown={"m": 0.5})
        _accumulate_task_cost(task, _SID)
        _save(usage=_usage(20, 2))
        _accumulate_task_cost(task, _SID)

        assert task.total_tokens == 33
        assert task.total_cost_usd == pytest.approx(0.5)

    def test_old_row_without_total_tokens_loads_none(self) -> None:
        raw = _task().model_dump(mode="json")
        raw.pop("total_tokens")

        assert TicketTask.model_validate(raw).total_tokens is None

    def test_missing_session_or_id_changes_nothing(self) -> None:
        save_state(CwState(sessions=[]))
        task = _task(total_tokens=3)

        _accumulate_task_cost(task, None)
        _accumulate_task_cost(task, "absent")

        assert task.total_tokens == 3
