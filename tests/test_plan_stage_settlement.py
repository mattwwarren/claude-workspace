"""Guard tests: settlement contract (R1-R6) for Step 1c's premise scan (#1683).

Pure-markdown assertions over the auto-dev pipeline instruction files,
following the ``read_text()`` + literal-substring/window convention of
``test_auto_dev_preflight_resolutions.py`` / ``test_plan_persistence.py`` /
``test_consolidated_park.py``. ``_cmd``/``_agent`` are duplicated locally per
the established convention; ``_after``/``_nearby`` are imported from
``test_auto_dev_preflight_resolutions`` and
``_step1c_prompt_must_include_window`` from
``test_ambiguity_scan_adopted_assumptions`` rather than re-derived.

Background: an operator answering a parked ambiguity in an ordinary ticket
comment had no way to tell the next round's scan that the question was
settled, so the same questions re-raised round after round with no bound on
the loop. This adds two pipeline-authored bookkeeping lines in
``.cw/plan-draft.md`` — a round counter (cap 2) and one closed-grammar
``plan-stage-settled`` marker per settled item — plus a ``## Settled Plan
Items`` plan-body accumulator that the Product Manager Reviewer excludes by
content identity. Neither marker is ever posted to the tracker, and nothing
in the mechanism redacts, truncates, or pre-verifies any ticket-comment text:
the reviewer always receives the complete live-fetched stream.
"""

from pathlib import Path

from tests.test_ambiguity_scan_adopted_assumptions import (
    _step1c_prompt_must_include_window,
)
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
AGENTS = ROOT / ".claude" / "agents"

DRAFT_FILE = ".cw/plan-draft.md"
ROUND_MARKER = "plan-stage-scan-round"
SETTLED_MARKER = "plan-stage-settled"
SETTLED_SECTION = "## Settled Plan Items"
PENDING_STUB = "PENDING — agent must supply on next scan"
UNCONVERGED = "ambiguity_scan_unconverged"
STUB_UNRESOLVED = "deferred_stub_unresolved"

STEP1C0_ANCHOR = (
    "**Step 1c.0 — Round-cap read + settlement folding (resumed rounds only).**"
)
SEAM_ANCHOR = "**Pre-branch integrity checks (single seam, #1683).**"
STUB_CHECK_ANCHOR = "**Stub check (first).**"
CAP_CHECK_ANCHOR = "**Cap check (second).**"
STEP4C_ANCHOR = "**Step 4c — Exit/continue decision.**"
SOURCE_LIST_ANCHOR = "1. **Source the ambiguity list.**"

EXIT_BULLET_ANCHORS = (
    "`parked` non-empty AND `unverified` empty → EXIT `ambiguities_pending_resolution`",
    "`unverified` non-empty AND `parked` empty → EXIT `premises_pending_verification`",
    "`unverified` non-empty AND `parked` non-empty → EXIT "
    "`premises_pending_verification`",
)

DEFERRED_WIRING_ANCHOR = "**`DEFERRED` wiring (R7, active registration):**"

PM_SETTLED_HEADING = "### Settled items are out of scope (do not re-raise)"
PM_1593_HEADING = "### Before surfacing: check the plan's own resolution record"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _agent(name: str) -> str:
    return (AGENTS / name).read_text()


def _step1a_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1a: Check for Existing Plan")
    end = content.index("### Step 1b:")
    return content[start:end]


def _step1c_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1c: Ambiguity Verification")
    end = content.index("### Step 1d:")
    return content[start:end]


def _step1c_headless_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("4. **Headless mode:**")
    end = content.index("### Step 1d:")
    return content[start:end]


def _step1c0_block() -> str:
    section = _step1c_section()
    start = section.index(STEP1C0_ANCHOR)
    end = section.index(SOURCE_LIST_ANCHOR, start)
    return section[start:end]


def _step4b_section() -> str:
    section = _step1c_headless_section()
    start = section.index("**Step 4b — Plan-body disposition.**")
    end = section.index(SEAM_ANCHOR)
    return section[start:end]


