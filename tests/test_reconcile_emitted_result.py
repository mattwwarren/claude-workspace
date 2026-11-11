"""Unit tests for cw.reconcile.emitted_result (#2699).

Disposition of a dead LOCAL session whose worker already ran ``cw result emit``:
route the staged result to its row and complete the session, page a permanent
stage mismatch, and fall through to git synthesis for everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cw.dev_queue import load_dev_queue, save_dev_queue
from cw.models import (
    DEFAULT_LANE,
    DevQueueStore,
    LastResultSource,
    QueueItemStatus,
    Session,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import ProposedAction, ReapCandidate
from cw.reconcile._shared import _stamp_sentinel_partial_route_consumed
from cw.reconcile.emitted_result import (
    EmittedResultDisposition,
    RefusalPageContext,
    dispose_emitted_result,
    handle_emitted_result,
    route_emitted_result,
    stamp_harvest_completed,
)
from tests._clients_yaml import staged_client, write_clients_yaml
from tests._reconcile_helpers import _stage_complete_payload
from tests._shared.session_seeding import _make_daemon_session

_TICKET = "t2699-unit"
_SUBJECT = "dead opencode process"
_VALIDATION_ATTEMPT_CAP = 3


def _blocked_payload() -> dict[str, Any]:
    """The parser-synthesized blocked shape: status=blocked, no schema_version."""
    return {
        "status": "blocked",
        "blocker": {"stage": "stage2_impl", "reason": "status_unknown", "details": ""},
    }


def _session(
    last_result: dict[str, Any] | None,
    *,
    source: LastResultSource = LastResultSource.EMIT_CLI,
) -> Session:
    session = _make_daemon_session(id=_TICKET, name=f"client-a/auto-dev/{_TICKET}")
    session.last_result = last_result
    session.last_result_source = source
    return session


def _candidate(ticket_id: str | None = _TICKET) -> ReapCandidate:
    return ReapCandidate(
        session_id=_TICKET,
        proposed_action=ProposedAction.HARVEST_LOCAL_COMPLETE,
        ticket_id=ticket_id,
        lane=DEFAULT_LANE,
        client="client-a",
        worktree_path=None,
    )


def _seed_row(
    status: QueueItemStatus,
    stage: Stage,
    *,
    session_id: str | None = _TICKET,
    attempts: int = 0,
) -> None:
    write_clients_yaml(staged_client("client-a", sentinel_mismatch_veto=True))
    save_dev_queue(
        DevQueueStore(
            tasks=[
                TicketTask(
                    ticket_id=_TICKET,
                    client="client-a",
                    status=status,
                    session_id=session_id,
                    stage=stage,
                    attempts=attempts,
                )
            ]
        )
    )


def _row() -> TicketTask:
    return next(t for t in load_dev_queue().tasks if t.ticket_id == _TICKET)


def test_other_authority_result_falls_through(tmp_config_dir: object) -> None:
    session = _session(
        _stage_complete_payload(), source=LastResultSource.STOP_HOOK_HARVEST
    )

    assert dispose_emitted_result(session, _candidate(), subject=_SUBJECT) is None


def test_non_terminal_result_falls_through(tmp_config_dir: object) -> None:
    session = _session({"paused_status": "silently_idle"})

    assert dispose_emitted_result(session, _candidate(), subject=_SUBJECT) is None


def test_unreconstructable_staged_result_falls_through(
    tmp_config_dir: object,
) -> None:
    session = _session({"status": "shipped", "schema_version": 4})

    assert dispose_emitted_result(session, _candidate(), subject=_SUBJECT) is None


def test_no_ticket_has_nothing_to_route(tmp_config_dir: object) -> None:
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = dispose_emitted_result(session, _candidate(None), subject=_SUBJECT)

    assert result == EmittedResultDisposition(complete=True)


def test_consumed_result_completes_without_routing_again(
    tmp_config_dir: object,
) -> None:
    """A #2458 partial route already consumed it: the row must not advance twice."""
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})
    _stamp_sentinel_partial_route_consumed(session)

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result == EmittedResultDisposition(complete=True)
    row = _row()
    assert (row.status, row.stage) == (QueueItemStatus.RUNNING, Stage.IMPL)


