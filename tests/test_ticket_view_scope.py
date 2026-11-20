"""Tests for the scope-drift decision view (#3772): owner, paths and next action."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.auto_dev_result import PLAN_SCOPE_DRIFT_BLOCKER_REASON
from cw.models.enums import QueueItemStatus, Stage
from cw.ticket_view import (
    ViewSources,
    _scope_decision_text,
    build_ticket_view,
    render_ticket_view,
)
from cw.ticket_view_scope import build_scope_decision
from tests._shared.scope_grant_builders import drift_details, make_grant
from tests._ticket_view_helpers import (
    ACME,
    ACME_TICKET,
    bound_session_for,
    install_acme_client,
    make_blocked,
    make_task,
    seed_queue,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models.scope_grant import ScopeGrant
    from cw.models.tasks.ticket_task import TicketTask

_DETAILS = drift_details(["src/c.py", "src/d.py"])
_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


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
    return make_grant(
        client=task.client,
        ticket_id=ticket_id or task.ticket_id,
        row_created_at=task.created_at,
        head=head,
        paths=paths,
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


class TestBuildTicketViewScopeDecision:
    """``build_ticket_view`` wires the decision into the JSON view and text."""

    @pytest.fixture(autouse=True)
    def _acme(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        install_acme_client(tmp_path, monkeypatch)

    @staticmethod
    def _seed_parked(details: str) -> TicketTask:
        task = make_blocked(
            stage=Stage.IMPL, blocked_reason=PLAN_SCOPE_DRIFT_BLOCKER_REASON
        )
        task.scope_grants = [_grant(task, ["src/b.py", "src/a.py"])]
        session = bound_session_for(
            task,
            last_result={
                "status": "blocked",
                "blocker": {
                    "stage": "impl",
                    "reason": PLAN_SCOPE_DRIFT_BLOCKER_REASON,
                    "details": details,
                },
            },
        )
        seed_queue([task], [session])
        return task

    def test_json_view_carries_the_populated_decision(self) -> None:
        self._seed_parked(drift_details(["src/c.py"]))

        view = build_ticket_view(ACME_TICKET, ACME, now=_NOW, sources=ViewSources())

        decision = view.scope_decision
        assert decision is not None
        assert decision.carried_paths == ["src/a.py", "src/b.py"]
        assert decision.new_paths == ["src/c.py"]
        assert decision.owner == "operator"
        assert decision.next_action.endswith("--scope-drift src/c.py")
        dumped = view.model_dump(mode="json")["scope_decision"]
        assert set(dumped) == {"owner", "carried_paths", "new_paths", "next_action"}

    def test_text_view_prints_the_scope_decision_line(self) -> None:
        self._seed_parked(drift_details(["src/c.py"]))

        view = build_ticket_view(ACME_TICKET, ACME, now=_NOW, sources=ViewSources())
        lines = [
            ln
            for ln in render_ticket_view(view, now=_NOW).splitlines()
            if ln.startswith("scope_decision:")
        ]

        assert len(lines) == 1
        assert "owner=operator" in lines[0]
        assert "carried=src/a.py, src/b.py" in lines[0]
        assert "new=src/c.py" in lines[0]
        assert "next=cw dev-queue approve" in lines[0]

    def test_other_rows_have_no_decision(self) -> None:
        seed_queue([make_task(status=QueueItemStatus.PENDING, stage=Stage.IMPL)])

        view = build_ticket_view(ACME_TICKET, ACME, now=_NOW, sources=ViewSources())

        assert view.scope_decision is None
        text = render_ticket_view(view, now=_NOW)
        assert "scope_decision: -" in text.splitlines()