def _step4b_bullet(needle: str) -> str:
    """Return the single Step 4b bullet line containing ``needle``.

    Step 4b's bullets are one long line each, so line-level extraction is an
    exact bullet boundary — no span guessing that could bleed into a sibling
    bullet and pass on the wrong text.
    """
    matches = [
        line
        for line in _step4b_section().splitlines()
        if line.lstrip().startswith("- ") and needle in line
    ]
    assert len(matches) == 1, f"expected exactly one Step 4b bullet for {needle!r}"
    return matches[0]


def _seam() -> str:
    section = _step1c_headless_section()
    start = section.index(SEAM_ANCHOR)
    end = section.index(STEP4C_ANCHOR, start)
    return section[start:end]


def _stub_check_block() -> str:
    seam = _seam()
    start = seam.index(STUB_CHECK_ANCHOR)
    end = seam.index(CAP_CHECK_ANCHOR, start)
    return seam[start:end]


def _cap_check_block() -> str:
    seam = _seam()
    return seam[seam.index(CAP_CHECK_ANCHOR) :]


def _exit_bullet(anchor: str) -> str:
    section = _step1c_headless_section()
    idx = section.index(anchor)
    line_start = section.rindex("\n", 0, idx) + 1
    line_end = section.index("\n", idx)
    return section[line_start:line_end]


# ---------------------------------------------------------------------------
# Round-cap mechanism
# ---------------------------------------------------------------------------


def test_step1a0_resume_reads_round_counter() -> None:
    """Step 1a.0's resume check reads the persisted round counter."""
    section = _step1a_section()
    assert ROUND_MARKER in section
    window = _after(section, "**Round counter (#1683):**", span=500)
    assert DRAFT_FILE in window
    assert f"reads `{ROUND_MARKER}`" in window
    assert "never increments the counter itself" in window


def test_step1c0_new_step_exists_before_source_ambiguity_list() -> None:
    """The new Step 1c.0 block precedes 'Source the ambiguity list'."""
    section = _step1c_section()
    assert STEP1C0_ANCHOR in section
    assert section.index(STEP1C0_ANCHOR) < section.index(SOURCE_LIST_ANCHOR)


def test_step1c0_fires_only_on_resumed_rounds() -> None:
    """Step 1c.0 is scoped to dispatches where Step 1a.0's resume branch fired."""
    block = _step1c0_block()
    assert "Fires only when Step 1a.0's resume branch fired this dispatch" in block
    assert "on a fresh (non-resumed) dispatch skip straight to step 1 below" in block


def test_cap_value_is_two() -> None:
    """The literal cap 2 is stated adjacent to the round-counter marker."""
    section = _step1c_section()
    window = _after(section, f"**Round counter (`{ROUND_MARKER}`, #1683).**", span=400)
    assert "cap 2" in window
    assert "incremented once per park EXIT" in window
    assert "AUTO-CONTINUE never does" in window
    cap = _cap_check_block()
    assert "If `N < 2`" in cap
    assert "If `N >= 2` (cap reached)" in cap


def test_cap_check_present_once_in_single_seam_not_duplicated_per_exit() -> None:
    """Both checks live once, in the pre-branch seam, stub check first."""
    content = _cmd("auto-dev-plan.md")
    assert content.count(SEAM_ANCHOR) == 1
    assert content.count(STUB_CHECK_ANCHOR) == 1
    assert content.count(CAP_CHECK_ANCHOR) == 1

    seam = _seam()
    assert seam.index(STUB_CHECK_ANCHOR) < seam.index(CAP_CHECK_ANCHOR)

    # The seam sits between Step 4b's last bullet and Step 4c's header.
    section = _step1c_headless_section()
    assert section.index("**Step 4b — Plan-body disposition.**") < section.index(
        SEAM_ANCHOR
    )
    assert section.index(SEAM_ANCHOR) < section.index(STEP4C_ANCHOR)

    # Neither check is duplicated immediately before any Step 4c EXIT bullet.
    for anchor in EXIT_BULLET_ANCHORS:
        preceding = _nearby(section, anchor, span=600)
        assert STUB_CHECK_ANCHOR not in preceding
        assert CAP_CHECK_ANCHOR not in preceding


