"""Unit tests for ``cw.reconcile._shared._stage_refusal`` (#2490, #2513).

The stage-refusal page, its canonical 9-field ``session.needs_attention``
payload, and the page-then-latch emit shared by the local harvest and the
idle, stalled and phantom sweeps.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.auto_dev_result import AutoDevResult, BlockedResult, Blocker
from cw.events import read_events
from cw.models import OrchestratorEventType, Session, Stage
from cw.opencode_runner import make_blocked as make_opencode_blocked
from cw.reconcile._shared import (
    SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON,
    SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON,
    SentinelRouteOutcome,
    StageRefusalPage,
    emit_stage_refusal_pages,
    page_and_latch_stage_refusal,
    stage_refusal_page,
)
from tests._reconcile_helpers import (
    _attention_events,
    _failing_record_event,
    _stage_complete_payload,
)
from tests.conftest import _make_daemon_session

_FAILING_TARGET = "cw.reconcile._shared._stage_refusal.record_event"
_PINNED_LOGGER_NAME = "cw.reconcile._shared"
_CANONICAL_KEYS = {
    "session_id",
    "session_name",
    "client",
    "ticket_id",
    "claude_session_id",
    "paused_status",
    "breadcrumbs",
    "crashed",
    "lane",
}
_REFUSED_AT_FINALIZE = SentinelRouteOutcome(
    rescued=False,
    routed=False,
    landed_terminal=False,
    stage_refused=True,
    refused_stage=Stage.FINALIZE,
)


def _session(sid: str = "ses-1") -> Session:
    return _make_daemon_session(
        id=sid,
        name=f"client-a/auto-dev/{sid}",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _stage_complete() -> AutoDevResult:
    return AutoDevResult.model_validate(_stage_complete_payload())


def _blocked_with_hint(tmp_path: Path) -> AutoDevResult:
    sentinel = make_opencode_blocked(
        ticket_id="T-1",
        worktree=tmp_path,
        reason="merge_conflict_post_push",
        stage_reached="stage4b_pr_create",
    )
    assert sentinel.blocker is not None
    blocker = sentinel.blocker.model_copy(
        update={"recovery_hint": "rebase onto main then requeue"}
    )
    return sentinel.model_copy(update={"blocker": blocker})


def _page(
    session: Session,
    sentinel: AutoDevResult | BlockedResult,
    *,
    live: bool,
    outcome: SentinelRouteOutcome | None = _REFUSED_AT_FINALIZE,
    ticket_id: str | None = "T-1",
) -> StageRefusalPage | None:
    return stage_refusal_page(
        session,
        outcome,
        ticket_id=ticket_id,
        lane="debt",
        sentinel=sentinel,
        subject="live worker" if live else "dead opencode process",
        live=live,
    )


# ---------------------------------------------------------------------------
# stage_refusal_page guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "ticket_id"),
    [
        pytest.param(None, "T-1", id="no-outcome"),
        pytest.param(
            SentinelRouteOutcome(rescued=False, routed=False, landed_terminal=False),
            "T-1",
            id="not-stage-refused",
        ),
        pytest.param(_REFUSED_AT_FINALIZE, None, id="no-ticket-id"),
        pytest.param(
            SentinelRouteOutcome(
                rescued=False,
                routed=False,
                landed_terminal=False,
                stage_refused=True,
                refused_stage=None,
            ),
            "T-1",
            id="no-refused-stage",
        ),
    ],
)
def test_stage_refusal_page_is_none_for_each_guard(
    outcome: SentinelRouteOutcome | None, ticket_id: str | None
) -> None:
    page = _page(
        _session(), _stage_complete(), live=True, outcome=outcome, ticket_id=ticket_id
    )

    assert page is None


def test_stage_refusal_page_for_an_auto_dev_result_without_blocker() -> None:
    page = _page(_session(), _stage_complete(), live=True)

    assert page is not None
    breadcrumbs = str(page.payload["breadcrumbs"])
    assert "live worker reported stage_complete at stage2_impl," in breadcrumbs
    assert "(" not in breadcrumbs.partition(", refused")[0]


def test_stage_refusal_page_for_a_blocker_without_recovery_hint(
    tmp_path: Path,
) -> None:
    sentinel = make_opencode_blocked(
        ticket_id="T-1", worktree=tmp_path, reason="merge_conflict_post_push"
    )

    page = _page(_session(), sentinel, live=True)

    assert page is not None
    breadcrumbs = str(page.payload["breadcrumbs"])
    assert "reported blocked at stage2_impl (merge_conflict_post_push)," in breadcrumbs
    assert "recovery hint" not in breadcrumbs


def test_stage_refusal_page_for_a_blocked_result_has_no_stage_reached() -> None:
    sentinel = BlockedResult(
        blocker=Blocker(stage="unknown", reason="parse_failed", details="x")
    )

    page = _page(_session(), sentinel, live=False)

    assert page is not None
    breadcrumbs = str(page.payload["breadcrumbs"])
    assert "dead opencode process reported blocked (parse_failed)," in breadcrumbs
    assert " at stage" not in breadcrumbs.partition(", refused")[0]


# ---------------------------------------------------------------------------
# Payload wording
# ---------------------------------------------------------------------------


def test_stage_mismatch_attention_payload_names_the_blocker_reason(
    tmp_path: Path,
) -> None:
    """A refused ``blocked`` result's breadcrumbs carry its blocker reason."""
    session = _session("ses-payload")

    page = stage_refusal_page(
        session,
        _REFUSED_AT_FINALIZE,
        ticket_id="T-1",
        lane="debt",
        sentinel=_blocked_with_hint(tmp_path),
        subject="dead opencode process",
        live=False,
    )

    assert page is not None
    payload = page.payload
    breadcrumbs = str(payload["breadcrumbs"])
    assert "blocked at stage4b_pr_create (merge_conflict_post_push" in breadcrumbs
    assert "rebase onto main then requeue" in breadcrumbs
    assert "dead opencode process" in breadcrumbs
    # The live row stage from the outcome.
    assert "the row is at stage finalize" in breadcrumbs
    assert "cw spawn close --confirmed-dead --requeue ses-payload" in breadcrumbs
    assert payload["ticket_id"] == "T-1"
    assert payload["claude_session_id"] is None
    assert set(payload) == _CANONICAL_KEYS
    assert payload["paused_status"] == SENTINEL_STAGE_MISMATCH_DEAD_SESSION_REASON
    assert payload["crashed"] is False
    assert payload["lane"] == "debt"
    assert payload["session_id"] == "ses-payload"
    assert payload["session_name"] == "client-a/auto-dev/ses-payload"
    assert payload["client"] == "client-a"


