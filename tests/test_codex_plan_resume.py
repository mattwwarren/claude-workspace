"""Tests for cw.codex_plan._resume: the codex PLAN resumed round (#3464).

Run-level tests drive ``run_codex_plan_stage`` through the run tests' own
harness (GitHub target, so the GitHub rule set), scripting the snapshot read
``gh issue view 42 --json title,body,url,comments`` from the observed
envelope. Unit-level tests drive :func:`resume_gate` with a tracker state the
``_tracker`` functions computed under an explicit :class:`SnapshotContext`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from cw.codex_plan._exits import _Exit
from cw.codex_plan._result import PLAN_MD_RELATIVE_PATH, RESUME_FRICTION, ResumeDraft
from cw.codex_plan._resume import (
    APPROVED_FINGERPRINT_MATCH,
    APPROVED_FINGERPRINT_MISMATCH,
    NO_APPROVAL_EVIDENCE,
    NO_SCOPE_TIER,
    RoundBook,
    fast_path_reason,
    resume_gate,
    tracker_state,
)
from cw.codex_plan._revise import REVISION_HEADER
from cw.codex_plan._run import _McpTarget, _record_ticket_snapshot, _Run
from cw.codex_plan._settle import DEFERRED_STUB
from cw.codex_review import CODEX_MODEL_CAPACITY
from cw.codex_runner import CodexRunResult
from cw.context_queue_metadata import operator_resolutions_source
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.exceptions import CwError, LockTimeoutError
from cw.gh import AGENT_COMMENT_MARKER
from cw.models import OrchestratorEventType
from cw.models.operator_resolution import OperatorResolution
from cw.plan_files import parse_plan_files_modified, parse_plan_lines_estimate
from cw.plan_fingerprint import (
    LeadingBlock,
    ResolutionsAttempt,
    compute_body_sha,
    compute_plan_draft_fingerprint,
    parse_leading_block,
    render_leading_block,
)
from cw.ticket_snapshot import SnapshotContext, write_snapshot
from tests._backend_mcp_helpers import codex_tracker_client
from tests._shared.ticket_context import (
    _AUTHORED_BODY,
    issue_envelope_json,
    make_comment,
    make_snapshot_from,
)
from tests._ticket_view_helpers import make_task
from tests._tool_fakes import (
    ScriptedCodexRunner,
    plan_sentinel,
    plan_turn_result,
    revise_turn_result,
    station_findings_doc,
    stdin_contains,
)
from tests.test_codex_plan_run import (
    PLAN_TURN,
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

MARKER = "<!-- auto-dev-preflight-resolutions -->"
_VIEW = ("issue", "view", TICKET, "--json", "title,body,url,comments")
_APPROVED_AT = datetime(2026, 11, 10, tzinfo=UTC)
_LEASE_FUTURE = "2026-11-10T12:10:00Z"
_LEASE_PAST = "2026-11-10T11:00:00Z"
_NOW = "2026-11-10 12:00:00"

BODY = (
    "## Summary\n\nAdd the widget.\n\n**Scope tier:** large\n\n"
    "## Patterns Found\n\n- none\n\n## Touch-point Contract\n\n- none\n\n"
    "## Files Modified\n\n- src/cw/widget.py (~40 lines)\n"
    "- src/cw/other.py (~-5 lines net, plus ~9 added)\n\n"
    "## Non-Goals\n\n- none\n\n"
    "## Self-Verified Premises\n\n- X holds — Evidence: y\n\n"
    "## Pre-flight Resolution Conformance\n\n- R1 [SATISFIED]\n\n"
    "## Ambiguities\n\nNO_AMBIGUITIES\n"
)
REVISED = BODY.replace("Add the widget.", "Add the revised widget.")
_PARK_TURN: dict[str, Any] = {
    "status": "ambiguities_pending_resolution",
    "ambiguities": [{"question": "Which store?"}],
    "next_actions": ["user_resolve_ambiguities"],
}
_NONE_FP = f"operator_comment=none|body_sha={compute_body_sha(_AUTHORED_BODY)}"


# --- helpers -------------------------------------------------------------------


def _seed(h: Harness, block: LeadingBlock, body: str = BODY) -> None:
    path = h.draft_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_leading_block(block) + body, encoding="utf-8")


def _book(h: Harness) -> LeadingBlock:
    return parse_leading_block(h.draft_path().read_text(encoding="utf-8"))


def _snap(
    h: Harness, comments: tuple[tuple[str, str, str], ...] = (), body: str | None = None
) -> None:
    h.gh.reply(*_VIEW, stdout=issue_envelope_json(comments, body=body))


def _approve(body: str = BODY) -> dict[str, object]:
    return {
        "plan_approved_at": _APPROVED_AT,
        "plan_approved_fingerprint": compute_plan_draft_fingerprint(body),
    }


def _rows(*texts: str) -> dict[str, object]:
    return {
        "operator_resolutions": [
            OperatorResolution(text=t, recorded_at=_APPROVED_AT, actor="op", round=i)
            for i, t in enumerate(texts, 1)
        ]
    }


def _revision(h: Harness, text: str | CodexRunResult = REVISED) -> None:
    reply = text if isinstance(text, CodexRunResult) else revise_turn_result(text)
    h.runner.add(stdin_contains(REVISION_HEADER), reply)


def _revision_calls(h: Harness) -> list[dict[str, object]]:
    return [c for c in h.runner.calls if REVISION_HEADER in str(c["stdin"])]


def _revoked() -> list[dict[str, Any]]:
    return [
        e.payload
        for e in read_events(event_types=[OrchestratorEventType.PLAN_APPROVAL_REVOKED])
    ]


def _row() -> Any:
    (task,) = load_dev_queue().tasks
    return task


def _marker_comment(comment_id: str, created: str) -> tuple[str, str, str]:
    return (comment_id, f"Answers\n{MARKER}\nR1: use redis", created)


def _assert_parked_unobserved(
    h: Harness, result: Any, applied: str, before: str
) -> None:
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == "agent_block"
    assert result.blocker.retry_eligible is True
    assert result.stage_reached == "stage1_plan"
    assert applied in result.blocker.details
    assert "approval stands" in result.blocker.details
    assert "re-dispatch once the ticket snapshot" in result.blocker.details
    assert "cw dev-queue resolve" in result.blocker.details
    assert any(
        f.startswith("resolutions-source-unobserved: ")
        for f in result.friction_highlights
    )
    assert h.plan_calls == []
    assert h.runner.calls == []
    assert _revoked() == []
    assert _row().plan_approved_fingerprint is not None
    assert h.draft_path().read_text(encoding="utf-8") == before
    assert result.plan_draft_fingerprint == compute_plan_draft_fingerprint(before)


# --- fast path -----------------------------------------------------------------


@freeze_time(_NOW)
def test_the_fast_path_takes_the_approved_draft_with_no_turn(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock(scan_round=1, applied="none"))
    _snap(h)
    add_stations(h.runner)
    result = h.run()
    assert h.plan_calls == []
    assert len(h.runner.calls) == 2
    skipped = [
        p
        for t, p, _ in stage_events()
        if p.get("stage", "") == "s1_ambiguity_scan_skipped"
    ]
    assert skipped == [
        {
            "session_id": "sess-1",
            "ticket_id": TICKET,
            "stage": "s1_ambiguity_scan_skipped",
            "prev_stage": "s1_plan_generated",
            "reason": "approved_fingerprint_match",
            "started_at": "2026-11-10T12:00:00Z",
        }
    ]
    assert not [
        p for _, p, _ in stage_events() if "scan_complete" in p.get("stage", "")
    ]
    assert result.status == "stage_complete"
    assert result.plan_draft_fingerprint == compute_plan_draft_fingerprint(BODY)
    assert result.plan_source == "generated"
    assert result.scope.tier == "large"
    assert result.scope.files == len(parse_plan_files_modified(BODY)) == 2
    assert result.scope.lines_estimate == parse_plan_lines_estimate(BODY) == 35
    assert result.health.lowest_agent_confidence is None
    assert RESUME_FRICTION in result.friction_highlights
    assert result.resolution_consumed is False
    plan = (h.worktree / PLAN_MD_RELATIVE_PATH).read_text(encoding="utf-8")
    assert plan.endswith(BODY)


def test_a_fast_path_station_block_keeps_the_body(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock())
    _snap(h)
    add_stations(
        h.runner,
        soundness=station_findings_doc("Plan Soundness Reviewer", severity="MUST_FIX"),
    )
    result = h.run()
    assert result.status == "blocked"
    assert h.plan_calls == []
    assert result.plan_draft_fingerprint == compute_plan_draft_fingerprint(BODY)
    assert h.draft_path().read_text(encoding="utf-8").endswith(BODY)


@pytest.mark.parametrize(
    ("updates", "body"),
    [
        (
            {"plan_approved_at": _APPROVED_AT, "plan_approved_fingerprint": "c" * 64},
            BODY,
        ),
        ({}, BODY),
        (
            _approve(BODY.replace("**Scope tier:** large", "")),
            BODY.replace("**Scope tier:** large", ""),
        ),
    ],
    ids=["mismatch", "no-approval", "no-tier"],
)
def test_without_the_fast_path_the_scan_turn_runs(
    harness: Callable[..., Harness], updates: dict[str, object], body: str
) -> None:
    h = harness(task_updates=updates)
    _seed(h, LeadingBlock(), body)
    _snap(h)
    add_stations(add_turn(h.runner, body, tier="large"))
    h.run()
    assert len(h.plan_calls) == 1


def test_fast_path_reason_names_each_case() -> None:
    task = make_task()
    assert fast_path_reason(task, BODY) == NO_APPROVAL_EVIDENCE
    approved = task.model_copy(update=_approve())
    assert fast_path_reason(approved, BODY) == APPROVED_FINGERPRINT_MATCH
    assert fast_path_reason(approved, REVISED) == APPROVED_FINGERPRINT_MISMATCH
    no_tier = BODY.replace("**Scope tier:** large", "")
    assert fast_path_reason(task.model_copy(update=_approve(no_tier)), no_tier) == (
        NO_SCOPE_TIER
    )


@pytest.mark.parametrize("applied", [None, "none", "row:1:" + "a" * 64])
def test_an_unavailable_snapshot_runs_the_scan_and_carries_the_stale_line(
    harness: Callable[..., Harness], applied: str | None
) -> None:
    h = harness(task_updates=_approve())
    stale = f"operator_comment=old|body_sha={'b' * 64}"
    _seed(h, LeadingBlock(scan_round=1, last_evaluated=stale, applied=applied))
    add_stations(add_turn(h.runner, BODY, tier="large", **_PARK_TURN))
    result = h.run()
    assert len(h.plan_calls) == 1
    assert result.status == "ambiguities_pending_resolution"
    book = _book(h)
    assert book.last_evaluated == stale
    assert book.scan_round == 2
    assert not [p for _, p, _ in stage_events() if "skipped" in p.get("stage", "")]


# --- round cap -----------------------------------------------------------------


@pytest.mark.parametrize(("seeded", "written"), [(0, 1), (1, 2)])
def test_an_exit_park_increments_the_round(
    harness: Callable[..., Harness], seeded: int, written: int
) -> None:
    h = harness()
    _seed(h, LeadingBlock(scan_round=seeded, last_evaluated=_NONE_FP, applied="none"))
    _snap(h)
    add_stations(add_turn(h.runner, BODY, tier="large", **_PARK_TURN))
    result = h.run()
    assert result.status == "ambiguities_pending_resolution"
    first = h.draft_path().read_text(encoding="utf-8").splitlines()[0]
    assert first == f"<!-- plan-stage-scan-round: {written} -->"


@freeze_time(_NOW)
def test_the_round_cap_blocks_unconverged_through_the_consolidated_park(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _seed(h, LeadingBlock(scan_round=2, last_evaluated=_NONE_FP, applied="none"))
    _snap(h)
    add_stations(add_turn(h.runner, BODY, tier="large", **_PARK_TURN))
    result = h.run()
    assert result.status == "blocked"
    assert result.blocker is not None
    assert result.blocker.reason == "ambiguity_scan_unconverged"
    assert result.blocker.retry_eligible is True
    assert result.stage_reached == "stage1_plan"
    details = result.blocker.details
    assert "N=2" in details
    assert "Which store?" in details
    assert "operator_comment=none" in details
    assert compute_body_sha(_AUTHORED_BODY) in details
    errored = [p for t, p, _ in stage_events() if t == "stage.errored"]
    assert errored[-1]["stage"] == "s1_ambiguity_scan_complete"
    assert errored[-1]["error_kind"] == "ambiguity_scan_unconverged"
    (entry,) = h.entries()
    assert entry.entry.marker.startswith("park-")
    text = "".join(entry.parts)
    assert text.startswith("## Pending Verification Scan")
    assert "### Round blocked" in text
    assert len(h.runner.calls) == 3
    assert _book(h).scan_round == 2


def test_a_fresh_fingerprint_resets_the_round(harness: Callable[..., Harness]) -> None:
    h = harness()
    old = f"operator_comment=c1|body_sha={'b' * 64}"
    _seed(h, LeadingBlock(scan_round=2, last_evaluated=old, applied="none"))
    _snap(h, (("c2", "new comment", "2026-11-09T00:00:00Z"),))
    add_stations(add_turn(h.runner, BODY, tier="large", **_PARK_TURN))
    result = h.run()
    assert result.status == "ambiguities_pending_resolution"
    book = _book(h)
    assert book.scan_round == 1
    assert book.last_evaluated == (
        f"operator_comment=c2|body_sha={compute_body_sha(_AUTHORED_BODY)}"
    )


def test_a_pending_approval_park_never_increments(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _seed(h, LeadingBlock(scan_round=1, last_evaluated=_NONE_FP, applied="none"))
    _snap(h)
    add_stations(add_turn(h.runner, BODY, tier="large"))
    result = h.run()
    assert result.status == "plan_pending_approval"
    assert _book(h).scan_round == 1


def test_a_capped_round_that_completes_is_untouched(
    harness: Callable[..., Harness],
) -> None:
    small = BODY.replace("large", "small")
    h = harness()
    _seed(h, LeadingBlock(scan_round=2, last_evaluated=_NONE_FP, applied="none"), small)
    _snap(h)
    add_stations(add_turn(h.runner, small))
    result = h.run()
    assert result.status == "stage_complete"


def test_a_blocked_draft_write_settles_without_incrementing(
    harness: Callable[..., Harness],
) -> None:
    small = BODY.replace("large", "small")
    h = harness()
    old = f"operator_comment=c1|body_sha={'b' * 64}"
    _seed(h, LeadingBlock(scan_round=2, last_evaluated=old, applied="none"), small)
    _snap(h)
    add_stations(
        add_turn(h.runner, small),
        soundness=station_findings_doc("Plan Soundness Reviewer", severity="MUST_FIX"),
    )
    result = h.run()
    assert result.status == "blocked"
    book = _book(h)
    assert book.scan_round == 0
    assert book.last_evaluated == _NONE_FP


def test_a_pending_stub_blocks_before_the_cap(harness: Callable[..., Harness]) -> None:
    stubbed = BODY + f"\n## Deferred Premises\n\n- X — {DEFERRED_STUB}\n"
    h = harness()
    _seed(h, LeadingBlock(scan_round=2, last_evaluated=_NONE_FP, applied="none"))
    _snap(h)
    add_stations(add_turn(h.runner, stubbed, tier="large", **_PARK_TURN))
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "deferred_stub_unresolved"
    assert result.blocker.retry_eligible is True
    assert DEFERRED_STUB in result.blocker.details
    (entry,) = h.entries()
    assert entry.entry.marker.startswith("park-")


# --- delta ordering ------------------------------------------------------------


@freeze_time(_NOW)
def test_a_different_marker_source_revokes_first(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock(applied="comment:A"))
    _snap(
        h,
        (
            _marker_comment("A", "2026-11-01T00:00:00Z"),
            _marker_comment("B", "2026-11-02T00:00:00Z"),
        ),
    )
    _revision(h)
    add_stations(add_turn(h.runner, REVISED, tier="large"))
    result = h.run()
    (payload,) = _revoked()
    assert payload["ticket_id"] == TICKET
    assert payload["client"] == _row().client
    assert payload["previous_fingerprint"] == compute_plan_draft_fingerprint(BODY)
    assert payload["resolutions_source"] == "codex_plan_resume"
    assert payload["reason"] == "preflight_resolutions_delta"
    assert {"revoked_at", "initiating_service"} <= payload.keys()
    assert _row().plan_approved_at is None
    assert _row().plan_approved_fingerprint is None
    assert len(_revision_calls(h)) == 1
    assert MARKER in str(_revision_calls(h)[0]["stdin"])
    assert result.status == "plan_pending_approval"
    book = _book(h)
    assert book.revoked is not None
    assert book.revoked.fingerprint == compute_plan_draft_fingerprint(BODY)
    assert book.attempted == ResolutionsAttempt("comment:B", "succeeded")
    assert book.applied == "comment:B"
    assert any(f.startswith("multi-marker: 2") for f in result.friction_highlights)


def test_a_row_delta_with_an_equal_fingerprint_keeps_the_approval(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates={**_approve(), **_rows("use redis")})
    _seed(h, LeadingBlock(applied="none"))
    _snap(h)
    _revision(h, BODY)
    add_stations(h.runner)
    result = h.run()
    assert _revoked() == []
    assert h.plan_calls == []
    assert result.status == "stage_complete"
    assert "Round 1: use redis" in str(_revision_calls(h)[0]["stdin"])


def test_a_row_delta_with_a_different_fingerprint_revokes_after_revising(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates={**_approve(), **_rows("use redis")})
    _seed(h, LeadingBlock(scan_round=2, last_evaluated=_NONE_FP, applied="none"))
    _snap(h)
    _revision(h)
    add_stations(add_turn(h.runner, REVISED, tier="large", **_PARK_TURN))
    result = h.run()
    (payload,) = _revoked()
    assert payload["previous_fingerprint"] == compute_plan_draft_fingerprint(BODY)
    assert result.status == "ambiguities_pending_resolution"
    book = _book(h)
    assert book.scan_round == 1
    assert book.applied is not None
    assert book.applied.startswith("row:1:")
    assert book.revoked is not None
    assert "revised widget" in str(h.plan_calls[0]["stdin"])


def test_bootstrap_writes_applied_none(harness: Callable[..., Harness]) -> None:
    h = harness()
    _seed(h, LeadingBlock())
    _snap(h)
    add_stations(add_turn(h.runner, BODY, tier="large", **_PARK_TURN))
    h.run()
    assert _book(h).applied == "none"


def test_a_legacy_row_source_is_a_delta(harness: Callable[..., Harness]) -> None:
    h = harness(task_updates=_rows("use redis"))
    _seed(h, LeadingBlock(applied="row:1"))
    _snap(h)
    _revision(h)
    add_stations(add_turn(h.runner, REVISED, tier="large", **_PARK_TURN))
    h.run()
    assert len(_revision_calls(h)) == 1


@freeze_time(_NOW)
@pytest.mark.parametrize(
    ("attempt", "needle"),
    [
        (ResolutionsAttempt("SRC", "started", _LEASE_FUTURE), "active reservation"),
        (ResolutionsAttempt("SRC", "failed"), "already used"),
        (ResolutionsAttempt("SRC", "succeeded"), "already used"),
    ],
    ids=["active", "failed", "succeeded"],
)
def test_a_live_or_used_attempt_blocks_without_a_turn(
    harness: Callable[..., Harness], attempt: ResolutionsAttempt, needle: str
) -> None:
    h = harness(task_updates=_rows("use redis"))
    source = _row_source()
    _seed(h, LeadingBlock(attempted=ResolutionsAttempt(source, *_tail(attempt))))
    _snap(h)
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "agent_block"
    assert result.blocker.retry_eligible is False
    assert needle in result.blocker.details
    assert h.runner.calls == []


def _row_source() -> str:
    resolution = OperatorResolution(
        text="use redis", recorded_at=_APPROVED_AT, actor="op", round=1
    )
    source = operator_resolutions_source([resolution])
    assert source is not None
    return source


def _tail(attempt: ResolutionsAttempt) -> tuple[str, str | None]:
    return attempt.outcome, attempt.lease_until


@freeze_time(_NOW)
@pytest.mark.parametrize("lease", [_LEASE_PAST, "not-a-time"])
def test_an_expired_lease_is_replaced(
    harness: Callable[..., Harness], lease: str
) -> None:
    h = harness(task_updates=_rows("use redis"))
    _seed(
        h, LeadingBlock(attempted=ResolutionsAttempt(_row_source(), "started", lease))
    )
    _snap(h)
    _revision(h)
    add_stations(add_turn(h.runner, REVISED, tier="large", **_PARK_TURN))
    h.run()
    assert len(_revision_calls(h)) == 1
    assert _book(h).attempted == ResolutionsAttempt(_row_source(), "succeeded")


@pytest.mark.parametrize(
    "reply",
    [
        revise_turn_result(BODY.replace("## Non-Goals", "## Goals")),
        revise_turn_result(BODY.replace("X holds", "X maybe holds")),
        revise_turn_result("<!-- plan-stage-scan-round: 1 -->\n" + BODY),
        revise_turn_result(BODY, returncode=1),
        CodexRunResult(returncode=0, stdout="", stderr="", output_file_content="{}"),
    ],
    ids=["missing-heading", "self-verified-changed", "bookkeeping", "exit", "bad-doc"],
)
def test_a_failed_revision_consumes_the_attempt_and_keeps_the_draft(
    harness: Callable[..., Harness], reply: CodexRunResult
) -> None:
    h = harness(task_updates={**_approve(), **_rows("use redis")})
    _seed(h, LeadingBlock(applied="none"))
    _snap(h)
    _revision(h, reply)
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "agent_block"
    assert "failed" in result.blocker.details
    assert h.plan_calls == []
    book = _book(h)
    assert book.attempted == ResolutionsAttempt(_row_source(), "failed")
    assert book.revoked is not None
    assert h.draft_path().read_text(encoding="utf-8").endswith(BODY)
    assert len(_revoked()) == 1


def test_a_body_section_delta_injects_the_section(
    harness: Callable[..., Harness],
) -> None:
    body = f"# Ticket\n\nText\n\n## Answers\nR1: use redis\n{MARKER}\n"
    h = harness()
    _seed(h, LeadingBlock(applied="none"))
    _snap(h, body=body)
    _revision(h)
    add_stations(add_turn(h.runner, REVISED, tier="large", **_PARK_TURN))
    h.run()
    (call,) = _revision_calls(h)
    assert f"## Answers\nR1: use redis\n{MARKER}" in str(call["stdin"])
    applied = _book(h).applied
    assert applied is not None
    assert applied.startswith("body:")


def test_a_failed_non_row_revision_blocks_after_the_revoke(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock(applied="none"))
    _snap(h, (_marker_comment("B", "2026-11-02T00:00:00Z"),))
    _revision(h, revise_turn_result("", returncode=2))
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "agent_block"
    assert len(_revoked()) == 1
    assert _book(h).attempted == ResolutionsAttempt("comment:B", "failed")


@pytest.mark.parametrize("reply", [REVISED, ""], ids=["after-revise", "after-fail"])
def test_a_failing_row_revoke_fails_closed(
    harness: Callable[..., Harness], reply: str
) -> None:
    h = harness(task_updates={**_approve(), **_rows("use redis")})
    _seed(h, LeadingBlock(applied="none"))
    _snap(h)
    _revision(h, reply)
    with patch("cw.codex_plan._resume.revoke_plan_approval", side_effect=CwError("x")):
        result = h.run()
    assert result.blocker is not None
    assert "could not be revoked" in result.blocker.details
    assert h.plan_calls == []


def test_a_capacity_failure_releases_the_reservation(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_rows("use redis"))
    _seed(h, LeadingBlock(applied="none"))
    _snap(h)
    _revision(
        h, revise_turn_result("", returncode=1, stderr="ERROR: model is at capacity")
    )
    with patch("cw.codex_plan._revise._is_model_capacity_error", return_value=True):
        result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_MODEL_CAPACITY
    assert result.blocker.retry_eligible is True
    assert _book(h).attempted is None


@pytest.mark.parametrize(
    "error",
    [
        CwError("nope"),
        LockTimeoutError(
            "busy", lock_name="dev_queue", lock_path=Path("/x.lock"), waited_s=1.0
        ),
    ],
)
def test_a_failing_revoke_fails_closed(
    harness: Callable[..., Harness], error: Exception
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock(applied="none"))
    _snap(h, (_marker_comment("B", "2026-11-02T00:00:00Z"),))
    with patch("cw.codex_plan._resume.revoke_plan_approval", side_effect=error):
        result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "agent_block"
    assert "could not be revoked" in result.blocker.details
    assert h.runner.calls == []


def test_an_unwritable_lease_fails_closed(harness: Callable[..., Harness]) -> None:
    h = harness(task_updates=_rows("use redis"))
    _seed(h, LeadingBlock(applied="none"))
    _snap(h)
    with patch("cw.codex_plan._resume.write_plan_draft", side_effect=OSError("ro")):
        result = h.run()
    assert result.blocker is not None
    assert "could not be written (OSError)" in result.blocker.details
    assert h.runner.calls == []


# --- revocation requires positive evidence (R8) ----------------------------------


def test_an_absent_applied_source_parks_without_revoking(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock(last_evaluated=_NONE_FP, applied="comment:A"))
    before = h.draft_path().read_text(encoding="utf-8")
    _snap(h, (("x", "plain operator comment", "2026-11-01T00:00:00Z"),))
    _assert_parked_unobserved(h, h.run(), "comment:A", before)


@pytest.mark.parametrize(
    "comments",
    [
        (("x", "plain", "2026-11-01T00:00:00Z"),),
        (("A", f"Answers\n{MARKER}\n{AGENT_COMMENT_MARKER}", "2026-11-01T00:00:00Z"),),
        (_marker_comment("B", "2026-10-01T00:00:00Z"),),
        (_marker_comment("B", "2026-11-05T00:00:00Z"),),
        (),
    ],
    ids=["omitted", "agent-authored", "older-B", "survivor-B", "no-comments"],
)
def test_a_partial_snapshot_parks_without_revoking(
    harness: Callable[..., Harness], comments: tuple[tuple[str, str, str], ...]
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock(applied="comment:A"))
    before = h.draft_path().read_text(encoding="utf-8")
    _snap(h, comments)
    _assert_parked_unobserved(h, h.run(), "comment:A", before)


def test_a_failed_snapshot_read_parks_an_applied_comment(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_approve())
    _seed(h, LeadingBlock(applied="comment:A"))
    before = h.draft_path().read_text(encoding="utf-8")
    _assert_parked_unobserved(h, h.run(), "comment:A", before)


def test_a_stale_stored_snapshot_is_never_read_on_github(
    harness: Callable[..., Harness],
) -> None:
    stored = make_snapshot_from([], ticket_id=TICKET, client="test")
    h = harness(task_updates={**_approve(), "snapshot_digest": write_snapshot(stored)})
    _seed(h, LeadingBlock(applied="none"))
    add_stations(add_turn(h.runner, BODY, tier="large"))
    h.run()
    assert len(h.plan_calls) == 1
    assert not [p for _, p, _ in stage_events() if "skipped" in p.get("stage", "")]


def test_record_ticket_snapshot_returns_the_fresh_snapshot(tmp_path: Path) -> None:
    snapshot = make_snapshot_from([])
    record = type("R", (), {"snapshot": snapshot})()
    with patch("cw.codex_plan._run.record_snapshot_from_github", return_value=record):
        assert _record_ticket_snapshot("42", "c", tmp_path) is snapshot
    for error in (CwError("x"), OSError("y")):
        with patch("cw.codex_plan._run.record_snapshot_from_github", side_effect=error):
            assert _record_ticket_snapshot("42", "c", tmp_path) is None


def test_the_row_route_unblocks_an_unobserved_park(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_rows("use redis"))
    _seed(h, LeadingBlock(applied="comment:A"))
    _snap(h)
    _revision(h)
    add_stations(add_turn(h.runner, REVISED, tier="large", **_PARK_TURN))
    h.run()
    assert _book(h).applied == _row_source()


# --- unit-level resume_gate --------------------------------------------------------


_MCP = SnapshotContext(github=False, operator_ids=("operator",))


def _unit_run(tmp_path: Path, block: LeadingBlock) -> _Run:
    return _Run(
        runner=ScriptedCodexRunner(),
        task=make_task(ticket_id=TICKET).model_copy(update=_approve()),
        client=codex_tracker_client(tmp_path),
        worktree=tmp_path,
        sid="sess-u",
        sess_name="u",
        model=None,
        reasoning_effort=None,
        book=RoundBook(block),
    )


def _resume_draft() -> ResumeDraft:
    return ResumeDraft(
        text=BODY,
        leading="",
        body=BODY,
        fingerprint=compute_plan_draft_fingerprint(BODY),
    )


@pytest.mark.parametrize(
    ("comment", "ctx"),
    [
        (make_comment("A", body=f"x\n{MARKER}", author_id="someone"), _MCP),
        (make_comment("A", body=f"x\n{MARKER}", author_conflict=True), _MCP),
        (make_comment("A", body=f"x\n{MARKER}"), SnapshotContext(github=False)),
    ],
    ids=["foreign-author", "author-conflict", "unreadable"],
)
def test_an_unbound_applied_comment_parks_on_a_non_github_tracker(
    tmp_path: Path, comment: Any, ctx: SnapshotContext
) -> None:
    run = _unit_run(tmp_path, LeadingBlock(applied="comment:A"))
    state = tracker_state(make_snapshot_from([comment]), ctx, run.task)
    out = resume_gate(run, _McpTarget(server="tracker"), _resume_draft(), state)
    assert isinstance(out, _Exit)
    assert out.result.blocker is not None
    assert out.result.blocker.reason == "agent_block"
    assert out.result.blocker.retry_eligible is True


@pytest.mark.parametrize("ctx", [_MCP, SnapshotContext(github=False)])
def test_an_unobserved_applied_body_parks(tmp_path: Path, ctx: SnapshotContext) -> None:
    run = _unit_run(tmp_path, LeadingBlock(applied="body:" + "a" * 64))
    snapshot = make_snapshot_from([make_comment("B", body=f"x\n{MARKER}")])
    state = tracker_state(snapshot, ctx, run.task)
    out = resume_gate(run, _McpTarget(server="tracker"), _resume_draft(), state)
    assert isinstance(out, _Exit)
    assert out.result.blocker is not None
    assert out.result.blocker.retry_eligible is True


def test_an_observed_applied_comment_with_no_change_takes_the_fast_path(
    tmp_path: Path,
) -> None:
    run = _unit_run(tmp_path, LeadingBlock(applied="comment:A"))
    snapshot = make_snapshot_from([make_comment("A", body=f"x\n{MARKER}")])
    state = tracker_state(snapshot, _MCP, run.task)
    out = resume_gate(run, _McpTarget(server="tracker"), _resume_draft(), state)
    assert not isinstance(out, _Exit)
    assert out.fast_path


@pytest.mark.parametrize("applied", [None, "none", "row:1:" + "a" * 64])
def test_a_current_source_of_none_is_no_delta(
    tmp_path: Path, applied: str | None
) -> None:
    run = _unit_run(tmp_path, LeadingBlock(applied=applied))
    state = tracker_state(make_snapshot_from([]), _MCP, run.task)
    out = resume_gate(run, _McpTarget(server="tracker"), _resume_draft(), state)
    assert not isinstance(out, _Exit)
    assert out.fast_path
    assert not out.row_delta


# --- settlement rounds ---------------------------------------------------------------


_PARK_BODY = (
    f"## Pending Verification Scan\n\n1. Which store?\n2. Which cache?\n\n"
    f"{AGENT_COMMENT_MARKER}"
)


def _settling_turn(h: Harness, settlements: list[dict[str, str]]) -> None:
    sentinel = plan_sentinel(tier="large", ticket_id=TICKET, **_PARK_TURN)
    h.runner.add(
        stdin_contains(PLAN_TURN),
        plan_turn_result(BODY, sentinel, settlements=settlements),
    )


def _settle_setup(h: Harness, round_: int = 0) -> None:
    _seed(h, LeadingBlock(scan_round=round_, last_evaluated=None, applied="none"))
    _snap(
        h,
        (
            ("p1", _PARK_BODY, "2026-11-01T00:00:00Z"),
            ("c9", "1: use redis", "2026-11-02T00:00:00Z"),
        ),
    )


def test_an_accepted_settlement_writes_its_line_and_stamps_the_evidence(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _settle_setup(h)
    _settling_turn(h, [{"item_id": "A1", "token": "ADOPTED", "source": "c9"}])
    add_stations(h.runner)
    result = h.run()
    assert result.status == "ambiguities_pending_resolution"
    lines = h.draft_path().read_text(encoding="utf-8").splitlines()
    settled = lines.index("<!-- plan-stage-settled: A1: ADOPTED -->")
    applied = lines.index("<!-- plan-stage-resolutions-applied: source=none -->")
    assert settled < applied
    assert (
        "plan-stage item settled: A1 → ADOPTED — round 1" in result.friction_highlights
    )
    assert result.resolution_consumed is True
    assert result.resolution_evidence == {"comment_id": "c9", "items": ["A1"]}
    assert "park ordinal: 1" in str(h.plan_calls[0]["stdin"])


def test_a_rejected_settlement_writes_nothing(harness: Callable[..., Harness]) -> None:
    h = harness()
    _settle_setup(h)
    _settling_turn(h, [{"item_id": "A9", "token": "ADOPTED", "source": "c9"}])
    add_stations(h.runner)
    result = h.run()
    assert _book(h).settled == ()
    assert result.resolution_consumed is False
    assert any(
        f.startswith("plan-stage settlements unmappable: 1")
        for f in result.friction_highlights
    )


def test_a_settling_round_that_hits_the_cap_is_not_consumed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    _seed(h, LeadingBlock(scan_round=2, last_evaluated=_NONE_FP, applied="none"))
    h.gh.reply(
        *_VIEW,
        stdout=json.dumps(
            json.loads(
                issue_envelope_json((("p1", _PARK_BODY, "2026-11-01T00:00:00Z"),))
            )
        ),
    )
    _settling_turn(h, [{"item_id": "A1", "token": "ADOPTED", "source": "row:1"}])
    add_stations(h.runner)
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "ambiguity_scan_unconverged"
    assert result.resolution_consumed is False


def test_a_row_sourced_settlement_stamps_the_row_round(
    harness: Callable[..., Harness],
) -> None:
    h = harness(task_updates=_rows("use redis"))
    _seed(h, LeadingBlock(applied=_row_source()))
    _snap(h, (("p1", _PARK_BODY, "2026-11-01T00:00:00Z"),))
    _settling_turn(h, [{"item_id": "A2", "token": "ADOPTED", "source": "row:1"}])
    add_stations(h.runner)
    result = h.run()
    assert result.resolution_evidence == {"comment_id": "row:1", "items": ["A2"]}