def test_exit_bullets_carry_no_inline_check_text() -> None:
    """The three Step 4c EXIT bullets carry no inline stub- or cap-check text."""
    for anchor in EXIT_BULLET_ANCHORS:
        bullet = _exit_bullet(anchor)
        assert "Cap check" not in bullet
        assert "Stub check" not in bullet
        assert "round-counter" not in bullet
        assert ROUND_MARKER not in bullet
        assert PENDING_STUB not in bullet


def test_cap_check_scoped_to_exit_branches_not_auto_continue() -> None:
    """The cap check's trigger stays scoped to the three EXIT-bullet outcomes."""
    cap = _cap_check_block()
    assert "Reachable only when the stub check passed" in cap
    assert "`parked` non-empty OR `unverified` non-empty" in cap
    assert "AUTO-CONTINUE never reaches this check" in cap


def test_ambiguity_scan_unconverged_blocker_reason_defined_in_plan_md() -> None:
    """Cap exhaustion hard-EXITs headless with the new blocker reason."""
    cap = _cap_check_block()
    assert f'`blocker.reason: "{UNCONVERGED}"`' in cap
    assert '`blocker.stage: "stage1_plan"`' in cap
    assert '`status: "blocked"`' in cap
    assert "`retry_eligible: true`" in cap
    assert "**through the consolidated park above**" in cap
    assert "naming the still-open item(s) verbatim" in cap


def test_ambiguity_scan_unconverged_emits_stage_errored() -> None:
    """Cap exhaustion emits a stage.errored mirroring the Step 1f.4 block."""
    cap = _cap_check_block()
    assert "cw event record stage.errored" in cap
    assert '--correlation-id "$TICKET"' in cap
    assert f'\\"error_kind\\":\\"{UNCONVERGED}\\"' in cap
    assert '\\"stage\\":\\"s1_ambiguity_scan_complete\\"' in cap
    assert "Emit the `stage.errored` block below before the `blocked` sentinel." in cap


def test_interactive_cap_reached_asks_instead_of_blocking() -> None:
    """Interactive mode asks the operator instead of hard-blocking at the cap."""
    cap = _cap_check_block()
    assert "Interactive mode does NOT proceed with that EXIT either" in cap
    assert "AskUserQuestion" in cap
    assert "parked for 2 consecutive rounds without converging" in cap
    assert "Continue for one more round" in cap
    assert "`## Open Questions`" in cap


def test_draft_persistence_rule_mentions_settled_items_counter_and_marker() -> None:
    """The draft-persistence rule captures the new sections and marker lines."""
    section = _step1c_headless_section()
    window = _after(section, "**Draft-persistence rule", span=1600)
    assert "## Adopted Assumptions" in window
    assert "## Self-Verified Premises" in window
    assert "## Deferred Premises" in window
    assert SETTLED_SECTION in window
    assert f"`<!-- {ROUND_MARKER}: N -->` round-counter first line" in window
    assert f"every `<!-- {SETTLED_MARKER}: ... -->` marker line" in window


# ---------------------------------------------------------------------------
# R1-R6 acceptance (binding, per the ticket's contract)
# ---------------------------------------------------------------------------


def test_settled_marker_grammar_is_closed() -> None:
    """The marker grammar is stated exhaustively; trailing content is invalid."""
    section = _step1c_section()
    window = _after(
        section,
        f"**Settlement marker grammar (`{SETTLED_MARKER}`, #1683).**",
        span=1600,
    )
    assert "A<n>: ADOPTED" in window
    assert "A<n>: ALT-<x>" in window
    assert "P<n>: CONFIRMED | REFUTED | DEFERRED" in window
    assert "these three forms are the ENTIRE grammar, and nothing else is valid" in (
        window
    )
    assert (
        "A line whose content is anything other than the exact grammar above "
        "(trailing text, a label, operator prose) is non-conforming and MUST "
        "NOT be written" in window
    )
    assert (
        "The grammar has no optional position, no label position, and no "
        "trailing-text position." in window
    )


