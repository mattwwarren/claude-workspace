"""Guard tests: steady-state orchestrator discipline in the cw skills (#2698).

Pure-markdown assertions, following the ``read_text()`` plus
whitespace-normalized literal-substring convention of
``test_park_retry_gating.py`` and ``test_harden_ticket_positioning.py``. The
literals pinned here are written verbatim in the skills, so a rewording that
drops a rule fails here instead of silently.

What is pinned: load ``/cw-orchestration`` first; one Monitor on
``cw orchestrate watch`` and no timer polling; silence when nothing changed;
cw state read through cw views, not subagents; release a parked row with
``resolve``/``requeue``, never a re-add; no hand-edit of the queue store; and
handoffs that carry state, not recipes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import _cmd, _norm
from tests.test_bodyfile_write_tool_conformance import POINTER

ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".claude" / "skills"

SPRINT = "orchestrate-sprint"
ORCHESTRATION = "cw-orchestration"


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _between(content: str, start: str, end: str) -> str:
    """Normalized text of *content* from *start* up to *end*."""
    begin = content.index(start)
    return _norm(content[begin : content.index(end, begin)])


# --- orchestrate-sprint ---


def test_phase0_loads_cw_orchestration_first() -> None:
    content = _skill(SPRINT)
    phase0 = _between(content, "### Phase 0", "### Phase 1")
    assert "Load `/cw-orchestration` first, before any cw command." in phase0
    assert "/cw-orchestration" in _between(content, "---", "# orchestrate-sprint")


def test_phase4_bans_timer_polling() -> None:
    phase4 = _between(_skill(SPRINT), "### Phase 4", "### Phase 5")
    for literal in (
        "Never use ScheduleWakeup, cron or sleep polling; arm one Monitor per sprint.",
        "Nothing changed means no output.",
        "Emit one CKPT line only when something changed.",
        "Leave PR review and CI watching alone unless the operator asks;"
        " the review pipeline owns it.",
    ):
        assert literal in phase4, literal


@pytest.mark.parametrize(
    "literal",
    [
        "timeout_ms: 1800000",
        "re-arm only when it expires",
        "check the first line for `lanes=`",
        "a second watch on the same scope needs `--force`",
        "pass `--all-lanes` unless you are deliberately scoping",
        "`--lane`",
        "`--ticket`",
        "`--all-lanes`",
        "`--since`",
        "`--stuck-after-minutes`",
        "`--force`",
    ],
)
def test_phase4_watch_accuracy(literal: str) -> None:
    phase4 = _between(_skill(SPRINT), "### Phase 4", "### Phase 5")
    assert literal in phase4


def test_rule1_reads_cw_state_through_cw_views() -> None:
    content = _norm(_skill(SPRINT))
    assert "Delegate nearly everything to sonnet agents" not in content
    rule1 = _between(_skill(SPRINT), "1. **", "2. **")
    for view in (
        "cw dev-queue show",
        "cw dev-queue tasks",
        "cw queue peek",
        "cw session result",
    ):
        assert view in rule1, view
    assert (
        "Never use Read Only Helper for `gh` or `git`"
        " (its tools are Read, Grep and Glob)." in rule1
    )


def test_decision_ownership_lists_resolve_and_move() -> None:
    ownership = _between(_skill(SPRINT), "## Decision ownership", "## The loop")
    assert "`cw dev-queue resolve`" in ownership
    assert "`cw dev-queue move`" in ownership
    assert "standing authorization" in ownership


def test_phase5_routes_repeat_parks_to_harden() -> None:
    phase5 = _between(_skill(SPRINT), "### Phase 5", "### Phase 6")
    assert (
        "Route to `/harden-ticket` when the operator flags hardening"
        " or a ticket parks twice on the same disposition." in phase5
    )


def test_phase6_handoff_carries_state_not_recipes() -> None:
    phase6 = _between(_skill(SPRINT), "### Phase 6", "## When to break containment")
    assert "cw dev-queue show" in phase6
    assert "cw orchestrate watch" in phase6


def test_anti_patterns_ask_recurring_question_once() -> None:
    anti = _between(_skill(SPRINT), "## Anti-patterns", "## Related skills")
    assert (
        "Ask a recurring either/or question once per sprint, then apply the answer."
        in anti
    )


# --- cw-orchestration ---


def test_orchestration_description_loads_first() -> None:
    frontmatter = _between(_skill(ORCHESTRATION), "---", "\n# ")
    assert (
        "Load first in any orchestrator session, including /orchestrate-sprint"
        in frontmatter
    )


@pytest.mark.parametrize(
    "literal",
    [
        "`cw dev-queue wait <T>... -c <C>`",
        "`--any`",
        "`--stuck-after-minutes",
        "`exit 4`",
        "`exit 5`",
        "6 hours",
    ],
)
def test_orchestration_wait_accuracy(literal: str) -> None:
    assert literal in _norm(_skill(ORCHESTRATION))


# --- cw-fanout ---


def test_fanout_has_no_queue_store_edit_recipe() -> None:
    content = _norm(_skill("cw-fanout"))
    assert "reset the row's" not in content
    assert "--reset-attempts" in content
    assert "sleep/checkpoint" not in content


# --- cw-followup ---


def test_followup_releases_parks_without_re_add() -> None:
    content = _skill("cw-followup")
    branch = _between(
        content,
        "#### `ambiguities_pending_resolution` / `premises_pending_verification`",
        "#### `blocked`",
    )
    assert "cw dev-queue resolve" in branch
    assert "cw dev-queue add <TICKET>" not in _norm(content)


# --- handoff.md ---


def test_handoff_orchestrator_section() -> None:
    content = _cmd("handoff.md")
    section = _between(content, "## Orchestrator handoffs", "## Related Skills")
    assert "cw dev-queue show" in section
    assert "cw orchestrate watch" in section
    for banned in ("dev_queue.json", "wait 3600", "python3 -c"):
        assert banned not in content, banned
    assert POINTER in content


# --- harden-ticket ---


def test_harden_routes_flagged_and_twice_parked_tickets() -> None:
    content = _norm(_skill("harden-ticket"))
    assert (
        "the operator flags hardening, or a ticket parks twice on the same disposition"
        in content
    )


# --- cw-queue-peek ---


def test_queue_peek_is_a_checkpoint_not_a_timer() -> None:
    content = _norm(_skill("cw-queue-peek"))
    assert "at a checkpoint, not on a timer" in content
