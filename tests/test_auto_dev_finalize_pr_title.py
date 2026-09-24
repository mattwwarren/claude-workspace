"""Guard tests: finalize composes the whole-branch PR title, not ship-it's
first-commit heuristic (#2362).

Pipeline PRs were titled after the branch's first substantive commit (ship-it.md
Step 3 Tier 3), which is usually a building block rather than the change itself
— squash-merge then bakes that building-block subject into main's history. Two
incidents on 2026-09-24 (#2358, #2361) were both fixed by hand before merge.

The fix has finalize compose a `PR_TITLE` in its Step 4c.2 agent-prompt
instructions and pass it through `/prep-pr --title`, which `ship-it.md`'s Tier 1
already honors unconditionally ahead of every other tier — see
``tests/test_ship_it_title_tiers.py::test_explicit_title_wins_over_first_commit_helper``
for the runtime pin on that side of the contract. This file follows the
established five-file prose-guard precedent for `.claude/commands/*.md`
(``test_auto_dev_finalize_early_push.py`` et al.): pure substring/region
assertions over the doc text, using the shared ``_cmd``/``_step4c2_section``
readers from ``tests/conftest.py`` (#1787/#2354).
"""

from __future__ import annotations

from tests.conftest import _cmd, _step4c2_section


def test_title_composition_instructions_present() -> None:
    section = _step4c2_section()
    assert ".cw/plan.md" in section
    assert "## Summary" in section
    assert ".cw/context.json" in section
    assert "ticket_title" in section


def test_prep_pr_invocations_include_composed_title() -> None:
    section = _step4c2_section()
    assert "/prep-pr --skip-review --base main --title" in section
    assert "/prep-pr --skip-review --base main --headless --title" in section
    assert "/prep-pr --skip-review --base main --headless --draft --title" in section


def test_old_bare_invocations_no_longer_present() -> None:
    section = _step4c2_section()
    assert "/prep-pr --skip-review --base main`" not in section
    assert "/prep-pr --skip-review --base main --headless`" not in section
    assert "/prep-pr --skip-review --base main --headless --draft`" not in section


def test_numeric_ticket_guard_present_for_title_suffix() -> None:
    """The `(#<ticket_id>)` suffix must only apply to a GitHub-numeric ticket id
    — mirroring ship-it.md's existing `CLOSES_TRAILER`/Tier-5 guard shape — so a
    Linear id or ticket-less interactive run doesn't get a garbage suffix."""
    section = _step4c2_section()
    assert "grep -qE '^[0-9]+$'" in section


def test_title_length_cap_mirrors_ship_it_tier5() -> None:
    section = _step4c2_section()
    assert "cut -c1-72" in section


def test_ship_it_and_prep_pr_unchanged_by_this_fix() -> None:
    """The plan's Touch-point Contract confirmed ship-it.md and prep-pr.md
    already implement their side of the `--title` contract — this fix is
    confined to finalize.md's Step 4c.2 prose."""
    ship_it = _cmd("ship-it.md")
    prep_pr = _cmd("prep-pr.md")
    assert "# Tier 1: Explicit --title override (passed from /prep-pr --title)" in ship_it
    assert "- `--title` if provided" in prep_pr
