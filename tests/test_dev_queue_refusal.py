"""``cw.dev_queue.refusal``: the session-loading side of refusal hints (#2692)."""

from __future__ import annotations

import pytest

from cw.dev_queue.next_commands import refusal_suffix
from cw.dev_queue.refusal import bound_session, refusal_hint
from cw.exceptions import CwError
from cw.models import Stage
from tests._ticket_view_helpers import (
    PLAN_PENDING,
    bound_session_for,
    make_blocked,
    make_task,
    seed_queue,
)


class TestBoundSession:
    def test_resolves_by_id(self) -> None:
        task = make_blocked(stage=Stage.PLAN)
        session = bound_session_for(task, last_result=PLAN_PENDING)
        seed_queue([task], [session])
        found = bound_session(task)
        assert found is not None
        assert found.id == "sess-1"

    def test_name_valued_session_id_is_not_resolved(self) -> None:
        # Documented divergence from approve's find_by_name_or_id.
        task = make_blocked(stage=Stage.PLAN, session_id="acme/auto-dev/ACM-1")
        seed_queue([task], [bound_session_for(task, id="real-id")])
        assert bound_session(task) is None

    def test_row_without_session_id(self) -> None:
        assert bound_session(make_task()) is None

    @pytest.mark.parametrize(
        "error", [OSError("disk"), ValueError("bad"), CwError("x")]
    )
    def test_unreadable_state_is_no_session(
        self, error: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail() -> object:
            raise error

        monkeypatch.setattr("cw.dev_queue.refusal.load_state", _fail)
        assert bound_session(make_blocked()) is None


class TestRefusalHint:
    def test_composes_bound_session_and_suffix(self) -> None:
        task = make_blocked(stage=Stage.PLAN)
        session = bound_session_for(task, last_result=PLAN_PENDING)
        seed_queue([task], [session])
        assert refusal_hint(task) == refusal_suffix(task, session=bound_session(task))
        assert "cw dev-queue approve ACM-1 -c acme" in refusal_hint(task)

    def test_skip_is_forwarded(self) -> None:
        task = make_blocked(stage=Stage.PLAN)
        seed_queue([task], [bound_session_for(task, last_result=PLAN_PENDING)])
        assert "approve" not in refusal_hint(task, skip="approve")