def test_no_marker_comment_on_ticket() -> None:
    """Settlement markers live only in the draft file, never on the tracker."""
    content = _cmd("auto-dev-plan.md")
    assert f"Settlement markers live only in `{DRAFT_FILE}`" in content
    assert "may carry a settlement marker of any kind" in content
    for forbidden in (
        f"post a `{SETTLED_MARKER}`",
        f"post the `{SETTLED_MARKER}`",
        f"`{SETTLED_MARKER}` marker to the ticket",
        f"`{SETTLED_MARKER}` marker to the tracker",
    ):
        assert forbidden not in content


def test_pm_prompt_receives_full_stream_plus_settled_identity_list() -> None:
    """The scan prompt gains the settled list additively — the stream is intact."""
    window = _step1c_prompt_must_include_window()
    assert "ALL ticket comments in chronological order" in window
    assert SETTLED_SECTION in window
    assert "**Settled items from prior rounds (do not re-raise):**" in window
    assert "closed by identity only" in window
    assert "never redacts, exempts, or pre-verifies any ticket-comment text" in window
    assert "must evaluate every claim in it on its own merits" in window


def test_no_redaction_paragraph_states_full_stream_always() -> None:
    """Step 1c.0 states the reviewer always gets the complete, unredacted stream."""
    section = _step1c_section()
    window = _after(section, "**No redaction, anywhere (R3).**", span=1200)
    assert "complete, unredacted live-fetched stream, always" in window
    assert (
        "Nothing in Step 1c.0 removes, truncates, or placeholders any span of "
        "ticket-comment text." in window
    )
    assert "passed alongside (never instead of) the full stream" in window
    assert "re-scrutinized by every subsequent scan, forever" in window


def test_cap_check_prose_is_self_contained_not_pointer_reference() -> None:
    """Cap check + stage.errored carry real content, not a 'see prior draft' pointer."""
    cap = _cap_check_block()
    for required in (
        "Read `.cw/plan-draft.md`'s round-counter line",
        "If `N < 2`",
        "If `N >= 2` (cap reached)",
        f'`blocker.reason: "{UNCONVERGED}"`',
        "AskUserQuestion",
        "cw event record stage.errored",
        "--payload",
    ):
        assert required in cap
    for pointer in (
        "reused from",
        "unchanged from the round-2 draft",
        "as in the prior draft",
        "see the prior draft",
    ):
        assert pointer not in cap


