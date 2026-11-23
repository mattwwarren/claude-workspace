"""Docs pins for RFC 0020 B3 (#3785): open an epic, land an epic, vocabulary.

An epic is a set of rows sharing a base branch. cw creates and deletes no
epic branch and has no epic command group; the orchestration skills and the
dispatch runbook carry the procedure. These pins hold the literals and the
ordering the procedure depends on, and the D-12 / D-6 negatives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._shared.workflow_text import _norm, _section_between, _skill
from tests.test_bodyfile_write_tool_conformance import POINTER
from tests.test_orchestration_state_map import (
    _click_paths,
    _resolve,
)

ROOT = Path(__file__).resolve().parents[1]
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
SPRINT_MIN_CW_SPANS = 5

CW_ORCH = "cw-orchestration/SKILL.md"
SPRINT = "orchestrate-sprint/SKILL.md"
FOLLOWUP = "cw-followup/SKILL.md"
QUEUE_ISSUES = ROOT / ".claude" / "commands" / "queue-issues.md"
RUNBOOK = ROOT / "docs" / "dispatch-runbook.md"

EPIC_HEADING = "## Epics: open one, land one"
OPEN_HEADING = "### Open an epic"
LAND_HEADING = "### Land an epic"
ANTI_HEADING = "## Anti-patterns"
SEMANTICS_HEADING = "## Command semantics"
DO_NOT_HEADING = "## Do NOT"
NO_EPIC_GROUP = "there is no epic command group"
BASE_ROW_HEADING = "#### A base row: merged is not shipped"
SHIPPED_HEADING = "#### `shipped`"
STEP_5C = "## Step 5c"
STEP_6 = "## Step 6"
RUNBOOK_HEADING = "### Open and land an epic (RFC 0020 B3, #3785)"
RUNBOOK_NEXT = "### Review-recipe dispatch and the one-shot latch"
B1_ANCHOR = "#enqueue-on-an-epic-base---base-rfc-0020-b1-3783"
B2_ANCHOR = "#adopt-an-open-human-pr---adopt-pr-rfc-0016-2943"


def _epic_section() -> str:
    return _norm(_section_between(_skill(SPRINT), EPIC_HEADING, ANTI_HEADING))


def _open_section() -> str:
    return _norm(_section_between(_skill(SPRINT), OPEN_HEADING, LAND_HEADING))


def _land_section() -> str:
    return _norm(_section_between(_skill(SPRINT), LAND_HEADING, ANTI_HEADING))


def _semantics() -> str:
    return _norm(_section_between(_skill(CW_ORCH), SEMANTICS_HEADING))


def _runbook_section() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index(RUNBOOK_HEADING)
    return _norm(text[start : text.index(RUNBOOK_NEXT, start)])


def _followup_base_row() -> str:
    return _norm(_section_between(_skill(FOLLOWUP), BASE_ROW_HEADING, SHIPPED_HEADING))


def _queue_step_5c() -> str:
    text = QUEUE_ISSUES.read_text(encoding="utf-8")
    return _norm(_section_between(text, STEP_5C, STEP_6))


def _new_prose_slices() -> dict[str, str]:
    return {
        "sprint-epics": _epic_section(),
        "followup-base-row": _followup_base_row(),
        "queue-step-5c": _queue_step_5c(),
        "runbook-epic": _runbook_section(),
    }


# 1. Vocabulary -------------------------------------------------------------


def test_vocabulary_defines_epic_next_to_fanout_and_swing() -> None:
    semantics = _semantics()
    assert "**Epic (RFC 0020).**" in semantics
    assert "reach the default branch once" in semantics
    assert "stand as a ticket" in semantics
    assert "A swing may be a row inside an epic" in semantics
    assert "--base" in semantics
    assert "--adopt-pr" in semantics
    assert NO_EPIC_GROUP in semantics
    assert "`cw epic" not in semantics


def test_do_not_carries_landing_pr_exception() -> None:
    do_not = _norm(_section_between(_skill(CW_ORCH), DO_NOT_HEADING))
    assert "epic's landing PR" in do_not


# 2. orchestrate-sprint structure ------------------------------------------


def test_sprint_has_epic_section_with_open_and_land_subsections() -> None:
    text = _skill(SPRINT)
    assert EPIC_HEADING in text
    assert OPEN_HEADING in text
    assert LAND_HEADING in text
    assert text.index(EPIC_HEADING) < text.index(ANTI_HEADING)


def test_phase_one_reads_swing_epic_or_fanout() -> None:
    assert "Swing, epic or fan-out?" in _skill(SPRINT)


# 3. Open literals ----------------------------------------------------------


@pytest.mark.parametrize(
    "literal",
    [
        "epic/<parent-id>",
        "git push origin origin/<default-branch>:refs/heads/epic/<parent-id>",
        "git ls-remote --exit-code --heads origin epic/<parent-id>",
        "gh api repos/{owner}/{repo}/branches/epic/<parent-id>/protection",
        "gh api repos/{owner}/{repo}/rules/branches/epic/<parent-id>",
        "--base epic/<parent-id>",
    ],
)
def test_open_section_carries_literal(literal: str) -> None:
    assert literal in _open_section()


# 4. Land literals ----------------------------------------------------------


@pytest.mark.parametrize(
    "literal",
    [
        "cw dev-queue tasks -c <C> --base epic/<parent-id>",
        "--status completed",
        "completed, failed and cancelled",
        "`blocked_on_user` is not terminal",
        "gh pr list --base epic/<parent-id> --state open",
        "landing_citations.py",
        "scripts/install-skills.sh",
        ".claude/scripts/landing_citations.py",
        "cw dev-queue add <parent-id> -c <C> --lane <lane> --adopt-pr <N>",
        "no `--base`",
        "/review-monitor",
        POINTER,
    ],
)
def test_land_section_carries_literal(literal: str) -> None:
    assert literal in _land_section()


# 5. Ordering ----------------------------------------------------------------


def test_open_protection_check_precedes_enqueue() -> None:
    section = _open_section()
    assert section.index("/protection") < section.index("cw dev-queue add <child>")


def test_land_ordering_terminal_then_pr_then_adopt_then_monitor() -> None:
    section = _land_section()
    terminal = section.index("completed, failed and cancelled")
    pr_create = section.index("gh pr create")
    adopt = section.index("--adopt-pr <N>")
    monitor = section.index("/review-monitor")
    assert terminal < pr_create < adopt < monitor


# 6. D-12 and D-6 negatives -------------------------------------------------


def test_new_sections_have_no_approval_or_mutating_api() -> None:
    for name, text in _new_prose_slices().items():
        assert "--approve" not in text, name
        for verb in ("--method", "-X PUT", "-X PATCH", "-X DELETE"):
            assert verb not in text, (name, verb)


def test_new_sections_say_settings_untouched_and_never_approves() -> None:
    combined = _epic_section()
    assert "never changes repository settings" in combined
    assert "never approves" in combined


def test_skill_never_says_cw_creates_or_deletes_the_branch() -> None:
    combined = _epic_section()
    assert "cw creates and deletes no epic branch" in combined
    for phrase in ("cw creates the epic branch", "cw deletes the epic branch"):
        assert phrase not in combined


# 7. cw-followup base row ---------------------------------------------------


@pytest.mark.parametrize(
    "literal",
    [
        "base_branch:",
        "cw dev-queue show <TICKET> -c <CLIENT>",
        "built, not shipped",
        "do not close the ticket",
        "rebase onto `origin/<base>`",
        "gh pr create --base <base>",
        "Never open a landing PR here",
    ],
)
def test_followup_base_row_carries_literal(literal: str) -> None:
    assert literal.lower() in _followup_base_row().lower()


# 8. queue-issues -----------------------------------------------------------


def test_queue_issues_step_5c_base_check() -> None:
    step = _queue_step_5c()
    assert "cw dev-queue tasks <ID> --json -c" in step
    assert "built on `<base>`; closes at the landing" in step
    assert "COMPLETED" in step


def test_queue_issues_step_5b_names_epic_option() -> None:
    text = _norm(QUEUE_ISSUES.read_text(encoding="utf-8"))
    assert "Open an epic" in text
    assert "`queue-issues` does not create the branch" in text


def test_queue_issues_base_fence_present() -> None:
    text = _norm(QUEUE_ISSUES.read_text(encoding="utf-8"))
    assert 'cw dev-queue add --client "$CLIENT" --base <epic-branch>' in text


# 9. Runbook ----------------------------------------------------------------


@pytest.mark.parametrize(
    "literal",
    [
        "git push origin --delete epic/<parent-id>",
        "operator action",
        "only after the landing PR is MERGED",
        NO_EPIC_GROUP,
        B1_ANCHOR,
        B2_ANCHOR,
    ],
)
def test_runbook_epic_section_carries_literal(literal: str) -> None:
    assert literal in _runbook_section()


def test_runbook_epic_section_does_not_restate_b2_refusal() -> None:
    assert "refuse_if_adoption_conflicts" not in _runbook_section()


# 10. Command resolution ----------------------------------------------------


def _spans(text: str) -> list[str]:
    """Single-backtick spans, with fenced blocks removed first.

    The slices are whitespace-collapsed, so a triple-backtick fence would
    otherwise flip the single-backtick pairing and hide every later span.
    """
    return _SPAN_RE.findall(_FENCE_RE.sub(" ", text))


def _cw_spans(text: str) -> list[str]:
    return [span for span in _spans(text) if span.startswith("cw ")]


@pytest.mark.parametrize(
    "name", ["sprint-epics", "followup-base-row", "queue-step-5c", "runbook-epic"]
)
def test_new_prose_cw_spans_resolve(name: str) -> None:
    paths = _click_paths()
    cw_spans = _cw_spans(_new_prose_slices()[name])
    if name == "sprint-epics":
        assert len(cw_spans) >= SPRINT_MIN_CW_SPANS
        assert any("--adopt-pr" in span for span in cw_spans)
        assert any("--base" in span for span in cw_spans)
    problems: list[str] = []
    for span in cw_spans:
        problems.extend(_resolve(span, paths))
    assert problems == []


def test_no_cw_epic_span_in_new_prose_or_semantics() -> None:
    texts = [*_new_prose_slices().values(), _semantics()]
    for text in texts:
        for span in _spans(text):
            assert span != "cw epic"
            assert not span.startswith("cw epic ")
