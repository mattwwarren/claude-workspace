"""Tests for the ``base-backend`` doctor check (RFC 0020 A1, #3781).

A live row with a non-null ``base_branch`` on a lane that routes a stage to a
backend that does not deliver the base would park ``base_unsupported_backend``
at spawn; doctor names it ahead of time, one FAIL per (client, lane).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cw.doctor import _deps, run_doctor
from cw.doctor.capabilities import _check_base_backend
from cw.doctor.core import _base_backend_checks
from cw.models import (
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
    DevQueueStore,
    QueueItemStatus,
    TicketTask,
)
from cw.models.orchestrator_config.constants import CLAUDE_NATIVE_BACKEND
from tests._doctor_helpers import lane_on, make_client
from tests._shared.session_seeding import _make_ticket_task

_CLIENT = "client-a"
_NAME = f"base-backend/{_CLIENT}/default"


def _row(
    ticket_id: str = "T-1",
    *,
    base: str | None = "epic/x",
    status: QueueItemStatus = QueueItemStatus.PENDING,
    client: str = _CLIENT,
) -> TicketTask:
    return _make_ticket_task(
        ticket_id=ticket_id, client=client, base_branch=base, status=status
    )


def test_a_null_base_row_yields_nothing() -> None:
    client = make_client(lane_on(OPENCODE_BACKEND), name=_CLIENT)
    assert _check_base_backend({_CLIENT: client}, [_row(base=None)]) == []


def test_a_based_row_on_a_claude_native_lane_yields_nothing() -> None:
    # The lane's REVIEW override is dropped by make_lane, so the client-level
    # REVIEW backend (codex by default) must be claude-native too.
    client = make_client(
        lane_on(CLAUDE_NATIVE_BACKEND), backend=CLAUDE_NATIVE_BACKEND, name=_CLIENT
    )
    assert _check_base_backend({_CLIENT: client}, [_row()]) == []


@pytest.mark.parametrize("backend", [OPENCODE_BACKEND, LOCAL_BACKEND, CODEX_BACKEND])
def test_a_based_row_on_a_non_delivering_lane_fails(backend: str) -> None:
    client = make_client(lane_on(backend), name=_CLIENT)

    (result,) = _check_base_backend({_CLIENT: client}, [_row()])

    assert result.name == _NAME
    assert result.ok is False
    assert result.warn is False
    assert f"to backend {backend}" in result.detail


def test_the_detail_lists_every_ticket_id_and_the_reason() -> None:
    client = make_client(lane_on(OPENCODE_BACKEND), name=_CLIENT)
    rows = [_row("T-2"), _row("T-1"), _row("T-3", base=None)]

    (result,) = _check_base_backend({_CLIENT: client}, rows)

    assert "T-1, T-2" in result.detail
    assert "T-3" not in result.detail
    assert "base_unsupported_backend" in result.detail


def test_a_terminal_row_is_ignored() -> None:
    client = make_client(lane_on(OPENCODE_BACKEND), name=_CLIENT)
    row = _row(status=QueueItemStatus.COMPLETED)
    assert _check_base_backend({_CLIENT: client}, [row]) == []


def test_a_row_whose_client_is_not_configured_is_skipped() -> None:
    client = make_client(lane_on(OPENCODE_BACKEND), name=_CLIENT)
    assert _check_base_backend({_CLIENT: client}, [_row(client="other")]) == []


def test_a_backend_without_a_declaration_is_not_an_offender_here() -> None:
    """``harness-capabilities`` already fails an undeclared backend."""
    client = make_client(
        lane_on("mystery-backend"), backend="mystery-backend", name=_CLIENT
    )
    assert _check_base_backend({_CLIENT: client}, [_row()]) == []


def test_each_lane_with_an_offender_gets_its_own_result() -> None:
    client = make_client(lane_on(OPENCODE_BACKEND), name=_CLIENT)
    other = _make_ticket_task(
        ticket_id="T-9",
        client=_CLIENT,
        base_branch="epic/x",
        lane="other",
    )
    # The unknown lane falls back to the client pipeline (codex review), which
    # does not deliver a base either.
    results = _check_base_backend({_CLIENT: client}, [_row(), other])
    assert sorted(r.name for r in results) == [
        _NAME,
        f"base-backend/{_CLIENT}/other",
    ]


def test_run_doctor_reports_a_based_row_on_a_non_delivering_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(lane_on(OPENCODE_BACKEND), name=_CLIENT)
    monkeypatch.setattr(_deps, "load_clients", lambda: {_CLIENT: client})
    monkeypatch.setattr(_deps, "load_dev_queue", lambda: DevQueueStore(tasks=[_row()]))

    report = run_doctor()

    (result,) = [c for c in report.checks if c.name == _NAME]
    assert result.ok is False


@pytest.mark.parametrize(
    "error",
    [
        OSError("unreadable"),
        json.JSONDecodeError("bad", "{", 0),
        ValidationError.from_exception_data("DevQueueStore", []),
    ],
    ids=["oserror", "json", "validation"],
)
def test_a_queue_that_will_not_load_yields_no_result_and_no_crash(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    client = make_client(lane_on(OPENCODE_BACKEND), name=_CLIENT)

    def _boom() -> DevQueueStore:
        raise error

    monkeypatch.setattr(_deps, "load_dev_queue", _boom)

    assert _base_backend_checks({_CLIENT: client}) == []