def test_staged_result_advances_the_row_and_completes(tmp_config_dir: object) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result == EmittedResultDisposition(complete=True)
    assert _row().stage == Stage.REVIEW


def test_crash_between_queue_save_and_session_save_does_not_advance_twice(
    tmp_config_dir: object,
) -> None:
    """The row was advanced (session_id cleared) but the session never saved.

    The next tick finds the session ACTIVE with an unconsumed result; the row no
    longer carries its session id, so the session completes and the row stays
    exactly where the first route left it.
    """
    _seed_row(QueueItemStatus.PENDING, Stage.REVIEW, session_id=None)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result == EmittedResultDisposition(complete=True)
    row = _row()
    assert (row.status, row.stage, row.session_id) == (
        QueueItemStatus.PENDING,
        Stage.REVIEW,
        None,
    )


def test_row_raced_to_terminal_completes(tmp_config_dir: object) -> None:
    _seed_row(QueueItemStatus.COMPLETED, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result == EmittedResultDisposition(complete=True)
    assert _row().status == QueueItemStatus.COMPLETED


def test_blocked_result_that_lands_the_row_failed_completes(
    tmp_config_dir: object,
) -> None:
    """A dead worker will never produce another leg, so FAILED is final."""
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL, attempts=_VALIDATION_ATTEMPT_CAP)
    session = _session(_blocked_payload())

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result == EmittedResultDisposition(complete=True)
    assert _row().status == QueueItemStatus.FAILED


def test_blocked_result_under_the_cap_requeues_the_row_and_completes(
    tmp_config_dir: object,
) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL, attempts=0)
    session = _session(_blocked_payload())

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result == EmittedResultDisposition(complete=True)
    row = _row()
    assert row.status == QueueItemStatus.PENDING
    assert row.session_id is None


def test_stage_mismatch_returns_a_page_built_from_the_emitted_result(
    tmp_config_dir: object,
) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.REVIEW)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result is not None
    assert result.complete is False
    assert result.page is not None
    assert "stage_complete at stage2_impl" in str(result.page.payload["breadcrumbs"])
    row = _row()
    assert (row.status, row.stage) == (QueueItemStatus.RUNNING, Stage.REVIEW)


def test_row_moved_on_non_terminal_falls_through(tmp_config_dir: object) -> None:
    """A PENDING row still carrying the session id is redispatch-eligible, not done."""
    _seed_row(QueueItemStatus.PENDING, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = dispose_emitted_result(session, _candidate(), subject=_SUBJECT)

    assert result is None
    row = _row()
    assert (row.status, row.stage) == (QueueItemStatus.PENDING, Stage.IMPL)


def test_handle_returns_false_and_mutates_nothing_on_fall_through(
    tmp_config_dir: object,
) -> None:
    session = _session(_stage_complete_payload(), source=LastResultSource.GIT_SYNTHESIS)
    harvested: list[str] = []
    events: list[dict[str, object]] = []
    pages: list[Any] = []

    handled = handle_emitted_result(
        session,
        _candidate(),
        subject=_SUBJECT,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        harvested_ticket_ids=harvested,
        pending_events=events,
        refusal_pages=pages,
    )

    assert handled is False
    assert (harvested, events, pages) == ([], [], [])
    assert session.status == SessionStatus.ACTIVE


def test_handle_queues_the_page_for_a_stage_mismatch(tmp_config_dir: object) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.REVIEW)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})
    harvested: list[str] = []
    events: list[dict[str, object]] = []
    pages: list[Any] = []

    handled = handle_emitted_result(
        session,
        _candidate(),
        subject=_SUBJECT,
        now=datetime(2026, 1, 2, tzinfo=UTC),
        harvested_ticket_ids=harvested,
        pending_events=events,
        refusal_pages=pages,
    )

    assert handled is True
    assert len(pages) == 1
    assert (harvested, events) == ([], [])
    assert session.status == SessionStatus.ACTIVE


