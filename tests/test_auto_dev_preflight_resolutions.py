"""Guard tests: pre-flight resolutions as a binding, checked constraint (#828).

Pure-markdown assertions over the auto-dev pipeline instruction files. Mirrors the
``read_text()`` + literal-substring convention of ``test_auto_dev_model_pins.py``.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
AGENTS = ROOT / ".claude" / "agents"
SKILLS = ROOT / ".claude" / "skills"

REFUSE = "multiple resolution comments detected — re-run /harden-ticket to consolidate"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _agent(name: str) -> str:
    return (AGENTS / name).read_text()


def _skill(name: str) -> str:
    return (SKILLS / name).read_text()


def test_plan_refuses_multiple_marker_comments() -> None:
    """>1 marker comment refuses with the exact operator message."""
    assert REFUSE in _cmd("auto-dev-plan.md")


def test_plan_refuse_uses_ambiguities_status() -> None:
    """Multi-marker refuse reuses the ambiguities_pending_resolution status."""
    content = _cmd("auto-dev-plan.md")
    idx = content.index(REFUSE)
    window = content[max(0, idx - 400) : idx + len(REFUSE)]
    assert "ambiguities_pending_resolution" in window


def test_plan_setup_greps_preflight_marker() -> None:
    """Step 1b setup greps the pre-flight resolutions marker."""
    assert "<!-- auto-dev-preflight-resolutions -->" in _cmd("auto-dev-plan.md")


def test_harden_directs_superseding_comment() -> None:
    """Re-harden posts a fresh superseding comment, not an append."""
    assert "## Pre-flight Resolutions (operator) — supersedes all prior" in _skill(
        "harden-ticket/SKILL.md"
    )


def test_harden_drops_accretion_guidance() -> None:
    """The 'append to the resolution comment' guidance is gone (newline-normalized)."""
    normalized = " ".join(_skill("harden-ticket/SKILL.md").split())
    assert "append to the resolution comment" not in normalized


def test_step1b_receives_all_comments() -> None:
    """Step 1b's context bullet passes ALL ticket comments, mirroring Step 1c."""
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1b: Generate Plan")
    end = content.index("### Step 1c:")
    section = content[start:end]
    assert "ALL ticket comments in chronological order" in section


def test_plan_injects_binding_resolutions_section() -> None:
    """Step 1b injects a `## Binding Pre-flight Resolutions` section."""
    assert "## Binding Pre-flight Resolutions" in _cmd("auto-dev-plan.md")


def test_plan_emits_conformance_section() -> None:
    """The producer contract requires a Pre-flight Resolution Conformance section."""
    assert "## Pre-flight Resolution Conformance" in _cmd("auto-dev-plan.md")


def test_conformance_line_format() -> None:
    """The conformance line template is specified verbatim."""
    template = (
        "- R<n>: <short restatement> — "
        "<how the plan honors it> [SATISFIED | NOT APPLICABLE]"
    )
    assert template in _cmd("auto-dev-plan.md")


def test_conformance_placed_before_ambiguities() -> None:
    """Within Step 1b, the conformance bullet precedes the Ambiguities bullet."""
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1b: Generate Plan")
    end = content.index("### Step 1c:")
    section = content[start:end]
    conf = section.index("## Pre-flight Resolution Conformance")
    amb = section.index("## Ambiguities")
    assert conf < amb


def test_reviewer_check1_weaves_conformance() -> None:
    """Plan Reviewer Check 1 (full section) gates conformance as MUST_FIX / MISSING."""
    content = _agent("plan-reviewer.md")
    start = content.index("### Check 1 — Contract Specificity")
    end = content.index("### Check 2 — File Enumeration")
    section = content[start:end]
    assert "## Binding Pre-flight Resolutions" in section
    assert "## Pre-flight Resolution Conformance" in section
    assert "MUST_FIX" in section
    assert "MISSING" in section


def test_plan_spec_stays_v2_no_bump() -> None:
    """The plan-spec marker stays v2 — no version bump for this ticket (R4)."""
    content = _cmd("auto-dev-plan.md")
    assert "plan-spec" in content
    assert "v2" in content
    assert "v3" not in content


def test_conformance_omitted_when_no_binding_resolutions() -> None:
    """The producer bullet states the no-marker omit fallback near the Binding name."""
    content = _cmd("auto-dev-plan.md")
    omit = content.index("omit `## Pre-flight Resolution Conformance` entirely")
    preceding = content[max(0, omit - 400) : omit]
    assert "## Binding Pre-flight Resolutions" in preceding
