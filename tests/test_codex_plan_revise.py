"""Tests for cw.codex_plan._revise: the Step 1f.4 revision cycle (#3464)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cw.codex_plan._result import PLAN_MD_RELATIVE_PATH, write_plan_draft
from cw.codex_plan._revise import (
    CONFORMANCE_HEADING,
    REVISION_HEADER,
    check_working_plan,
    render_revision_prompt,
)
from cw.codex_plan._stations import PLAN_REVIEWER, PLAN_SOUNDNESS_REVIEWER
from cw.codex_review import CODEX_MODEL_CAPACITY
from cw.models.operator_resolution import OperatorResolution
from cw.plan_fingerprint import (
    LeadingBlock,
    compute_body_sha,
    compute_plan_draft_fingerprint,
)
from tests._backend_mcp_helpers import codex_tracker_client
from tests._shared.plan_approval_builders import (
    APPROVAL_AT,
    PARK_AT,
    park_body,
    token_body,
    with_operator_ids,
)
from tests._shared.ticket_context import issue_envelope_json, observed_issue_envelope
from tests._tool_fakes import (
    revise_turn_result,
    station_findings_doc,
    stdin_contains,
)
from tests.test_codex_plan_run import (
    PLAN_TURN,
    TICKET,
    Harness,
    add_turn,
    harness,
    stage_events,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.codex_runner import CodexRunResult

pytestmark = pytest.mark.usefixtures("isolated_backend_layers")
__all__ = ["harness"]

PLAN = (
    "## Summary\n\nAdd the widget.\n\n**Scope tier:** small\n\n"
    "## Patterns Found\n\n- none\n\n## Touch-point Contract\n\n- none\n\n"
    "## Files Modified\n\n- src/cw/widget.py (~40 lines)\n\n"
    "## Non-Goals\n\n- none\n\n"
    "## Self-Verified Premises\n\n- X holds — Evidence: y\n\n"
    "## Ambiguities\n\nNO_AMBIGUITIES\n"
)
REVISED = PLAN.replace("Add the widget.", "Add the revised widget.")
LARGE = PLAN.replace("small", "large")
REVISED_LARGE = REVISED.replace("small", "large")
_MUST_FIX = "MUST_FIX"
_VIEW = ("issue", "view", TICKET, "--json", "title,body,url,comments")
_CONSUMED = "plan_comment_approval_consumed: comment=c9"
_MISMATCH = "plan_approval_fingerprint_mismatch"


def _stations(
    h: Harness,
    *,
    reviewer: list[CodexRunResult],
    soundness: CodexRunResult | None = None,
) -> None:
    h.runner.add(
        stdin_contains(f"You are the {PLAN_SOUNDNESS_REVIEWER}"),
        soundness or station_findings_doc(PLAN_SOUNDNESS_REVIEWER),
    )
    for reply in reviewer:
        h.runner.add(stdin_contains(f"You are the {PLAN_REVIEWER}"), reply, times=1)


def _must_fix(role: str = PLAN_REVIEWER) -> CodexRunResult:
    return station_findings_doc(role, severity=_MUST_FIX)


def _clean(role: str = PLAN_REVIEWER) -> CodexRunResult:
    return station_findings_doc(role)


def _revision(h: Harness, reply: CodexRunResult) -> None:
    h.runner.add(stdin_contains(REVISION_HEADER), reply)


def _revision_calls(h: Harness) -> list[dict[str, object]]:
    return [c for c in h.runner.calls if REVISION_HEADER in str(c["stdin"])]


def _errored() -> list[dict[str, object]]:
    return [p for t, p, _ in stage_events() if t == "stage.errored"]


def test_a_revision_that_clears_the_finding_completes(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_must_fix(), _clean()])
    _revision(h, revise_turn_result(REVISED))
    result = h.run()
    assert result.status == "stage_complete"
    plan = (h.worktree / PLAN_MD_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "Add the revised widget." in plan
    (call,) = _revision_calls(h)
    stdin = str(call["stdin"])
    assert "[MUST_FIX]" in stdin
    assert "## Current draft" in stdin
    assert "byte-for-byte" in stdin
    assert PLAN_TURN not in stdin
    assert len(h.plan_calls) == 1


def test_a_persisting_must_fix_posts_the_blocking_findings(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_must_fix(), _must_fix()])
    _revision(h, revise_turn_result(REVISED))
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_unreviewable"
    assert result.plan_draft_fingerprint == compute_plan_draft_fingerprint(
        h.draft_path().read_text(encoding="utf-8")
    )
    assert "Add the revised widget." in h.draft_path().read_text(encoding="utf-8")
    assert f"blocking findings posted: {PLAN_REVIEWER}" in result.friction_highlights
    (entry,) = h.entries()
    assert entry.entry.marker.startswith("blocking-")
    text = "".join(entry.parts)
    assert text.startswith("## Blocking Review Findings\n\n")
    assert f"### {PLAN_REVIEWER} — MUST_FIX" in text
    assert _errored()[-1]["error_kind"] == "plan_revision_failed"
    assert _errored()[-1]["stage"] == "s1_plan_reviewed"


def test_a_soundness_must_fix_blocks_at_once_with_no_revision(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_clean()], soundness=_must_fix(PLAN_SOUNDNESS_REVIEWER))
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_unsound"
    assert _revision_calls(h) == []
    (entry,) = h.entries()
    assert f"### {PLAN_SOUNDNESS_REVIEWER} — MUST_FIX" in "".join(entry.parts)
    assert _errored()[-1]["error_kind"] == "plan_unsound"


@pytest.mark.parametrize("status", ["failed", "degraded"])
def test_a_non_must_fix_block_takes_no_revision_and_posts_nothing(
    harness: Callable[..., Harness], status: str
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[station_findings_doc(PLAN_REVIEWER, status=status)])
    result = h.run()
    assert result.blocker is not None
    assert _revision_calls(h) == []
    assert h.entries() == []


@pytest.mark.parametrize(
    "reply",
    [
        revise_turn_result(PLAN.replace("## Non-Goals", "## Goals")),
        revise_turn_result(""),
        revise_turn_result(PLAN, returncode=3),
    ],
    ids=["missing-heading", "empty", "exit"],
)
def test_an_invalid_revision_keeps_the_draft(
    harness: Callable[..., Harness], reply: CodexRunResult
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_must_fix()])
    _revision(h, reply)
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_unreviewable"
    assert h.draft_path().read_text(encoding="utf-8").endswith(PLAN)
    assert _errored()[-1]["error_kind"] == "plan_revision_failed"


def test_a_revised_tier_mismatch_fails_gate_set_b(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_must_fix()])
    _revision(h, revise_turn_result(PLAN.replace("small", "large")))
    result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "codex_plan_gate_failed"


def test_a_revised_large_plan_parks_for_fresh_approval(
    harness: Callable[..., Harness],
) -> None:
    large = PLAN.replace("small", "large")
    approval = {
        "plan_approved_at": datetime(2026, 11, 10, tzinfo=UTC),
        "plan_approved_fingerprint": compute_plan_draft_fingerprint(large),
    }
    h = harness(task_updates=approval)
    add_turn(h.runner, large, tier="large")
    _stations(h, reviewer=[_must_fix(), _clean(), _clean()])
    _revision(h, revise_turn_result(REVISED.replace("small", "large")))
    result = h.run()
    assert result.status == "plan_pending_approval"
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert [f for f in result.friction_highlights if f.startswith(_MISMATCH)]
    assert not [f for f in result.friction_highlights if f.startswith(_CONSUMED)]


def _comment_approved(h: Harness) -> Harness:
    """*h* resumed from a parked large draft that an operator token approved."""
    write_plan_draft(h.worktree, LARGE, book=LeadingBlock(applied="none"))
    comments = [("p1", park_body(), PARK_AT), ("c9", token_body(), APPROVAL_AT)]
    h.gh.reply(*_VIEW, stdout=issue_envelope_json(comments))
    h.client = with_operator_ids(codex_tracker_client(h.worktree))
    add_turn(h.runner, LARGE, tier="large")
    return h


def test_a_revision_that_rewrites_a_comment_approved_plan_parks_again(
    harness: Callable[..., Harness],
) -> None:
    """The approval was for the parked plan; the reviewer's rewrite is a new one."""
    h = _comment_approved(harness())
    _stations(h, reviewer=[_must_fix(), _clean()])
    _revision(h, revise_turn_result(REVISED_LARGE))
    result = h.run()
    assert result.status == "plan_pending_approval"
    assert not (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert [f for f in result.friction_highlights if f.startswith(_CONSUMED)]
    assert len(_revision_calls(h)) == 1


def test_a_revision_that_leaves_the_draft_fingerprint_keeps_the_comment_approval(
    harness: Callable[..., Harness],
) -> None:
    h = _comment_approved(harness())
    _stations(h, reviewer=[_must_fix(), _clean()])
    _revision(h, revise_turn_result(LARGE))
    result = h.run()
    assert result.status == "stage_complete"
    assert (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert not [f for f in result.friction_highlights if f.startswith(_CONSUMED)]
    assert len(_revision_calls(h)) == 1


def test_a_revised_plan_stays_approved_while_the_alternate_holds(
    harness: Callable[..., Harness],
) -> None:
    """A row approval the alternate still honours covers a reviewer's rewrite."""
    approved = {
        "plan_approved_at": datetime(2026, 11, 10, tzinfo=UTC),
        "plan_approved_fingerprint": compute_plan_draft_fingerprint(LARGE),
    }
    h = harness(task_updates=approved)
    drifted = LARGE.replace("Add the widget.", "Add the drifted widget.")
    body_sha = compute_body_sha(observed_issue_envelope()["body"])
    book = LeadingBlock(last_evaluated=f"operator_comment=none|body_sha={body_sha}")
    write_plan_draft(h.worktree, drifted, book=book)
    h.gh.reply(*_VIEW, stdout=issue_envelope_json())
    h.client = codex_tracker_client(h.worktree)
    _stations(h, reviewer=[_must_fix(), _clean()])
    _revision(h, revise_turn_result(REVISED_LARGE))
    result = h.run()
    assert h.plan_calls == []
    assert result.status == "stage_complete"
    assert (h.worktree / PLAN_MD_RELATIVE_PATH).exists()
    assert not [f for f in result.friction_highlights if f.startswith(_MISMATCH)]


def test_a_capacity_failure_in_the_revision_is_retryable(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_must_fix()])
    _revision(h, revise_turn_result("", returncode=1))
    with patch("cw.codex_plan._revise._is_model_capacity_error", return_value=True):
        result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == CODEX_MODEL_CAPACITY
    assert result.blocker.retry_eligible is True


def test_the_comment_enqueue_fault_is_swallowed(
    harness: Callable[..., Harness],
) -> None:
    h = harness()
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_must_fix(), _must_fix()])
    _revision(h, revise_turn_result(REVISED))
    with patch("cw.outbox_producer.enqueue", side_effect=OSError("disk")):
        result = h.run()
    assert result.blocker is not None
    assert result.blocker.reason == "plan_unreviewable"