def test_stamp_harvest_completed_without_a_ticket_id() -> None:
    session = _session(None)
    harvested: list[str] = []
    events: list[dict[str, object]] = []

    stamp_harvest_completed(
        session,
        _candidate(None),
        now=datetime(2026, 1, 2, tzinfo=UTC),
        harvested_ticket_ids=harvested,
        pending_events=events,
    )

    assert session.status == SessionStatus.COMPLETED
    assert harvested == []
    assert events == [
        {"session_id": _TICKET, "session_name": session.name, "crashed": False}
    ]


def test_stamp_harvest_completed_records_the_ticket() -> None:
    session = _session(None)
    harvested: list[str] = []
    events: list[dict[str, object]] = []

    stamp_harvest_completed(
        session,
        _candidate(),
        now=datetime(2026, 1, 2, tzinfo=UTC),
        harvested_ticket_ids=harvested,
        pending_events=events,
    )

    assert harvested == [_TICKET]
    assert events[0]["ticket_id"] == _TICKET


# --- route_emitted_result: the shared primitive (#2704) -------------------

_LANE_X = "lane-x"
_BOTH_POLICIES = [
    pytest.param(True, RefusalPageContext(_LANE_X, _SUBJECT), id="harvest"),
    pytest.param(False, None, id="spawn-close"),
]


@pytest.mark.parametrize(("complete_unrouted", "page"), _BOTH_POLICIES)
def test_route_staged_result_advances_row_under_both_policies(
    tmp_config_dir: object,
    complete_unrouted: bool,
    page: RefusalPageContext | None,
) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = route_emitted_result(
        session,
        ticket_id=_TICKET,
        complete_unrouted=complete_unrouted,
        page=page,
    )

    assert result == EmittedResultDisposition(complete=True)
    assert _row().stage == Stage.REVIEW


@pytest.mark.parametrize(("complete_unrouted", "page"), _BOTH_POLICIES)
def test_route_raced_terminal_row_completes_under_both_policies(
    tmp_config_dir: object,
    complete_unrouted: bool,
    page: RefusalPageContext | None,
) -> None:
    _seed_row(QueueItemStatus.COMPLETED, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = route_emitted_result(
        session,
        ticket_id=_TICKET,
        complete_unrouted=complete_unrouted,
        page=page,
    )

    assert result == EmittedResultDisposition(complete=True)


@pytest.mark.parametrize(("complete_unrouted", "page"), _BOTH_POLICIES)
def test_route_blocked_result_landing_failed_completes_under_both_policies(
    tmp_config_dir: object,
    complete_unrouted: bool,
    page: RefusalPageContext | None,
) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL, attempts=_VALIDATION_ATTEMPT_CAP)
    session = _session(_blocked_payload())

    result = route_emitted_result(
        session,
        ticket_id=_TICKET,
        complete_unrouted=complete_unrouted,
        page=page,
    )

    assert result == EmittedResultDisposition(complete=True)
    assert _row().status == QueueItemStatus.FAILED