def test_pm_reviewer_settled_items_content_match_not_redaction() -> None:
    """The PM Reviewer subsection excludes by content match and exempts no text."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, PM_SETTLED_HEADING, span=2600)
    assert "**Exclusion is by content match, not by number.**" in window
    assert "never key the exclusion on `A3`/`P2` alone" in window
    assert (
        "**The list never redacts, exempts, or pre-verifies any ticket-comment "
        "text.**" in window
    )
    assert "complete, unredacted comment stream" in window
    assert "it confers no immunity on any *text*" in window


def test_unmappable_operator_reply_stays_open() -> None:
    """An unmappable reply settles nothing — the item stays open, scanned as text."""
    block = _step1c0_block()
    assert "is **unmappable**: default to unmappable on any doubt" in block
    assert (
        "An unmappable item is not settled; it stays open and is scanned as "
        "ordinary ticket text like any other comment in the spawn below — no "
        "special handling, no partial credit." in block
    )
    assert (
        "The only permitted output of this step is one closed token from the "
        "grammar above, per item." in block
    )


def test_four_historical_rounds_structurally_impossible() -> None:
    """One assertion per historical exploit shape this contract closes."""
    content = _cmd("auto-dev-plan.md")
    block = _step1c0_block()

    # (1) No "trust the whole comment body" shape anywhere in Step 1c.0.
    assert "trust the whole comment body" not in block
    assert "whole comment body" not in block

    # (2) No line-level redaction instruction anywhere in the file: every
    #     "redact" occurrence sits inside explicitly negated prose.
    negation_cues = ("no redaction", "unredact", "never redact", "not redact")
    idx = content.find("redact")
    assert idx != -1, "the no-redaction prose must exist"
    while idx != -1:
        window = content[max(0, idx - 80) : idx + 80].lower()
        assert any(cue in window for cue in negation_cues), (
            f"un-negated 'redact' at offset {idx}"
        )
        idx = content.find("redact", idx + 1)

    # (3) No "mechanically extract the bare decision text" shape — closed-token
    #     classification is the only permitted transcription output.
    for forbidden in (
        "extract the bare decision",
        "bare decision text",
        "mechanically extract",
    ):
        assert forbidden not in content

    # (4) The grammar has no optional/label/trailing-text position.
    assert (
        "The grammar has no optional position, no label position, and no "
        "trailing-text position." in content
    )


# ---------------------------------------------------------------------------
# PM Reviewer settled-items exclusion
# ---------------------------------------------------------------------------


def test_pm_settled_subsection_exists_and_is_distinct_from_1593_check() -> None:
    """The new subsection is separate from, and follows, the #1593 check."""
    content = _agent("product-manager-reviewer.md")
    assert PM_SETTLED_HEADING in content
    assert PM_1593_HEADING in content
    assert content.index(PM_1593_HEADING) < content.index(PM_SETTLED_HEADING)

    window = _after(content, PM_SETTLED_HEADING, span=900)
    assert "Distinct from — and additional to — the resolution-record check" in window
    assert "#1593" in window
    assert SETTLED_SECTION in window
    assert "`auto-dev-plan.md` Step 1c.0" in window


def test_pm_reviewer_alternatives_are_lettered() -> None:
    """Mode 1's output format requires a lettered alternatives list."""
    content = _agent("product-manager-reviewer.md")
    assert (
        "- Alternative(s) the ticket also supports: a lettered list — "
        "`(a) <alternative>`, `(b) <alternative>`, …" in content
    )
    window = _after(content, "- Alternative(s) the ticket also supports:", span=400)
    assert "always lettered, even when there is only one alternative" in window
    assert "`ALT-b`" in window


# ---------------------------------------------------------------------------
# auto-dev.md appendix rows
# ---------------------------------------------------------------------------


def test_new_blocker_reason_rows_and_stage_reached_mapping() -> None:
    """Both new blocker reasons get an appendix row and the stage_reached map."""
    content = _cmd("auto-dev.md")
    lines = content.splitlines()
    unsound_idx = next(
        i for i, ln in enumerate(lines) if ln.startswith("| `plan_unsound` |")
    )
    assert lines[unsound_idx + 1].startswith(f"| `{UNCONVERGED}` |")
    assert lines[unsound_idx + 2].startswith(f"| `{STUB_UNRESOLVED}` |")

    assert (
        '- `blocked` with `blocker.reason: "plan_unreviewable"`, '
        '`"plan_unsound"`, `"ambiguity_scan_unconverged"`, or '
        '`"deferred_stub_unresolved"` → `"stage1_plan"`' in content
    )


def test_unconverged_row_names_round_cap_and_stage() -> None:
    """The unconverged row explains the cap and pins blocker.stage."""
    content = _cmd("auto-dev.md")
    row = next(
        ln for ln in content.splitlines() if ln.startswith(f"| `{UNCONVERGED}` |")
    )
    assert "2 consecutive rounds" in row
    assert ROUND_MARKER in row
    assert '`blocker.stage` is `"stage1_plan"`' in row
    assert "#1683" in row


def test_stub_unresolved_row_names_pending_stub_and_stage() -> None:
    """The stub row explains the un-enforced halt check and pins blocker.stage."""
    content = _cmd("auto-dev.md")
    row = next(
        ln for ln in content.splitlines() if ln.startswith(f"| `{STUB_UNRESOLVED}` |")
    )
    assert PENDING_STUB in row
    assert "AUTO-CONTINUE included" in row
    assert '`blocker.stage` is `"stage1_plan"`' in row
    assert "#1683" in row