def test_dead_payload_breadcrumbs_are_byte_identical_to_the_local_text(
    tmp_path: Path,
) -> None:
    """The dead form concatenates to exactly the pre-#2513 local harvest text."""
    page = stage_refusal_page(
        _session("ses-x"),
        _REFUSED_AT_FINALIZE,
        ticket_id="T-1",
        lane="default",
        sentinel=_blocked_with_hint(tmp_path),
        subject="dead opencode process",
        live=False,
    )

    assert page is not None
    assert page.payload["breadcrumbs"] == (
        "dead opencode process reported blocked at stage4b_pr_create"
        " (merge_conflict_post_push); worker's recovery hint: rebase onto main"
        " then requeue, refused by the staged-advance guard: the row is at stage"
        " finalize. The result was NOT applied; the row is unchanged. To discard"
        " the dead session and rerun the row's current stage:"
        " cw spawn close --confirmed-dead --requeue ses-x"
    )


def test_live_payload_never_claims_the_worker_is_dead(tmp_path: Path) -> None:
    page = stage_refusal_page(
        _session("ses-live"),
        _REFUSED_AT_FINALIZE,
        ticket_id="T-1",
        lane="debt",
        sentinel=_blocked_with_hint(tmp_path),
        subject="live worker",
        live=True,
    )

    assert page is not None
    payload = page.payload
    breadcrumbs = str(payload["breadcrumbs"])
    assert payload["paused_status"] == SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON
    assert set(payload) == _CANONICAL_KEYS
    assert "live worker" in breadcrumbs
    assert "blocked at stage4b_pr_create (merge_conflict_post_push" in breadcrumbs
    assert "the row is at stage finalize" in breadcrumbs
    assert "may yet report a result" in breadcrumbs
    assert "cw spawn close --requeue ses-live" in breadcrumbs
    assert "--confirmed-dead" not in breadcrumbs
    assert "dead" not in breadcrumbs


# ---------------------------------------------------------------------------
# emit_stage_refusal_pages
# ---------------------------------------------------------------------------


def test_emit_pages_then_latches_a_fresh_session(tmp_config_dir: Path) -> None:
    session = _session()
    page = _page(session, _stage_complete(), live=True)
    assert page is not None

    latched = emit_stage_refusal_pages([page])

    assert latched is True
    events = read_events(
        consumer="test-emit-fresh",
        event_types=[OrchestratorEventType.SESSION_NEEDS_ATTENTION],
    )
    assert len(events) == 1
    assert events[0].correlation_id == "T-1"
    assert events[0].payload == page.payload
    assert session.last_result == {"paused_status": "sentinel_stage_mismatch_refused"}


def test_emit_merges_the_latch_into_an_existing_last_result(
    tmp_config_dir: Path,
) -> None:
    session = _session()
    session.last_result = {"paused_status": "silently_idle", "note": None}
    page = _page(session, _stage_complete(), live=True)
    assert page is not None

    assert emit_stage_refusal_pages([page]) is True

    assert session.last_result == {
        "paused_status": "silently_idle",
        "note": None,
        "sentinel_advance_refused": True,
    }


def test_emit_uses_the_pages_custom_stamp(tmp_config_dir: Path) -> None:
    session = _session()
    stamped: list[str] = []
    page = stage_refusal_page(
        session,
        _REFUSED_AT_FINALIZE,
        ticket_id="T-1",
        lane="debt",
        sentinel=_stage_complete(),
        subject="live worker",
        live=True,
        stamp=lambda s: stamped.append(s.id),
    )
    assert page is not None

    assert emit_stage_refusal_pages([page]) is True

    assert stamped == ["ses-1"]
    assert session.last_result is None