@pytest.mark.parametrize(("complete_unrouted", "page"), _BOTH_POLICIES)
def test_route_parked_row_rescued_by_staged_result_under_both_policies(
    tmp_config_dir: object,
    complete_unrouted: bool,
    page: RefusalPageContext | None,
) -> None:
    """A parked row still carrying the session id is rescued (#918/#990)."""
    _seed_row(QueueItemStatus.BLOCKED_ON_USER, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = route_emitted_result(
        session,
        ticket_id=_TICKET,
        complete_unrouted=complete_unrouted,
        page=page,
    )

    assert result == EmittedResultDisposition(complete=True)
    assert _row().stage == Stage.REVIEW


@pytest.mark.parametrize(("complete_unrouted", "page"), _BOTH_POLICIES)
def test_route_falls_through_on_non_emit_source_under_both_policies(
    tmp_config_dir: object,
    complete_unrouted: bool,
    page: RefusalPageContext | None,
) -> None:
    session = _session(
        _stage_complete_payload(), source=LastResultSource.STOP_HOOK_HARVEST
    )

    assert (
        route_emitted_result(
            session,
            ticket_id=_TICKET,
            complete_unrouted=complete_unrouted,
            page=page,
        )
        is None
    )


@pytest.mark.parametrize(("complete_unrouted", "page"), _BOTH_POLICIES)
def test_route_falls_through_on_row_moved_non_terminal_under_both_policies(
    tmp_config_dir: object,
    complete_unrouted: bool,
    page: RefusalPageContext | None,
) -> None:
    _seed_row(QueueItemStatus.PENDING, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    assert (
        route_emitted_result(
            session,
            ticket_id=_TICKET,
            complete_unrouted=complete_unrouted,
            page=page,
        )
        is None
    )
    assert _row().stage == Stage.IMPL


def test_route_consumed_result_with_harvest_policy_completes(
    tmp_config_dir: object,
) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})
    _stamp_sentinel_partial_route_consumed(session)

    result = route_emitted_result(
        session,
        ticket_id=_TICKET,
        complete_unrouted=True,
        page=RefusalPageContext(_LANE_X, _SUBJECT),
    )

    assert result == EmittedResultDisposition(complete=True)
    assert _row().stage == Stage.IMPL


def test_route_consumed_result_with_spawn_policy_falls_through_untouched(
    tmp_config_dir: object,
) -> None:
    """Spawn close cancels a row bound to a consumed result (#317 guard)."""
    _seed_row(QueueItemStatus.RUNNING, Stage.IMPL)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})
    _stamp_sentinel_partial_route_consumed(session)

    result = route_emitted_result(
        session, ticket_id=_TICKET, complete_unrouted=False, page=None
    )

    assert result is None
    row = _row()
    assert (row.status, row.stage) == (QueueItemStatus.RUNNING, Stage.IMPL)


def test_route_no_ticket_completes_only_under_harvest_policy(
    tmp_config_dir: object,
) -> None:
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    assert route_emitted_result(
        session,
        ticket_id=None,
        complete_unrouted=True,
        page=RefusalPageContext(_LANE_X, _SUBJECT),
    ) == EmittedResultDisposition(complete=True)
    assert (
        route_emitted_result(
            session, ticket_id=None, complete_unrouted=False, page=None
        )
        is None
    )


def test_route_stage_mismatch_with_page_context_owes_page(
    tmp_config_dir: object,
) -> None:
    _seed_row(QueueItemStatus.RUNNING, Stage.REVIEW)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = route_emitted_result(
        session,
        ticket_id=_TICKET,
        complete_unrouted=True,
        page=RefusalPageContext(_LANE_X, _SUBJECT),
    )

    assert result is not None
    assert result.complete is False
    assert result.page is not None
    assert result.page.payload["lane"] == _LANE_X
    assert _SUBJECT in str(result.page.payload["breadcrumbs"])


def test_route_stage_mismatch_without_page_context_falls_through(
    tmp_config_dir: object,
) -> None:
    """Spawn close does not page: returns None and leaves the row unchanged."""
    _seed_row(QueueItemStatus.RUNNING, Stage.REVIEW)
    session = _session({**_stage_complete_payload(), "ticket_id": _TICKET})

    result = route_emitted_result(
        session, ticket_id=_TICKET, complete_unrouted=False, page=None
    )

    assert result is None
    row = _row()
    assert (row.status, row.stage) == (QueueItemStatus.RUNNING, Stage.REVIEW)
