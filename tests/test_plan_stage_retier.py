"""Guard tests: Step 1g.0 tier re-verification before the scope-tier stamp (#1897).

Pure-markdown assertions over the auto-dev pipeline instruction files,
following the ``read_text()`` + literal-substring/window convention of
``test_plan_stage_settlement.py`` / ``test_plan_persistence.py``. ``_cmd`` is
imported from ``tests.conftest`` (#1787); ``_after``/``_nearby`` are imported
from ``test_auto_dev_preflight_resolutions`` rather than duplicated;
``_step1g_section`` is imported from ``test_plan_persistence`` (which already
owns the canonical Step 1g slice) rather than re-derived here per the plan's
touch-point contract.

Background: Step 1d classifies scope (small/large) once, but Step 1f.4 can
revise the plan afterward (changing ``## Files Modified`` / line estimates)
without re-running that classification. Step 1g then stamps whatever tier
Step 1d computed *before* the revision onto the *post*-revision plan — a
stale-Small stamp on a plan that actually grew Large silently skips the
Large-tier operator-approval gate at Checkpoint 1. This adds a new Step
1g.0 seam, immediately before the ``**Scope tier:**`` stamp is written, that
mechanically recomputes the tier and blocks (headless) or asks (interactive)
on a mismatch.

``docs/headless-contract.md`` is deliberately NOT touched or tested here —
tracked separately as follow-up issue #1951, per the plan's Adopted
Assumptions.
"""

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_plan_persistence import _step1g_section

STEP1G0_ANCHOR = "**Step 1g.0 — Tier re-verification before stamp (#1897).**"
PERSIST_BULLET = "**FIRST, persist the plan file"
SCOPE_TIER_STALE = "scope_tier_stale"
DEFERRED_STUB_UNRESOLVED = "deferred_stub_unresolved"
STEP1D3_BOUNDARY = "≤10 files AND ≤500 lines AND no forbidden-area touches"
BLOCKING_FINDINGS_HEADER = "## Blocking Review Findings"
STEP1G0_SUBHEADING = "### Step 1g.0 Tier Re-verification — MUST_FIX"


# ---------------------------------------------------------------------------
# 1. Anchor exists inside Step 1g, before the persist bullet.
# ---------------------------------------------------------------------------


def test_step1g0_anchor_exists_before_persist_bullet() -> None:
    """The Step 1g.0 heading exists in Step 1g, before the persist bullet."""
    section = _step1g_section()
    assert STEP1G0_ANCHOR in section
    assert section.index(STEP1G0_ANCHOR) < section.index(PERSIST_BULLET)


# ---------------------------------------------------------------------------
# 2. Re-runs Step 1d's classification against current plan state.
# ---------------------------------------------------------------------------


def test_step1g0_reruns_step1d_classification_against_current_state() -> None:
    """Prose asserts re-running Step 1d's classification against current state."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=500)
    assert "re-run Step 1d's classification" in window
    assert "against the plan text exactly as it stands at this moment" in window


# ---------------------------------------------------------------------------
# 3. Compares against the Step 1d.3 boundary.
# ---------------------------------------------------------------------------


def test_step1g0_compares_against_step1d3_boundary() -> None:
    """Prose references the exact Step 1d.3 small/large boundary."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=1300)
    assert STEP1D3_BOUNDARY in window


# ---------------------------------------------------------------------------
# 4. Silent/proceed behavior when the recomputed tier is unchanged.
# ---------------------------------------------------------------------------


def test_step1g0_silent_when_tier_unchanged() -> None:
    """Prose states the seam proceeds silently when the tier hasn't changed."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=1600)
    assert "Proceed silently whenever the recomputed tier is unchanged" in window


# ---------------------------------------------------------------------------
# 5. scope_tier_stale blocker.reason is defined with stage + retry_eligible.
# ---------------------------------------------------------------------------


def test_scope_tier_stale_blocker_reason_defined_in_plan_md() -> None:
    """The scope_tier_stale blocker.reason appears with stage + retry_eligible."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=2200)
    assert f'`blocker.reason: "{SCOPE_TIER_STALE}"`' in window
    assert '`blocker.stage: "stage1_plan"`' in window
    assert "`retry_eligible: true`" in window


