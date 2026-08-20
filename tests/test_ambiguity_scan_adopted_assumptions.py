"""Guard tests: ambiguity scan does not re-raise plan-closed questions (#1593).

Pure-markdown assertions over the auto-dev pipeline instruction files. Mirrors
the ``read_text()`` + literal-substring convention of
``test_auto_dev_preflight_resolutions.py``. Imports ``_after``/``_nearby``
from that module rather than duplicating them, per its existing export and
the convention already followed by ``test_consolidated_park.py`` /
``test_plan_persistence.py``.
"""

from pathlib import Path

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).parent.parent
AGENTS = ROOT / ".claude" / "agents"


def _agent(name: str) -> str:
    return (AGENTS / name).read_text()


def _step1c_prompt_must_include_window() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1c: Ambiguity Verification")
    end = content.index("### Step 1d:")
    section = content[start:end]
    prompt_start = section.index("**Prompt must include:**")
    next_heading = section.index("2. Parse the agent's output.", prompt_start)
    return section[prompt_start:next_heading]


def test_pm_reviewer_mode1_has_resolution_record_subsection() -> None:
    """Mode 1 gains a subsection cross-checking the plan's own resolution record."""
    content = _agent("product-manager-reviewer.md")
    assert "### Before surfacing: check the plan's own resolution record" in content


def test_pm_reviewer_cross_checks_all_three_sections() -> None:
    """The new subsection names all three resolution-record sections, not just one."""
    content = _agent("product-manager-reviewer.md")
    window = _after(
        content,
        "### Before surfacing: check the plan's own resolution record",
        span=1500,
    )
    assert "`## Adopted Assumptions`" in window
    assert "`## Self-Verified Premises`" in window
    assert "`## Deferred Premises`" in window


def test_pm_reviewer_adopted_match_is_suppressed_not_downgraded() -> None:
    """An Adopted Assumptions match suppresses the candidate entirely, not a note."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "Matches an entry in `## Adopted Assumptions`", span=400)
    assert "suppress the candidate ambiguity entirely" in window
    assert "do not downgrade it to a note" in window


def test_pm_reviewer_states_park_asymmetry() -> None:
    """A merely-parked, unresolved question still surfaces (the ticket's asymmetry)."""
    content = _agent("product-manager-reviewer.md")
    window = _after(
        content,
        "### Before surfacing: check the plan's own resolution record",
        span=2500,
    )
    assert "Note the asymmetry: a PARKED item stays open" in window


def test_pm_reviewer_what_does_not_count_forward_references_subsection() -> None:
    """The 'What does NOT count' list forward-references the new subsection."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "### What does NOT count", span=800)
    assert "`## Adopted Assumptions`" in window
    assert (
        'see "Before surfacing: check the plan\'s own resolution record" below'
        in window
    )


def test_step1c_otherwise_prompt_notes_resolution_record_sections() -> None:
    """Step 1c's Otherwise-branch prompt calls out the three resolution sections."""
    window = _step1c_prompt_must_include_window()
    assert "- The plan: full text, file list, phases" in window
    assert "`## Adopted Assumptions`" in window
    assert "`## Self-Verified Premises`" in window
    assert "`## Deferred Premises`" in window
    assert "#1593" in window
