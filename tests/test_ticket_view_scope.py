"""Tests for the scope-drift decision view (#3772): owner, paths and next action."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.models.enums import QueueItemStatus, Stage
from cw.models.scope_grant import ScopeGrant
from cw.ticket_view import _scope_decision_text
from cw.ticket_view_scope import build_scope_decision
from tests._ticket_view_helpers import make_task

if TYPE_CHECKING:
    from cw.models.tasks.ticket_task import TicketTask

_DETAILS = "extra_files:\n- src/c.py\n- src/d.py\n"


def _parked(**overrides: object) -> TicketTask:
    kwargs: dict[str, object] = {
        "status": QueueItemStatus.BLOCKED_ON_USER,
        "stage": Stage.IMPL,
        "blocked_reason": PLAN_SCOPE_DRIFT_BLOCKER_REASON,
    }
    kwargs.update(overrides)
    return make_task(**kwargs)


def _grant(
    task: TicketTask,
    paths: list[str],
    *,
    ticket_id: str | None = None,
    head: str = "1" * 40,
) -> ScopeGrant:
    return ScopeGrant(
        client=task.client,
        ticket_id=ticket_id or task.ticket_id,
        row_created_at=task.created_at,
        head=head,
        policy="p",
        paths=paths,
        recorded_at=datetime(2026, 11, 3, tzinfo=UTC),
    )


class TestBuildScopeDecision:
    def test_none_outside_a_scope_drift_park(self) -> None:
        task = make_task(status=QueueItemStatus.PENDING, stage=Stage.IMPL)
        assert build_scope_decision(task, human_owner=False, details=_DETAILS) is None

    def test_operator_owns_an_ordinary_park(self) -> None:
        decision = build_scope_decision(_parked(), human_owner=False, details=_DETAILS)

        assert decision is not None
        assert decision.owner == "operator"
        assert decision.new_paths == ["src/c.py", "src/d.py"]
        assert decision.next_action.endswith("--scope-drift src/c.py,src/d.py")
        assert not decision.next_action.startswith("escalate")

    def test_human_owner_on_opt_in_is_prefixed(self) -> None:
        decision = build_scope_decision(_parked(), human_owner=True, details=_DETAILS)

        assert decision is not None
        assert decision.owner == "human"
        assert decision.next_action.startswith("escalate to the human: cw dev-queue")

    def test_row_scope_hint_large_is_a_human_opt_in(self) -> None:
        task = _parked(scope_hint="large")

        decision = build_scope_decision(task, human_owner=False, details=_DETAILS)

        assert decision is not None
        assert decision.owner == "human"

    def test_carried_paths_are_this_rows_grants_union(self) -> None:
        task = _parked()
        task.scope_grants = [
            _grant(task, ["src/b.py", "src/a.py"]),
            _grant(task, ["src/a.py"], head="2" * 40),
            _grant(task, ["src/other.py"], ticket_id="OTHER-1"),
        ]

        decision = build_scope_decision(task, human_owner=False, details=_DETAILS)

        assert decision is not None
        assert decision.carried_paths == ["src/a.py", "src/b.py"]

    def test_unparseable_details_leave_new_paths_empty_with_placeholder(self) -> None:
        decision = build_scope_decision(
            _parked(), human_owner=False, details="no list here"
        )

        assert decision is not None
        assert decision.new_paths == []
        assert decision.next_action.endswith("--scope-drift <paths>")


class TestScopeDecisionText:
    def test_absent_renders_a_dash(self) -> None:
        assert _scope_decision_text(None) == "-"

    def test_present_renders_owner_paths_and_next(self) -> None:
        decision = build_scope_decision(_parked(), human_owner=False, details=_DETAILS)

        line = _scope_decision_text(decision)

        assert line.startswith("owner=operator; carried=-; new=src/c.py, src/d.py;")
        assert "next=cw dev-queue approve" in line