# ---------------------------------------------------------------------------
# DEFERRED premise wiring (R7 — active registration)
# ---------------------------------------------------------------------------


def test_deferred_settlement_transcribes_like_confirmed_for_suppression() -> None:
    """DEFERRED suppresses re-raising identically to CONFIRMED at settlement."""
    block = _step1c0_block()
    window = _after(block, "**`DEFERRED` (R7, active registration):**", span=900)
    assert (
        "transcribes identically to `CONFIRMED` for re-raise-suppression purposes"
        in window
    )
    assert "audit-only and carries no different settlement behavior" in window
    assert (
        "Settling a premise `DEFERRED` never itself writes an "
        "`In-implementation check:`/`On mismatch:` pair from operator prose" in window
    )


def test_deferred_settlement_writes_pending_stub_to_deferred_premises() -> None:
    """A DEFERRED settlement actively registers a PENDING stub at settlement time."""
    block = _step1c0_block()
    window = _after(block, DEFERRED_WIRING_ANCHOR, span=1400)
    assert "ALSO writes a stub entry to `## Deferred Premises` at settlement time" in (
        window
    )
    assert "carries only the plan-authored claim text" in window
    assert "never operator prose" in window
    assert f"marked `{PENDING_STUB}`" in window
    assert "The stub is not itself a runtime check" in window


def test_pm_prompt_requires_classifying_pending_stub_next_scan() -> None:
    """The scan prompt makes an outstanding PENDING stub a required target."""
    window = _step1c_prompt_must_include_window()
    assert PENDING_STUB in window
    assert "MUST classify that exact claim's `Verified:` status" in window
    assert "a required classification target, not an optional re-discovery" in window


def test_deferred_claim_independently_reproposable_next_scan() -> None:
    """The agent's own next-scan classification — never the settlement — resolves it."""
    block = _step1c0_block()
    window = _after(block, DEFERRED_WIRING_ANCHOR, span=2400)
    assert (
        "the agent supplies its own `In-implementation check:`/`On mismatch:` "
        "pair (never transcribed from operator prose)" in window
    )
    assert "replacing the stub's placeholder pair" in window
    assert (
        "reopens as an ordinary unverified premise, subject to Step 4c gating "
        "like any other" in window
    )
    assert (
        "A stub that survives past its immediately-next scan without being "
        "resolved is a defect in this mechanism, not an accepted steady state" in window
    )
    assert "the pre-branch stub check below hard-blocks the round" in window


