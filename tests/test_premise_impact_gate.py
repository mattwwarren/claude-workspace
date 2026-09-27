"""Guard tests: the premise Impact gate and split Verified grammar (#2432).

Pure-markdown assertions over the auto-dev pipeline instruction files and the
headless contract, following the ``read_text()`` + literal-substring/window
convention of ``test_auto_dev_preflight_resolutions.py`` /
``test_plan_stage_settlement.py``.

Background: premises the Product Manager Reviewer had already verified (a
citation whose line number merely drifted) or that changed no code (a claim
orthogonal to the diff) still parked the ticket for an operator round. The
fix adds an orthogonal ``Impact:`` classification whose validly-exempting
``NONE`` (token plus a non-empty ``Impact-Reason:``) routes a premise into the
existing ``self_verified`` bucket, splits the ``Verified:`` line into a bare
closed token plus ``Citation:``/``Reason:`` sub-bullets, and tallies parse
failures in ``malformed_verified_count``.
"""

from pathlib import Path

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_plan_stage_settlement import _step4b_bullet

ROOT = Path(__file__).parent.parent
AGENTS = ROOT / ".claude" / "agents"
CONTRACT = ROOT / "docs" / "headless-contract.md"

IMPACT_HEADING = "### Impact-gated premises — orthogonal to `Verified` (#2432)"
IMPACT_MANDATORY = (
    "**Impact is mandatory on every item too — never omit it, and a bare "
    "`NONE` does not exempt (#2432).**"
)
DRIFTED_CITATION = "**A drifted citation is not a failure (#2432).**"
IMPACT_GATE = (
    "**Impact gate (checked first, ahead of the three-way split above, #2432).**"
)
THREE_WAY = "**Same partition, mirrored for premises — three ways (#1651).**"
MALFORMED_REC_TALLY = "**Malformed-recommendation tally"
MALFORMED_VERIFIED_TALLY = (
    "**Malformed-verified tally (additive, no new bucket, #2432).**"
)
MALFORMED_VERIFIED_NOTE = "**Malformed-verified note (#2432).**"
PREMISES_ROW = "| `premises` | `premises_pending_verification` |"
COERCION_ROW = "| no-impact premise items downgrade |"
NOTE_A16 = "**Note (A16, #2432):**"
NOTE_A15 = "**Note (A15, #2009):**"


def _pm() -> str:
    return (AGENTS / "product-manager-reviewer.md").read_text()


def _contract() -> str:
    return CONTRACT.read_text()


def _premises_block() -> str:
    content = _pm()
    start = content.index("PREMISES TO VERIFY — N items")
    end = content.index("2. ...", start)
    return content[start:end]


