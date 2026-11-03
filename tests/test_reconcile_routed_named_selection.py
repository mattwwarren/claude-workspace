"""Tests for the id-keyed routed-result selection (#2708).

``cw doctor --reap --routed-session-id`` classifies each named id as a marker
candidate, an unknown id, or an ineligible session carrying the structured
:class:`MarkerRejection` reason. The rejection is the single source of the
marker filter, so the boolean candidate filter can never drift from the
refusal reason. Pure: no transcript, no daemon, no queue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.models import (
    CwState,
    LivenessBucket,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.reconcile.routed_result_sessions import (
    ROUTED_RESULT_GRACE_BUCKETS,
    MarkerRejection,
    routed_marker_candidates,
    routed_marker_rejection,
    select_named_routed_sessions,
)
from tests._reconcile_helpers import _mk_routed_session, _routed_last_result

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import Session

_SID = "2708"
_CLIENT = "client-a"

# Each rejection and the one field change that produces it on an otherwise
# eligible routed-marker session.
_REJECTION_MUTATIONS: list[tuple[MarkerRejection, dict[str, object]]] = [
    (MarkerRejection.NOT_DAEMON, {"origin": SessionOrigin.USER}),
    (MarkerRejection.NOT_LIVE, {"status": SessionStatus.COMPLETED}),
    (MarkerRejection.ORCHESTRATOR, {"purpose": SessionPurpose.ORCHESTRATE}),
    (MarkerRejection.NO_ROUTED_RESULT, {"last_result": {"status": "stage_complete"}}),
    (
        MarkerRejection.REFUSAL_LATCHED,
        {"last_result": _routed_last_result(sentinel_advance_refused=True)},
    ),
    (MarkerRejection.NO_SURFACE, {"surface_ref": None}),
]


def _routed(tmp_path: Path, sid: str = _SID, **changes: object) -> Session:
    sess = _mk_routed_session(sid, tmp_path / f"wt-{sid}")
    for field, value in changes.items():
        setattr(sess, field, value)
    return sess


@pytest.mark.parametrize(
    ("rejection", "changes"),
    _REJECTION_MUTATIONS,
    ids=[rejection.value for rejection, _ in _REJECTION_MUTATIONS],
)
def test_marker_rejection_for_each_reason(
    tmp_path: Path, rejection: MarkerRejection, changes: dict[str, object]
) -> None:
    assert routed_marker_rejection(_routed(tmp_path, **changes)) is rejection


def test_marker_rejection_none_for_routed_session(tmp_path: Path) -> None:
    assert routed_marker_rejection(_routed(tmp_path)) is None


def test_marker_rejection_follows_documented_order(tmp_path: Path) -> None:
    """Two defects report the earlier check (origin before surface)."""
    sess = _routed(tmp_path, origin=SessionOrigin.USER, surface_ref=None)

    assert routed_marker_rejection(sess) is MarkerRejection.NOT_DAEMON


@pytest.mark.parametrize(
    "changes",
    [{}, *(changes for _, changes in _REJECTION_MUTATIONS)],
)
def test_candidate_filter_agrees_with_rejection(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    sess = _routed(tmp_path, **changes)

    found = routed_marker_candidates(
        CwState(sessions=[sess]), client=_CLIENT, ticket_id=_SID
    )

    assert (found == []) is (routed_marker_rejection(sess) is not None)


def test_select_named_unknown_id(tmp_path: Path) -> None:
    state = CwState(sessions=[_routed(tmp_path)])

    selection = select_named_routed_sessions(state, ["ghost"])

    assert selection.candidates == []
    assert selection.unknown == ["ghost"]
    assert selection.ineligible == []


def test_select_named_ineligible_carries_rejection(tmp_path: Path) -> None:
    sess = _routed(tmp_path, status=SessionStatus.COMPLETED)

    selection = select_named_routed_sessions(CwState(sessions=[sess]), [_SID])

    assert selection.candidates == []
    assert selection.unknown == []
    ((ineligible_session, rejection),) = selection.ineligible
    assert ineligible_session.id == _SID
    assert rejection is MarkerRejection.NOT_LIVE


def test_select_named_dedupes_and_keeps_input_order(tmp_path: Path) -> None:
    first = _routed(tmp_path, "first")
    second = _routed(tmp_path, "second")
    state = CwState(sessions=[first, second])

    selection = select_named_routed_sessions(
        state, ["second", "ghost", "first", "second", "ghost"]
    )

    assert [s.id for s in selection.candidates] == ["second", "first"]
    assert selection.unknown == ["ghost"]


def test_select_named_matches_exact_id_not_name(tmp_path: Path) -> None:
    sess = _routed(tmp_path)

    selection = select_named_routed_sessions(CwState(sessions=[sess]), [sess.name])

    assert selection.candidates == []
    assert selection.unknown == [sess.name]


def test_grace_buckets_unchanged() -> None:
    """The debounced finder still waits for the 30m bucket (ADR-0014 inv. 1)."""
    assert (
        frozenset({LivenessBucket.STALE_30M, LivenessBucket.STALE_45M})
        == ROUTED_RESULT_GRACE_BUCKETS
    )