def test_pm_settled_items_deferred_carveout_stated() -> None:
    """The exclusion explicitly does not cover the two DEFERRED duties."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "**`DEFERRED` carve-out (do not over-read the", span=1400)
    assert "must never be read as blanket immunity for the claim" in window
    assert "the mandatory next-scan stub classification" in window
    assert "leaving it unclassified blocks the round" in window
    assert "ordinary independent premise proposal on any later scan" in window


# ---------------------------------------------------------------------------
# Deferred-stub resolution enforcement (Step 4b identity match + seam check)
# ---------------------------------------------------------------------------


def test_step4b_deferred_bucket_matches_pending_stub_and_replaces_in_place() -> None:
    """A DEFER re-classification updates the matched stub in place, never appends."""
    bullet = _step4b_bullet("If `deferred` is non-empty")
    assert "**Stub-identity match (#1683).**" in bullet
    assert f"still reads `{PENDING_STUB}`" in bullet
    assert (
        "exact-string claim-text match is sufficient identity — no separate "
        "fingerprint field" in bullet
    )
    assert "update that entry **in place**" in bullet
    assert "remove the `PENDING` marker" in bullet
    assert "never append a second, duplicate entry for the same claim" in bullet


def test_step4b_unverified_bucket_removes_matched_stub_on_no() -> None:
    """A Verified: NO of a stubbed claim removes the stub instead of orphaning it."""
    bullet = _step4b_bullet("**`unverified`-bucket stub-identity match (#1683).**")
    assert "a `Verified: NO` classification of a previously-stubbed claim" in bullet
    assert "remove that stub entry from `## Deferred Premises` entirely" in bullet
    assert "reopens as an ordinary unverified premise" in bullet
    assert "never left behind as an orphaned `PENDING` stub" in bullet


def test_step4b_self_verified_bucket_removes_matched_stub_on_yes() -> None:
    """A Verified: YES of a stubbed claim removes the stub outright."""
    bullet = _step4b_bullet("If `self_verified` is non-empty")
    assert "**Stub-identity match (#1683).**" in bullet
    assert "remove that stub entry from `## Deferred Premises` entirely" in bullet
    assert (
        "a `Verified: YES` reclassification fully confirms the claim, so there "
        "is nothing to update in place" in bullet
    )

    invariant = _step4b_bullet("**Completed no-orphan invariant (#1683).**")
    assert "`deferred` (stub updated in place)" in invariant
    assert "`unverified` (stub removed, claim reopens as an ordinary premise)" in (
        invariant
    )
    assert "`self_verified` (stub removed, claim confirmed)" in invariant
    assert "can leave a matched `PENDING` stub behind" in invariant


def test_stub_check_present_once_in_single_seam_not_duplicated_per_exit() -> None:
    """The stub check lives once, in the seam, and not per EXIT bullet."""
    content = _cmd("auto-dev-plan.md")
    assert content.count(STUB_CHECK_ANCHOR) == 1
    assert STUB_CHECK_ANCHOR in _seam()
    section = _step1c_headless_section()
    for anchor in EXIT_BULLET_ANCHORS:
        assert STUB_CHECK_ANCHOR not in _nearby(section, anchor, span=600)


def test_stub_check_blocks_auto_continue_path() -> None:
    """The stub check gates all four Step 4c outcomes, AUTO-CONTINUE included."""
    seam = _seam()
    assert "covering all four Step 4c outcomes, AUTO-CONTINUE included" in seam
    stub = _stub_check_block()
    assert (
        "the round does NOT proceed to ANY of Step 4c's four outcomes, "
        "AUTO-CONTINUE (`parked` empty AND `unverified` empty) included" in stub
    )


def test_deferred_stub_unresolved_blocker_reason_defined_in_plan_md() -> None:
    """An unresolved stub hard-EXITs headless with the sibling blocker reason."""
    stub = _stub_check_block()
    assert f'`blocker.reason: "{STUB_UNRESOLVED}"`' in stub
    assert '`blocker.stage: "stage1_plan"`' in stub
    assert '`status: "blocked"`' in stub
    assert "`retry_eligible: true`" in stub
    assert "**through the consolidated park above**" in stub
    assert "naming the unresolved stub(s) verbatim" in stub
    assert "never dropped behind a stub-only block" in stub


def test_deferred_stub_unresolved_emits_stage_errored() -> None:
    """The stub check emits a sibling stage.errored with the same payload shape."""
    stub = _stub_check_block()
    assert "cw event record stage.errored" in stub
    assert '--correlation-id "$TICKET"' in stub
    assert f'\\"error_kind\\":\\"{STUB_UNRESOLVED}\\"' in stub
    assert '\\"stage\\":\\"s1_ambiguity_scan_complete\\"' in stub
    assert "Emit the `stage.errored` block below before the `blocked` sentinel." in stub


def test_stub_check_fires_on_unclassified_stub() -> None:
    """An unresolved stub blocks the round in both modes, never proceeding silently."""
    stub = _stub_check_block()
    assert f"any entry whose check pair still reads `{PENDING_STUB}`" in stub
    assert "Interactive mode instead **AskUserQuestion:**" in stub
    assert "were not classified on their required next scan" in stub
    assert "abandon the ticket?" in stub


def test_stub_check_silent_when_all_stubs_classified() -> None:
    """A fully-resolved round passes the stub check without blocking."""
    stub = _stub_check_block()
    assert (
        "If none remain, the check passes silently and evaluation proceeds to "
        "the cap check below — a fully-resolved round never trips it." in stub
    )