# ---------------------------------------------------------------------------
# 6. Emits stage.errored before the blocked exit.
# ---------------------------------------------------------------------------


def test_scope_tier_stale_emits_stage_errored() -> None:
    """The blocked exit emits cw event record stage.errored with error_kind."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=2600)
    assert "cw event record stage.errored" in window
    assert f'\\"error_kind\\":\\"{SCOPE_TIER_STALE}\\"' in window


# ---------------------------------------------------------------------------
# 7. Persists the draft before the blocked exit.
# ---------------------------------------------------------------------------


def test_scope_tier_stale_persists_draft_before_exit() -> None:
    """Prose states writing the draft to .cw/plan-draft.md before exiting."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=2200)
    assert "write the plan's current text to `.cw/plan-draft.md`" in window


# ---------------------------------------------------------------------------
# 8. Posts the mismatch as a Blocking Review Findings tracker comment.
# ---------------------------------------------------------------------------


def test_scope_tier_stale_posts_blocking_findings_comment() -> None:
    """The blocked exit posts under the fixed Blocking Review Findings header."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=2200)
    assert BLOCKING_FINDINGS_HEADER in window
    assert STEP1G0_SUBHEADING in window


# ---------------------------------------------------------------------------
# 9. Interactive mode asks instead of blocking.
# ---------------------------------------------------------------------------


def test_scope_tier_stale_interactive_asks_instead_of_blocking() -> None:
    """Interactive mode has a distinct AskUserQuestion bullet, after Headless."""
    window = _after(_step1g_section(), STEP1G0_ANCHOR, span=2300)
    assert "**Headless:**" in window
    assert "**Interactive:** AskUserQuestion" in window
    assert window.index("**Headless:**") < window.index("**Interactive:**")


# ---------------------------------------------------------------------------
# 10. Gate-Collapse Table gets a new S1 row between the two existing rows.
# ---------------------------------------------------------------------------


def test_new_blocker_reason_row_in_gate_collapse_table() -> None:
    """A new Gate-Collapse Table row sits between the BLOCK and scope-limit rows."""
    content = _cmd("auto-dev.md")
    lines = content.splitlines()
    block_idx = next(
        i
        for i, ln in enumerate(lines)
        if ln.startswith("| S1 plan reviewer agent BLOCK")
    )
    scope_limit_idx = next(
        i for i, ln in enumerate(lines) if ln.startswith("| S1 scope-limit hit")
    )
    assert scope_limit_idx == block_idx + 2
    new_row = lines[block_idx + 1]
    assert "Step 1g.0" in new_row
    assert f'"{SCOPE_TIER_STALE}"' in new_row
    assert "#1897" in new_row


# ---------------------------------------------------------------------------
# 11. blocker.reason Values table row + stage_reached mapping extension.
# ---------------------------------------------------------------------------


def test_scope_tier_stale_blocker_reason_row_and_stage_mapping() -> None:
    """The Values table gets a scope_tier_stale row and stage_reached lists it."""
    content = _cmd("auto-dev.md")
    lines = content.splitlines()
    stub_idx = next(
        i
        for i, ln in enumerate(lines)
        if ln.startswith(f"| `{DEFERRED_STUB_UNRESOLVED}` |")
    )
    new_row = lines[stub_idx + 1]
    assert new_row.startswith(f"| `{SCOPE_TIER_STALE}` |")
    assert '`blocker.stage` is `"stage1_plan"`' in new_row
    assert "`retry_eligible: true`" in new_row
    assert "(files, lines, forbidden_touched)" in new_row
    assert "#1815" in new_row


# ---------------------------------------------------------------------------
# 12. The marker-requirement sentence forward-references Step 1g.0's tuple.
# ---------------------------------------------------------------------------


def test_scope_tier_marker_requirement_forward_references_step1g0() -> None:
    """The Step 1f.4-revision marker sentence names Step 1g.0's confirmed tuple."""
    section = _step1g_section()
    window = _after(section, "If the plan was loaded from Linear in Step 1a", span=350)
    assert "Step 1g.0" in window
    assert "not an earlier cached one" in window