def test_row_resolutions_are_injected_into_the_revision(
    harness: Callable[..., Harness],
) -> None:
    row = OperatorResolution(
        text="use redis",
        recorded_at=datetime(2026, 11, 9, tzinfo=UTC),
        actor="o",
        round=1,
    )
    h = harness(task_updates={"operator_resolutions": [row]})
    add_turn(h.runner, PLAN)
    _stations(h, reviewer=[_must_fix(), _clean()])
    _revision(h, revise_turn_result(REVISED))
    h.run()
    (call,) = _revision_calls(h)
    assert "- Round 1: use redis" in str(call["stdin"])


class TestWorkingPlan:
    def test_accepts_a_valid_revision(self) -> None:
        assert check_working_plan(PLAN, REVISED, resolutions=False) is None

    @pytest.mark.parametrize(
        ("revised", "why"),
        [
            ("   ", "the revision is empty"),
            ("<!-- plan-stage-scan-round: 0 -->\n" + PLAN, "plan-stage- line"),
            (PLAN.replace("## Patterns Found", "## Patterns"), "missing ## Patterns"),
            (PLAN.replace("X holds", "Y holds"), "Self-Verified Premises entries"),
        ],
    )
    def test_rejects(self, revised: str, why: str) -> None:
        rejection = check_working_plan(PLAN, revised, resolutions=False)
        assert rejection is not None
        assert why in rejection

    def test_an_accumulator_section_the_prior_carried_must_stay(self) -> None:
        prior = PLAN + "\n## Settled Plan Items\n\n- A1 — Which?\n"
        assert check_working_plan(prior, PLAN, resolutions=False) == (
            "missing ## Settled Plan Items"
        )

    def test_a_resolutions_revision_needs_the_conformance_section(self) -> None:
        assert check_working_plan(PLAN, PLAN, resolutions=True) == (
            f"missing {CONFORMANCE_HEADING}"
        )
        misplaced = PLAN.replace(
            "## Patterns Found", f"{CONFORMANCE_HEADING}\n\n- R1\n\n## Patterns Found"
        )
        rejection = check_working_plan(PLAN, misplaced, resolutions=True)
        assert rejection is not None
        assert "is not immediately before ## Ambiguities" in rejection
        placed = PLAN.replace(
            "## Ambiguities", f"{CONFORMANCE_HEADING}\n\n- R1\n\n## Ambiguities"
        )
        assert check_working_plan(PLAN, placed, resolutions=True) is None

    def test_the_prompt_carries_the_resolutions_and_findings(self) -> None:
        prompt = render_revision_prompt(
            body=PLAN, findings=["- finding"], resolutions="- Round 1: X"
        )
        assert prompt.startswith(REVISION_HEADER)
        assert "## Binding Pre-flight Resolutions\n\n- Round 1: X" in prompt
        assert "## Review findings to address (MUST_FIX)\n\n- finding" in prompt
        assert PLAN_TURN not in prompt
