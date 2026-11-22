"""Unit tests for the row and result lookups the tip sweep shares (#2994).

Extracted from ``_apply_events_to_store`` and shared by the locked apply and
the pre-lock REVIEW tip sweep, so the sweep plans against exactly the row the
apply will route. Pure lookups: no lock, no I/O.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from cw.dispatch.session_completions import (
    _EventRow,
    _running_row_for_event,
    _session_last_result,
)
from cw.models import (
    CwState,
    DevQueueStore,
    OrchestratorEvent,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.reconcile import _USAGE_LIMITED_MID_TURN_REASON
from tests._shared.session_seeding import _make_daemon_session, _make_ticket_task


def _event(**payload: object) -> OrchestratorEvent:
    return OrchestratorEvent(
        type=OrchestratorEventType.SESSION_COMPLETED, payload=dict(payload)
    )


def _running(ticket: str, session_id: str, *, client: str = "acme") -> Any:
    return _make_ticket_task(
        ticket_id=ticket,
        client=client,
        status=QueueItemStatus.RUNNING,
        session_id=session_id,
    )


class TestRunningRowForEvent:
    def test_a_matching_event_returns_the_row_and_its_session(self) -> None:
        task = _running("T-1", "s-1")
        store = DevQueueStore(tasks=[task])

        row = _running_row_for_event(store, _event(ticket_id="T-1", session_id="s-1"))

        assert row == _EventRow(task, "s-1")

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"crashed": True}, id="crashed-is-reconcile-owned"),
            pytest.param(
                {"reason": _USAGE_LIMITED_MID_TURN_REASON},
                id="usage-limit-act-is-reconcile-owned",
            ),
        ],
    )
    def test_a_reconcile_owned_event_matches_no_row(
        self, payload: dict[str, object]
    ) -> None:
        store = DevQueueStore(tasks=[_running("T-1", "s-1")])

        event = _event(ticket_id="T-1", session_id="s-1", **payload)

        assert _running_row_for_event(store, event) is None

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"session_id": "s-1"}, id="no-ticket-id"),
            pytest.param(
                {"session_id": "s-1", "session_name": "acme/not-an-auto-dev-name"},
                id="session-name-without-a-ticket",
            ),
            pytest.param({"ticket_id": "", "session_id": "s-1"}, id="empty-ticket-id"),
        ],
    )
    def test_an_event_that_names_no_ticket_matches_no_row(
        self, payload: dict[str, object]
    ) -> None:
        store = DevQueueStore(tasks=[_running("T-1", "s-1")])

        assert _running_row_for_event(store, _event(**payload)) is None

    @pytest.mark.parametrize(
        ("payload", "log_fragment"),
        [
            pytest.param(
                {"ticket_id": "T-1"},
                "legacy_session_completed_event_skipped",
                id="legacy-no-session-id",
            ),
            pytest.param(
                {"ticket_id": "T-1", "session_id": 7},
                "malformed_session_completed_event_skipped",
                id="non-string-session-id",
            ),
            pytest.param(
                {"ticket_id": "T-1", "session_id": ""},
                "malformed_session_completed_event_skipped",
                id="empty-session-id",
            ),
        ],
    )
    def test_a_legacy_or_malformed_session_id_is_skipped_and_logged(
        self,
        caplog: pytest.LogCaptureFixture,
        payload: dict[str, object],
        log_fragment: str,
    ) -> None:
        """Never matched on a bare ``(ticket_id, RUNNING)`` (#2219)."""
        store = DevQueueStore(tasks=[_running("T-1", "s-1")])

        with caplog.at_level(logging.DEBUG, logger="cw.dispatch"):
            row = _running_row_for_event(store, _event(**payload))

        assert row is None
        assert log_fragment in caplog.text

    def test_the_ticket_is_recovered_from_the_session_name(self) -> None:
        task = _running("T-9", "s-9")
        store = DevQueueStore(tasks=[task])

        row = _running_row_for_event(
            store, _event(session_name="acme/auto-dev/T-9", session_id="s-9")
        )

        assert row == _EventRow(task, "s-9")

    def test_duplicate_running_rows_are_told_apart_by_session_id(self) -> None:
        first = _running("T-1", "s-a")
        second = _running("T-1", "s-b")
        other_client = _running("T-1", "s-c", client="globex")
        store = DevQueueStore(tasks=[first, second, other_client])

        rows = {
            sid: _running_row_for_event(store, _event(ticket_id="T-1", session_id=sid))
            for sid in ("s-a", "s-b", "s-c")
        }

        assert rows["s-a"] == _EventRow(first, "s-a")
        assert rows["s-b"] == _EventRow(second, "s-b")
        assert rows["s-c"] == _EventRow(other_client, "s-c")

    def test_a_session_that_owns_no_running_row_matches_none(self) -> None:
        parked = _make_ticket_task(
            ticket_id="T-1",
            client="acme",
            status=QueueItemStatus.BLOCKED_ON_USER,
            session_id="s-1",
        )
        store = DevQueueStore(tasks=[parked, _running("T-1", "s-other")])

        assert (
            _running_row_for_event(store, _event(ticket_id="T-1", session_id="s-1"))
            is None
        )


class TestSessionLastResult:
    def test_the_owning_sessions_sentinel_dict_is_returned(self) -> None:
        sentinel = {"status": "shipped"}
        state = CwState(
            sessions=[
                _make_daemon_session(id="s-1", last_result=sentinel),
                _make_daemon_session(id="s-2", last_result={"status": "blocked"}),
            ]
        )

        assert _session_last_result(state, "s-1") == sentinel

    def test_no_such_session_has_no_result(self) -> None:
        state = CwState(sessions=[_make_daemon_session(id="s-1")])

        assert _session_last_result(state, "s-missing") is None

    def test_a_session_with_no_sentinel_has_no_result(self) -> None:
        state = CwState(sessions=[_make_daemon_session(id="s-1", last_result=None)])

        assert _session_last_result(state, "s-1") is None

    def test_a_non_dict_last_result_is_ignored(self) -> None:
        # ``model_copy`` skips validation, reaching the defensive isinstance guard.
        session = _make_daemon_session(id="s-1").model_copy(
            update={"last_result": "not-a-dict"}
        )

        assert _session_last_result(CwState(sessions=[session]), "s-1") is None