def _line_starting(content: str, prefix: str) -> str:
    matches = [line for line in content.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one line starting {prefix!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Product Manager Reviewer (producer) side
# ---------------------------------------------------------------------------


def test_pm_reviewer_impact_subsection_exists() -> None:
    assert IMPACT_HEADING in _pm()


def test_pm_reviewer_impact_orthogonal_to_verified() -> None:
    window = _after(_pm(), IMPACT_HEADING, span=2200)
    assert "`Impact` is evaluated independently of `Verified`" in window
    assert "proceeds without the human regardless of its `Verified` value" in window
    assert "including one that is `NO` or malformed" in window


def test_pm_reviewer_impact_mandatory_fail_closed_to_code_affecting() -> None:
    window = _after(_pm(), IMPACT_MANDATORY, span=900)
    assert "is treated as `CODE-AFFECTING` downstream" in window
    assert "whose `Impact-Reason:` sub-bullet is missing or empty" in window
    assert "tallied together with it in `malformed_verified_count`" in window


def test_pm_reviewer_drifted_citation_is_still_yes() -> None:
    window = _after(_pm(), DRIFTED_CITATION, span=700)
    assert "this is still `Verified: YES`, never `NO`, and never a park" in window
    assert "re-verify against its current location and cite that" in window


def test_pm_reviewer_verified_line_is_bare_closed_token() -> None:
    block = _premises_block()
    assert "   - Verified: YES | NO | DEFER\n" in block
    assert "no other text permitted on that line" in _pm()


def test_pm_reviewer_citation_and_reason_are_own_sub_bullets() -> None:
    block = _premises_block()
    assert "   - Citation: <YES only" in block
    assert "   - Reason: <NO only" in block
    assert "a `YES` missing its `Citation:` sub-bullet" in _pm()


def test_pm_reviewer_output_format_includes_impact_bullet() -> None:
    block = _premises_block()
    assert "   - Impact: NONE | CODE-AFFECTING\n" in block
    assert "   - Impact-Reason: <NONE only, mandatory to exempt" in block
    assert block.index("   - Impact: NONE") < block.index("   - Verified: YES")


# ---------------------------------------------------------------------------
# auto-dev-plan.md (consumer) side
# ---------------------------------------------------------------------------


def test_plan_impact_gate_precedes_three_way_split() -> None:
    content = _cmd("auto-dev-plan.md")
    gate = content.index(IMPACT_GATE)
    assert content.index(THREE_WAY) < gate < content.index(MALFORMED_REC_TALLY)
    window = _after(content, IMPACT_GATE, span=400)
    assert "Before applying the `Verified:`-based split above" in window


def test_plan_impact_gate_absorbs_regardless_of_verified_value() -> None:
    window = _after(_cmd("auto-dev-plan.md"), IMPACT_GATE, span=2600)
    assert (
        "regardless of what its `Verified:` sub-bullet says, including one "
        "that is missing, malformed, or explicitly `NO`" in window
    )
    assert (
        "a premise cannot be both a validly-exempting `Impact: NONE` and "
        "land in `deferred`" in window
    )


def test_plan_impact_gate_reuses_self_verified_plumbing() -> None:
    window = _after(_cmd("auto-dev-plan.md"), IMPACT_GATE, span=3000)
    assert "reuses every existing self_verified mechanism unchanged" in window
    assert "key on bucket membership, not on which condition produced it" in window


def test_plan_impact_gate_requires_nonempty_reason_to_exempt() -> None:
    window = _after(_cmd("auto-dev-plan.md"), IMPACT_GATE, span=900)
    assert "AND is accompanied by a non-empty `Impact-Reason:` sub-bullet" in window


def test_plan_impact_none_without_reason_falls_through_as_malformed() -> None:
    window = _after(_cmd("auto-dev-plan.md"), IMPACT_GATE, span=2200)
    assert "a `NONE` token whose `Impact-Reason:` sub-bullet is missing or empty" in (
        window
    )
    assert "is additionally tallied in `malformed_verified_count`" in window


def test_plan_malformed_impact_token_falls_through_as_malformed() -> None:
    window = _after(_cmd("auto-dev-plan.md"), IMPACT_GATE, span=2200)
    assert (
        "any other unparseable/malformed `Impact:` value — is treated as "
        "`CODE-AFFECTING`" in window
    )
    assert "a malformed `Impact:` token is additionally tallied" in window


def test_plan_malformed_verified_tally_is_additive_only() -> None:
    window = _after(_cmd("auto-dev-plan.md"), MALFORMED_VERIFIED_TALLY, span=1800)
    assert "does not introduce a fourth partition bucket" in window
    assert "not guaranteed to be a subset count of `unverified`" in window
    assert "regardless of which bucket each ultimately lands in" in window
    assert "whose `Impact:` sub-bullet was missing" in window


def test_plan_stage_entered_payload_has_malformed_verified_count() -> None:
    content = _cmd("auto-dev-plan.md")
    assert (
        '\\"malformed_recommendation_count\\":<M>,\\"malformed_verified_count\\":<V>'
        in content
    )
    assert "`<V>` is likewise a literal placeholder for `malformed_verified_count`" in (
        content
    )
    assert "NOT guaranteed always-`0` on the AUTO-CONTINUE path (#2432)" in content
    assert "malformed/missing-`Impact:` component" in content


def test_plan_malformed_verified_note_is_count_only() -> None:
    content = _cmd("auto-dev-plan.md")
    window = _after(content, MALFORMED_VERIFIED_NOTE, span=1000)
    assert (
        "`Note: <malformed_verified_count> premise item(s) this scan carried a "
        "missing/malformed Verified field" in window
    )
    assert "deliberately NOT worded as" in window
    assert "A count-only note — no per-item classification" in window
    assert content.index("**Malformed-recommendation note (#1274).**") < (
        content.index(MALFORMED_VERIFIED_NOTE)
    )


def test_plan_step4b_self_verified_bullet_cites_impact_reason_alternative() -> None:
    bullet = _step4b_bullet("If `self_verified` is non-empty")
    assert "`Verified: YES` sub-bullet's `Citation:` line" in bullet
    assert "when a prior citation had drifted, #2432" in bullet
    assert "when routed via the Impact gate, the item's `Impact-Reason:` text" in (
        bullet
    )


def test_plan_friction_highlights_format_distinguishes_impact_routed_items() -> None:
    bullet = _step4b_bullet("For each `self_verified` premise")
    assert "`self-verified premise: <claim> — <evidence citation>`" in bullet
    assert "`self-verified premise (no impact): <claim> — <Impact-Reason: text>`" in (
        bullet
    )
    assert "a `NONE` without one never produces this line" in bullet


def test_plan_no_orphan_invariant_covers_impact_gated_items() -> None:
    bullet = _step4b_bullet("**Completed no-orphan invariant (#1683).**")
    assert "Impact-gated `self_verified` items get identical treatment (#2432)" in (
        bullet
    )
    assert "bucket-keyed, not entry-condition-keyed" in bullet


def test_plan_interactive_premises_skip_askuserquestion_for_no_impact() -> None:
    line = _line_starting(
        _cmd("auto-dev-plan.md"), "   - **`PREMISES TO VERIFY — N items`** →"
    )
    assert "skips the AskUserQuestion entirely (#2432)" in line
    assert "a `NONE` without `Impact-Reason:`" in line
    assert "is still asked about" in line


# ---------------------------------------------------------------------------
# docs/headless-contract.md (wire contract) side
# ---------------------------------------------------------------------------


def test_headless_contract_premises_key_union_includes_impact() -> None:
    row = _line_starting(_contract(), PREMISES_ROW)
    assert "`impact`" in row
    assert "#2432" in _contract()


def test_headless_contract_malformed_verified_count_documented() -> None:
    content = _contract()
    section = content[content.index("### 10.3 Payload Schema") :]
    section = section[: section.index("### 10.4")]
    bullet = _line_starting(section, "- `malformed_verified_count` (int)")
    assert "`s1_ambiguity_scan_complete` only" in bullet
    assert "not guaranteed always-`0`" in bullet
    assert "#2432" in bullet


def test_headless_contract_no_impact_coercion_row_and_note_a16() -> None:
    content = _contract()
    row = _line_starting(content, COERCION_ROW)
    assert row.rstrip().endswith("| #2432 |")
    assert "**rewrite `status` to `stage_complete`**" in row
    assert NOTE_A16 in content
    assert content.index(NOTE_A15) < content.index(NOTE_A16)
    note = _after(content, NOTE_A16, span=700)
    assert "No version bump" in note


def test_headless_contract_impact_reason_key_documented() -> None:
    row = _line_starting(_contract(), PREMISES_ROW)
    assert "`impact` / `impact_reason`" in row


def test_headless_contract_no_impact_dual_condition_documented() -> None:
    row = _line_starting(_contract(), COERCION_ROW)
    assert "`impact` normalized (strip+lower) to exactly `none`" in row
    assert "**AND** a non-empty `impact_reason` string" in row
    assert "does NOT trigger this row" in row
