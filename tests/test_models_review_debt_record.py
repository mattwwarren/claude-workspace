"""Tests for :mod:`cw.models.review_debt_record` and the v55 row field (#3041)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cw.models import DEV_QUEUE_SCHEMA_VERSION, TicketTask
from cw.models.review_debt_record import ReviewDebtRecord
from tests._review_debt_helpers import _debt_record
from tests.conftest import _make_ticket_task

_SCHEMA_V55 = 55


def test_schema_version_is_55() -> None:
    assert DEV_QUEUE_SCHEMA_VERSION == _SCHEMA_V55


@pytest.mark.parametrize("mode", ["filed", "commented", "emitted"])
def test_every_mode_validates(mode: str) -> None:
    assert _debt_record(mode=mode).mode == mode


def test_an_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _debt_record(mode="posted")


def test_an_unknown_tracker_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _debt_record(tracker="notion")


def test_an_empty_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _debt_record(key="")


def test_a_filed_record_needs_a_ticket() -> None:
    with pytest.raises(ValidationError, match="ticket_id"):
        _debt_record(ticket_id=None)


def test_an_emitted_record_may_have_no_ticket_yet() -> None:
    record = _debt_record(mode="emitted", ticket_id=None, ticket_url=None)
    assert record.ticket_id is None
    assert record.pr_url is None


def test_ticket_task_defaults_review_debt_to_empty() -> None:
    assert _make_ticket_task().review_debt == []


def test_a_v54_payload_loads_with_no_review_debt() -> None:
    payload = _make_ticket_task().model_dump(mode="json")
    payload.pop("review_debt")
    assert TicketTask.model_validate(payload).review_debt == []


def test_review_debt_round_trips_on_the_row() -> None:
    task = _make_ticket_task(review_debt=[_debt_record(pr_url="https://x/pull/1")])
    loaded = TicketTask.model_validate(task.model_dump(mode="json"))
    assert loaded.review_debt == task.review_debt
    assert isinstance(loaded.review_debt[0], ReviewDebtRecord)
