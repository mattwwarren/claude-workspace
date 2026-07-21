"""Guard tests: Plan Reviewer format-only severity floor + defense-in-depth (#1393).

Pure-markdown assertions over the auto-dev pipeline instruction files. Mirrors the
``read_text()`` + literal-substring/window convention of
``test_auto_dev_preflight_resolutions.py`` / ``test_auto_dev_model_pins.py`` — no
shared import module exists in this repo, so the helpers are duplicated locally
per the established convention.

Background: a Plan Reviewer finding that is purely a format/shape regression of an
already-content-verified section was unconditionally MUST_FIX, even when the
underlying facts were still accurate. This could burn the plan's single
revision-cycle budget on a cosmetic issue, leaving zero budget for a genuinely
substantive MUST_FIX that surfaces later. The fix adds (1) a format-only severity
floor to Plan Reviewer's Check 1 Reject/MUST_FIX criteria, and (2) an independent,
one-shot revision-cycle axis in auto-dev-plan.md's Step 1f.3/1f.4 for the case
where every persisting MUST_FIX finding is category ``Format-Only``.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
AGENTS = ROOT / ".claude" / "agents"

CARVEOUT_ANCHOR = "**Format-only carve-out (severity floor).**"
FORMAT_ONLY_REVISION_ANCHOR = "**Format-only revision (defense-in-depth).**"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _agent(name: str) -> str:
    return (AGENTS / name).read_text()


def _nearby(content: str, anchor: str, span: int = 400) -> str:
    idx = content.index(anchor)
    return content[max(0, idx - span) : idx + len(anchor)]


def _after(content: str, anchor: str, span: int = 400) -> str:
    idx = content.index(anchor)
    return content[idx : idx + span]


def test_reviewer_documents_severity_floor() -> None:
    """A pure format/shape regression, independently re-verified accurate, is SHOULD_FIX."""
    content = _agent("plan-reviewer.md")
    window = _after(content, CARVEOUT_ANCHOR, span=1200)
    assert "not automatically MUST_FIX" in window
    assert "Downgrade to SHOULD_FIX" in window


def test_reviewer_severity_floor_requires_independent_reverification() -> None:
    """The carve-out requires re-reading/confirming claims in *this* pass, not rubber-stamping."""
    content = _agent("plan-reviewer.md")
    window = _after(content, CARVEOUT_ANCHOR, span=1200)
    assert (
        "the reviewer independently re-reads the cited touch-points in "
        "*this* review pass and confirms every claim is still accurate" in window
    )
    assert (
        "reusing a prior pass's verdict without re-reading does not qualify"
        in window
    )


def test_reviewer_severity_floor_excludes_unverifiable_regressions() -> None:
    """The carve-out does not apply when the regression destroys verifiability."""
    content = _agent("plan-reviewer.md")
    window = _after(content, CARVEOUT_ANCHOR, span=1200)
    assert (
        "does **not** apply when the regression also destroys verifiability"
        in window
    )
    assert "those cases remain MUST_FIX per the Reject bullets above" in window


def test_reviewer_reserves_format_only_category() -> None:
    """The `<category>` finding-line slot reserves the literal value `Format-Only`."""
    content = _agent("plan-reviewer.md")
    assert "**Reserved category:** `Format-Only`" in content


def test_reviewer_carveout_scoped_to_required_sections_only() -> None:
    """The carve-out is scoped to Check 1's 3 required sections, not Checks 2-4."""
    content = _agent("plan-reviewer.md")
    window = _after(content, CARVEOUT_ANCHOR, span=1200)
    assert "## Patterns Found" in window
    assert "## Touch-point Contract" in window
    assert "## Pre-flight Resolution Conformance" in window
    assert "does not extend to Checks 2-4" in window


def test_plan_step1f3_grants_independent_format_only_cycle() -> None:
    """Step 1f.3 gates all-Format-Only MUST_FIX to Step 1f.4 without standard budget."""
    content = _cmd("auto-dev-plan.md")
    anchor = (
        "**MUST_FIX where every persisting finding's category is exactly "
        "`Format-Only`, format-only cycle not yet used (any scope, any mode)** "
        "→ spawn plan-revision agent (Step 1f.4)"
    )
    assert anchor in content
    window = _after(content, anchor, span=300)
    assert (
        "does not require or consume the standard revision-cycle budget"
        in window
    )


def test_plan_step1f4_format_only_cycle_does_not_decrement_standard_budget() -> None:
    """Step 1f.4's format-only cycle is tracked on an independent budget axis."""
    content = _cmd("auto-dev-plan.md")
    window = _after(content, FORMAT_ONLY_REVISION_ANCHOR, span=1000)
    assert "independent axis" in window
    assert "does **not** decrement, the standard 1-cycle budget" in window


def test_plan_step1f4_format_only_cycle_capped_at_one() -> None:
    """The format-only cycle is itself capped at 1 attempt, falling through if exhausted."""
    content = _cmd("auto-dev-plan.md")
    window = _after(content, FORMAT_ONLY_REVISION_ANCHOR, span=1000)
    assert "capped at **1 attempt**" in window
    assert "fall through to the standard" in window


def test_plan_step1f4_sonnet_pin_preserved() -> None:
    """Regression guard: the sonnet pin substring pinned by test_auto_dev_model_pins.py."""
    content = _cmd("auto-dev-plan.md")
    assert 'Re-spawn the **Plan** agent (`model: "sonnet"`)' in content


def test_plan_substantive_must_fix_branches_unchanged() -> None:
    """Regression guard: existing substantive MUST_FIX gating bullets are untouched."""
    content = _cmd("auto-dev-plan.md")
    assert (
        "**MUST_FIX, 1st cycle, Small or interactive** → spawn plan-revision "
        "agent (Step 1f.4), re-review once."
    ) in content
    assert (
        '**MUST_FIX persists after 1 revision cycle, headless** → EXIT '
        '`blocked` with `blocker.reason: "plan_unreviewable"`. Do NOT post '
        "the stale plan to Linear."
    ) in content


def test_plan_spec_marker_stays_v2() -> None:
    """The plan-spec marker stays v2 — no version bump for this ticket."""
    content = _cmd("auto-dev-plan.md")
    assert "plan-spec" in content
    assert "v2" in content
    assert "v3" not in content
