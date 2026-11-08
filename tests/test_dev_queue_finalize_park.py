"""``cw.dev_queue.finalize_park``: the rule 0c/0d park predicates (#2779)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cw.dev_queue.finalize_park import (
    _STALE_TIP_LARGE_NOTE,
    _STALE_TIP_OTHER_TAIL,
    _STALE_TIP_ROUND_TAIL,
    approval_recorded_after_park,
    finalize_park_result,
    is_approval_park,
    is_stale_tip_park,
    stale_tip_park_reason,
    stale_tip_purpose,
)
from cw.models import QueueItemStatus, Stage
from tests._ticket_view_helpers import STEP40_PARK, bound_session_for, make_blocked

if TYPE_CHECKING:
    from cw.models import TicketTask

_STALE_TIP: dict[str, object] = {
    **STEP40_PARK,
    "scope": {"tier": "small"},
    "friction_highlights": ["review_tip_check: stale (no_clean_round)"],
}
_APPROVED_AT = datetime(2026, 11, 1, tzinfo=UTC)


def _park_row(**overrides: object) -> TicketTask:
    """A BLOCKED FINALIZE row parked ``review_pending_approval``."""
    kwargs: dict[str, object] = {
        "stage": Stage.FINALIZE,
        "disposition": "review_pending_approval",
    }
    kwargs.update(overrides)
    return make_blocked(**kwargs)


def _with_token(token: str) -> dict[str, object]:
    return {
        **STEP40_PARK,
        "friction_highlights": [f"review_approval_park: {token} — detail"],
    }


class TestFinalizeParkResult:
    def test_returns_the_stamped_sentinel(self) -> None:
        task = _park_row()
        session = bound_session_for(task, last_result=STEP40_PARK)
        assert finalize_park_result(task, session) == STEP40_PARK

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"stage": Stage.REVIEW}, id="not-finalize"),
            pytest.param({"status": QueueItemStatus.PENDING}, id="not-blocked"),
            pytest.param({"disposition": "approval_gate"}, id="other-disposition"),
        ],
    )
    def test_other_rows_are_none(self, overrides: dict[str, object]) -> None:
        task = _park_row(**overrides)
        session = bound_session_for(task, last_result=STEP40_PARK)
        assert finalize_park_result(task, session) is None

    def test_no_session_or_unstamped_sentinel_is_none(self) -> None:
        task = _park_row()
        assert finalize_park_result(task, None) is None
        unstamped = {**STEP40_PARK, "stage_reached": "stage3_review"}
        session = bound_session_for(task, last_result=unstamped)
        assert finalize_park_result(task, session) is None


class TestParkKinds:
    def test_step40_park_is_an_approval_park(self) -> None:
        task = _park_row()
        session = bound_session_for(task, last_result=STEP40_PARK)
        assert is_approval_park(task, session)
        assert not is_stale_tip_park(task, session)
        assert stale_tip_park_reason(task, session) is None

    def test_stale_tip_park_is_not_an_approval_park(self) -> None:
        task = _park_row()
        session = bound_session_for(task, last_result=_STALE_TIP)
        assert not is_approval_park(task, session)
        assert is_stale_tip_park(task, session)
        assert stale_tip_park_reason(task, session) == "no_clean_round"

    def test_non_finalize_row_is_neither(self) -> None:
        task = _park_row(stage=Stage.REVIEW)
        session = bound_session_for(task, last_result=STEP40_PARK)
        assert not is_approval_park(task, session)
        assert not is_stale_tip_park(task, session)


class TestApprovalRecordedAfterPark:
    def test_approval_after_a_missing_park(self) -> None:
        task = _park_row(review_approved_at=_APPROVED_AT)
        session = bound_session_for(task, last_result=_with_token("missing"))
        assert approval_recorded_after_park(task, session)

    @pytest.mark.parametrize("token", ["stale", "unreadable"])
    def test_other_tokens_never_qualify(self, token: str) -> None:
        task = _park_row(review_approved_at=_APPROVED_AT)
        session = bound_session_for(task, last_result=_with_token(token))
        assert not approval_recorded_after_park(task, session)

    def test_needs_an_approval_and_a_park(self) -> None:
        task = _park_row()
        session = bound_session_for(task, last_result=_with_token("missing"))
        assert not approval_recorded_after_park(task, session)
        approved = _park_row(review_approved_at=_APPROVED_AT)
        assert not approval_recorded_after_park(approved, None)
        no_token = bound_session_for(approved, last_result=STEP40_PARK)
        assert not approval_recorded_after_park(approved, no_token)


class TestStaleTipPurpose:
    def test_round_reason(self) -> None:
        purpose = stale_tip_purpose(_park_row(), "no_clean_round")
        assert purpose == (
            "a review round is needed: no clean review round is recorded;"
            f" {_STALE_TIP_ROUND_TAIL}"
        )

    @pytest.mark.parametrize(
        ("reason", "shown"),
        [("head_not_origin_tip", " (head_not_origin_tip)"), ("", "")],
    )
    def test_other_reason(self, reason: str, shown: str) -> None:
        purpose = stale_tip_purpose(_park_row(), reason)
        assert purpose == f"the final-tip check failed{shown}; {_STALE_TIP_OTHER_TAIL}"

    def test_large_note(self) -> None:
        purpose = stale_tip_purpose(_park_row(scope_hint="large"), "no_clean_round")
        assert purpose.endswith(f"; {_STALE_TIP_LARGE_NOTE}")
