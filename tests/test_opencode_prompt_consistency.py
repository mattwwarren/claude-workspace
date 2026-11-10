"""Cross-prompt consistency pins for the opencode stage prompts (RFC 0018 B1, #3059).

Three properties, each pinned here rather than in the older prompt test file:

* the PLAN audit copy ends with the cw-owned ``cw-opencode-plan`` marker and the
  IMPL prompt reads it alongside the claude-native ``plan-spec-reviewed`` marker;
* the PLAN prompt forges no review sign-off marker (it runs no review stations);
* the FINALIZE prompt emits and frames its result exactly as the doc does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cw.opencode_runner import (
    PLAN_AUDIT_MARKER,
    _sentinel_rules,
    build_stage_prompt,
)
from cw.opencode_tracker_prompt import tracker_ops_fragment
from cw.tracker import TrackerKind
from tests._opencode_helpers import GH_TRACKER, LINEAR_TRACKER, UNSET_TRACKER
from tests.conftest import _cmd

_TRACKERS = [GH_TRACKER, LINEAR_TRACKER, UNSET_TRACKER, None]
_MARKER_LINE = "<!-- cw-opencode-plan: T-1 -->"
_MARKER_RE = re.compile(r"<!-- [a-z-]+")
_SRC = Path(__file__).parent.parent / "src" / "cw"


def _prompt(
    stage: str,
    tracker: TrackerKind | None = None,
    session_id: str | None = None,
) -> str:
    return build_stage_prompt(
        stage,
        "T-1",
        Path("/wt"),
        session_id,
        tracker=tracker,
        tracker_server="linear",
    )


def test_plan_audit_marker_constant() -> None:
    assert PLAN_AUDIT_MARKER == "<!-- cw-opencode-plan"


@pytest.mark.parametrize("tracker", _TRACKERS, ids=["gh", "linear", "unset", "none"])
def test_plan_prompt_tells_the_audit_copy_to_end_with_the_marker_line(
    tracker: TrackerKind | None,
) -> None:
    plan = _prompt("plan", tracker)
    assert (
        "The last line of the audit-copy comment must be exactly "
        f"`{_MARKER_LINE}`" in plan
    )
    assert "Do not put that line in .cw/plan.md" in plan
    assert "Write the full plan verbatim to .cw/plan.md" in plan


@pytest.mark.parametrize("tracker", _TRACKERS, ids=["gh", "linear", "unset", "none"])
def test_impl_prompt_reads_the_marker_the_plan_writes(
    tracker: TrackerKind | None,
) -> None:
    impl = _prompt("impl", tracker)
    assert (
        "ticket comment whose final line is exactly the ticket-specific marker "
        "`<!-- cw-opencode-plan: T-1 -->` (written by an opencode PLAN) or "
        "the claude-native `<!-- plan-spec-reviewed` marker (written by a "
        "claude-native PLAN)" in impl
    )
    assert "ME=$(gh api user --jq .login)" in impl
    assert "comment.author.login == ME" in impl
    assert "MCP viewer/identity operation" in impl
    assert 'fail closed as "plan_missing"' in impl
    assert "never trust marker text alone" in impl
    assert PLAN_AUDIT_MARKER in impl
    assert ".cw/plan.md" in impl
    assert "plan_missing" in impl
    assert tracker_ops_fragment("T-1", tracker, "linear") in impl


@pytest.mark.parametrize("tracker", _TRACKERS, ids=["gh", "linear", "unset", "none"])
def test_plan_prompt_forges_no_review_signoff(tracker: TrackerKind | None) -> None:
    """Kill-check against the rejected option: PLAN must not write a review marker."""
    plan = _prompt("plan", tracker)
    assert "plan-spec-reviewed" not in plan
    assert "plan-soundness-reviewed" not in plan
    assert "write no review sign-off marker of any kind" in plan
    assert set(_MARKER_RE.findall(plan)) == {PLAN_AUDIT_MARKER}


@pytest.mark.parametrize("tracker", _TRACKERS, ids=["gh", "linear", "unset", "none"])
def test_impl_prompt_markers_all_have_a_writer(tracker: TrackerKind | None) -> None:
    """Equality, not subset: also fails if a reader is dropped."""
    impl = _prompt("impl", tracker)
    assert set(_MARKER_RE.findall(impl)) == {
        PLAN_AUDIT_MARKER,
        "<!-- plan-spec-reviewed",
    }


def test_claude_native_writer_of_plan_spec_marker_still_exists() -> None:
    assert "<!-- plan-spec-reviewed" in _cmd("auto-dev-plan.md")


def test_finalize_prompt_uses_the_docs_emit_then_frame() -> None:
    doc = _cmd("auto-dev-finalize.md")
    emit = "printf '%s' \"$SENTINEL_JSON\" | cw result emit -"
    for literal in (emit, "<<<AUTO_DEV_RESULT", "AUTO_DEV_RESULT>>>"):
        assert literal in doc
    prompt = _prompt("finalize", session_id="sid-1")
    for literal in (emit, "<<<AUTO_DEV_RESULT", "AUTO_DEV_RESULT>>>"):
        assert literal in prompt
    assert "cw result emit - --session-id sid-1" in prompt
    assert "<<<AUTO_DEV_RESULT>>>" not in prompt


@pytest.mark.parametrize("session_id", [None, "sid-1"])
def test_finalize_prompt_embeds_the_shared_sentinel_rules(
    session_id: str | None,
) -> None:
    assert _sentinel_rules(session_id) in _prompt("finalize", session_id=session_id)


def test_finalize_prompt_without_session_id_validates_only() -> None:
    prompt = _prompt("finalize")
    assert "cw result validate -" in prompt
    assert "--session-id" not in prompt


def test_no_stale_backend_neutral_claim() -> None:
    """Stale comments that called FINALIZE backend-neutral must be corrected."""
    texts = [
        (_SRC / "opencode_runner.py").read_text(encoding="utf-8"),
        (_SRC / "executor" / "opencode.py").read_text(encoding="utf-8"),
    ]
    normalised = [re.sub(r"\s*\n\s*(?:#\s*)?", " ", t) for t in texts]
    needles = (
        "validated backend-neutral",
        "which never threads one",
        "FINALIZE never receives it",
        "PLAN/IMPL/REVIEW get self-contained prompts carrying *session_id*",
    )
    for text in normalised:
        for needle in needles:
            assert needle not in text
