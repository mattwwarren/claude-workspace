"""The operator-authority alternate through the resume gate (#3465).

A row approval bound to a fingerprint the draft has since drifted from still
approves while no operator-authority comment is newer than it and the ticket
body is unchanged (the persisted ``body_sha``). Every case runs through
``resume_gate`` against the scripted runner and ``gh``: the alternate takes the
fast path (no turn); a newer operator comment, in either form of the
``--post-marker`` audit comment included, sends the round to the scan and a
mismatch park; a row delta never takes it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from freezegun import freeze_time

from cw.cli.dev_queue._plan_marker import _plan_approved_marker
from cw.codex_plan._result import PLAN_MD_RELATIVE_PATH, write_plan_draft
from cw.codex_plan._resume import APPROVED_FINGERPRINT_MATCH, OPERATOR_APPROVAL_NO_DELTA
from cw.codex_plan._revise import REVISION_HEADER
from cw.events import read_events
from cw.models import OrchestratorEventType
from cw.models.operator_resolution import OperatorResolution
from cw.plan_fingerprint import (
    LeadingBlock,
    compute_body_sha,
    compute_plan_draft_fingerprint,
)
from tests._shared.ticket_context import issue_envelope_json, observed_issue_envelope
from tests._tool_fakes import revise_turn_result, stdin_contains
from tests.test_codex_plan_revise import LARGE
from tests.test_codex_plan_run import (
    LARGE_PLAN,
    TICKET,
    Harness,
    add_stations,
    add_turn,
    harness,
    stage_events,
)

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")
__all__ = ["harness"]

_VIEW = ("issue", "view", TICKET, "--json", "title,body,url,comments")
_NOW = "2026-11-10 12:00:00"
_APPROVED_AT = datetime(2026, 11, 10, tzinfo=UTC)
_BEFORE = "2026-11-09T09:00:00Z"
_AFTER = "2026-11-11T09:00:00Z"
_ORIGINALLY_APPROVED = LARGE_PLAN.replace("Add the widget.", "Add the original widget.")
#: A plan a resolutions revision may return: every required heading, plus the
#: conformance section the revision of a row delivery must carry.
FULL_PLAN = LARGE.replace(
    "## Ambiguities",
    "## Pre-flight Resolution Conformance\n\n- R1 [SATISFIED]\n\n## Ambiguities",
)
_MISMATCH = "plan_approval_fingerprint_mismatch"
_SKIPPED = "s1_ambiguity_scan_skipped"


def _live_body_sha() -> str:
    return compute_body_sha(observed_issue_envelope()["body"])


def _drifted(
    h: Harness,
    *,
    comments: tuple[tuple[str, str, str], ...] = (),
    plan: str = LARGE_PLAN,
) -> None:
    """A draft whose fingerprint differs from the row's approval, body unchanged."""
    book = LeadingBlock(
        applied="none",
        last_evaluated=f"operator_comment=none|body_sha={_live_body_sha()}",
    )
    write_plan_draft(h.worktree, plan, book=book)
    h.gh.reply(*_VIEW, stdout=issue_envelope_json(comments))


def _approval() -> dict[str, object]:
    return {
        "plan_approved_at": _APPROVED_AT,
        "plan_approved_fingerprint": compute_plan_draft_fingerprint(
            _ORIGINALLY_APPROVED
        ),
    }


def _skipped() -> list[dict[str, Any]]:
    return [p for _, p, _ in stage_events() if p.get("stage") == _SKIPPED]


def _revoked() -> list[dict[str, Any]]:
    return [
        e.payload
        for e in read_events(event_types=[OrchestratorEventType.PLAN_APPROVAL_REVOKED])
    ]


@freeze_time(_NOW)
def test_a_drifted_row_approval_with_no_newer_comment_takes_the_fast_path(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approval())
    _drifted(h)
    add_stations(h.runner)
    result = h.run()
    assert h.plan_calls == []
    assert result.status == "stage_complete"
    assert (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    (event,) = _skipped()
    assert event["reason"] == OPERATOR_APPROVAL_NO_DELTA
    assert event["reason"] != APPROVED_FINGERPRINT_MATCH


@freeze_time(_NOW)
def test_an_operator_comment_older_than_the_approval_keeps_the_alternate(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approval())
    _drifted(h, comments=(("c1", "an earlier operator note", _BEFORE),))
    add_stations(h.runner)
    result = h.run()
    assert h.plan_calls == []
    assert result.status == "stage_complete"
    assert [e["reason"] for e in _skipped()] == [OPERATOR_APPROVAL_NO_DELTA]


def _assert_scan_then_mismatch_park(h: Harness) -> None:
    result = h.run()
    assert _skipped() == []
    assert len(h.plan_calls) == 1
    assert result.status == "plan_pending_approval"
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    mismatch = [f for f in result.friction_highlights if f.startswith(_MISMATCH)]
    assert mismatch == [
        f"{_MISMATCH}: approved={compute_plan_draft_fingerprint(_ORIGINALLY_APPROVED)} "
        f"current={compute_plan_draft_fingerprint(LARGE_PLAN)}"
    ]


@pytest.mark.parametrize(
    "comment",
    [
        pytest.param("a later operator note", id="operator-comment"),
        pytest.param(
            _plan_approved_marker(compute_plan_draft_fingerprint(LARGE_PLAN)),
            id="post-marker-bound",
        ),
        pytest.param(_plan_approved_marker(None), id="post-marker-bare"),
    ],
)
@freeze_time(_NOW)
def test_a_newer_operator_comment_ends_the_alternate(
    harness: Callable[..., Harness], comment: str
) -> None:
    """A ``--post-marker`` audit comment is an operator comment: parity with claude."""
    h = harness(task_updates=_approval())
    _drifted(h, comments=(("c1", comment, _AFTER),))
    add_stations(add_turn(h.runner, LARGE_PLAN, tier="large"))
    _assert_scan_then_mismatch_park(h)


@freeze_time(_NOW)
def test_a_changed_ticket_body_ends_the_alternate(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approval())
    _drifted(h)
    h.gh.reply(*_VIEW, stdout=issue_envelope_json(body="The body was edited."))
    add_stations(add_turn(h.runner, LARGE_PLAN, tier="large"))
    _assert_scan_then_mismatch_park(h)


@freeze_time(_NOW)
def test_a_row_delta_never_takes_the_alternate(
    harness: Callable[..., Harness],
) -> None:
    """A delivered row revision keeps exact fingerprint equality (no alternate)."""
    row = OperatorResolution(
        text="use redis", recorded_at=_APPROVED_AT, actor="op", round=1
    )
    h = harness(task_updates={**_approval(), "operator_resolutions": [row]})
    _drifted(h, plan=FULL_PLAN)
    revised = FULL_PLAN.replace("Add the widget.", "Add the redis widget.")
    h.runner.add(stdin_contains(REVISION_HEADER), revise_turn_result(revised))
    add_stations(add_turn(h.runner, revised, tier="large"))
    result = h.run()
    assert _skipped() == []
    assert len([c for c in h.runner.calls if REVISION_HEADER in str(c["stdin"])]) == 1
    assert len(_revoked()) == 1
    assert len(h.plan_calls) == 1
    assert result.status == "plan_pending_approval"