def test_emit_failure_leaves_the_session_unlatched_and_warns(
    tmp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    failures = _failing_record_event(
        monkeypatch,
        target=_FAILING_TARGET,
        event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        fail_for=lambda _payload: True,
    )
    session = _session()
    page = _page(session, _stage_complete(), live=True)
    assert page is not None

    with caplog.at_level(logging.WARNING, logger=_PINNED_LOGGER_NAME):
        latched = emit_stage_refusal_pages([page])

    assert latched is False
    assert failures == [1]
    assert session.last_result is None
    records = [
        r for r in caplog.records if "stage_mismatch_page_failed" in r.getMessage()
    ]
    assert [r.name for r in records] == [_PINNED_LOGGER_NAME]
    assert records[0].levelno == logging.WARNING


def test_one_failing_page_does_not_cancel_the_next(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_record_event(
        monkeypatch,
        target=_FAILING_TARGET,
        event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        fail_for=lambda payload: payload.get("ticket_id") == "T-fail",
    )
    failing, landing = _session("ses-fail"), _session("ses-land")
    first = _page(failing, _stage_complete(), live=True, ticket_id="T-fail")
    second = _page(landing, _stage_complete(), live=True, ticket_id="T-land")
    assert first is not None
    assert second is not None

    assert emit_stage_refusal_pages([first, second]) is True

    assert failing.last_result is None
    assert landing.last_result == {"paused_status": "sentinel_stage_mismatch_refused"}
    assert _attention_events("test-emit-multi", "T-fail") == []
    assert len(_attention_events("test-emit-multi", "T-land")) == 1


def test_emit_with_no_pages_latches_nothing() -> None:
    assert emit_stage_refusal_pages([]) is False


# ---------------------------------------------------------------------------
# page_and_latch_stage_refusal
# ---------------------------------------------------------------------------


def test_page_and_latch_pages_and_latches_a_stage_refusal(
    tmp_config_dir: Path,
) -> None:
    session = _session()

    mutated = page_and_latch_stage_refusal(
        session,
        _REFUSED_AT_FINALIZE,
        ticket_id="T-1",
        lane="debt",
        sentinel=_stage_complete(),
        subject="live worker",
        live=True,
    )

    assert mutated is True
    pages = _attention_events(
        "test-pal-paged",
        "T-1",
        paused_status=SENTINEL_STAGE_MISMATCH_LIVE_SESSION_REASON,
    )
    assert len(pages) == 1
    assert session.last_result == {"paused_status": "sentinel_stage_mismatch_refused"}


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(None, id="no-outcome"),
        pytest.param(
            SentinelRouteOutcome(rescued=False, routed=False, landed_terminal=False),
            id="not-stage-refused",
        ),
    ],
)
def test_page_and_latch_stamps_a_non_stage_refusal_silently(
    tmp_config_dir: Path, outcome: SentinelRouteOutcome | None
) -> None:
    session = _session()

    mutated = page_and_latch_stage_refusal(
        session,
        outcome,
        ticket_id="T-1",
        lane="debt",
        sentinel=_stage_complete(),
        subject="live worker",
        live=True,
    )

    assert mutated is True
    assert _attention_events("test-pal-silent", "T-1") == []
    assert session.last_result == {"paused_status": "sentinel_stage_mismatch_refused"}


@pytest.mark.parametrize(
    ("outcome", "pages_expected"),
    [
        pytest.param(_REFUSED_AT_FINALIZE, 1, id="paged"),
        pytest.param(None, 0, id="silent"),
    ],
)
def test_page_and_latch_uses_the_custom_stamp_on_both_branches(
    tmp_config_dir: Path,
    outcome: SentinelRouteOutcome | None,
    pages_expected: int,
) -> None:
    session = _session()
    stamped: list[str] = []

    mutated = page_and_latch_stage_refusal(
        session,
        outcome,
        ticket_id="T-1",
        lane="debt",
        sentinel=_stage_complete(),
        subject="live worker",
        live=True,
        stamp=lambda s: stamped.append(s.id),
    )

    assert mutated is True
    assert stamped == ["ses-1"]
    assert session.last_result is None
    assert len(_attention_events("test-pal-custom", "T-1")) == pages_expected


def test_page_and_latch_failed_page_leaves_the_session_untouched(
    tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_record_event(
        monkeypatch,
        target=_FAILING_TARGET,
        event_type=OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        fail_for=lambda _payload: True,
    )
    session = _session()
    session.last_result = {"status": "stage_complete"}

    mutated = page_and_latch_stage_refusal(
        session,
        _REFUSED_AT_FINALIZE,
        ticket_id="T-1",
        lane="debt",
        sentinel=_stage_complete(),
        subject="live worker",
        live=True,
    )

    assert mutated is False
    assert session.last_result == {"status": "stage_complete"}
