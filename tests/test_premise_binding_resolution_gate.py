"""Guards for #1411: premises closed by a pre-existing binding resolution.

The plan-stage partition gains a Resolution gate (Step 4a) that routes a
premise answered by an operator decision on record into ``self_verified``.
The Product Manager Reviewer emits the ``Resolution:`` sub-bullet that feeds
it. These tests pin the prose contract and the wire shape the #1325 parser
must keep dropping.
"""

from pathlib import Path

import pytest

from cw.auto_dev_result import AutoDevResult, parse_stdout
from tests.conftest import _cmd
from tests.test_ambiguity_scan_adopted_assumptions import (
    _step1c_prompt_must_include_window,
)
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_auto_dev_result import (
    _premise_item,
    _premises_pending_payload,
    _wrap_sentinel,
)
from tests.test_plan_stage_settlement import _step4b_bullet
from tests.test_premise_impact_gate import (
    IMPACT_GATE,
    IMPACT_HEADING,
    MALFORMED_REC_TALLY,
    MALFORMED_VERIFIED_TALLY,
    THREE_WAY,
    _contract,
    _line_starting,
    _pm,
    _premises_block,
)

ROOT = Path(__file__).parent.parent
PM_HEADING = "### Premises closed by a binding resolution — `Resolution:` (#1411)"
PM_OPTIONAL = "**Resolution is optional and fail-closed (#1411).**"
PM_BEFORE_SURFACING = "### Before surfacing: check the plan's own resolution record"
RESOLUTION_GATE = (
    "**Resolution gate (checked second, after the Impact gate and ahead of the "
    "three-way split, #1411).**"
)
PRODUCER_NOTE = "**Producer note (#1411, no schema change):**"
PARSER_NOTE = "**Parser note (#1325, no schema change):**"
RESOLVER = ROOT / "src" / "cw" / "auto_dev_result" / "_premises_resolution.py"
PLAN_ROUND_COUNTER = "<!-- el: plan.round-counter -->"
PLAN_FINGERPRINT_RULE = "<!-- el: plan.plan-draft-fingerprint-rule -->"


# ---------------------------------------------------------------------------
# Product Manager Reviewer contract
# ---------------------------------------------------------------------------


def test_pm_reviewer_has_binding_resolution_subsection() -> None:
    pm = _pm()
    assert PM_HEADING in pm
    assert (
        pm.index(IMPACT_HEADING) < pm.index(PM_HEADING) < pm.index(PM_BEFORE_SURFACING)
    )


def test_pm_reviewer_resolution_is_authority_not_evidence_bar() -> None:
    window = _after(_pm(), PM_HEADING, span=2200)
    assert "authority decision, not an evidence-bar question" in window
    assert "neither admits nor needs it" in window
    assert "## Binding Pre-flight Resolutions" in window
    assert "<!-- auto-dev-preflight-resolutions -->" in window
    assert "## Operator resolutions recorded on the dev-queue row (binding)" in window


def test_pm_reviewer_resolution_forms_and_verbatim_quote() -> None:
    window = _after(_pm(), PM_HEADING, span=2200)
    assert "`R<n>`" in window
    assert "`row:<round>`" in window
    assert "character-for-character" in window
    assert "evaluated independently of both" in window


def test_pm_reviewer_resolution_optional_fail_closed() -> None:
    window = _after(_pm(), PM_OPTIONAL, span=1000)
    assert "never inferred" in window
    assert "treated as absent" in window
    assert "ordinary" in window
    assert "routing" in window


def test_pm_reviewer_premises_block_has_resolution_bullet_before_verified() -> None:
    block = _premises_block()
    assert "   - Resolution: <optional" in block
    assert (
        block.index("   - Impact-Reason:")
        < block.index("   - Resolution:")
        < block.index("   - Verified: YES")
    )


def test_pm_reviewer_omit_block_paragraph_mentions_resolution() -> None:
    window = _after(_pm(), "Omit the block entirely when there are none.", span=1800)
    assert "Premises closed by a binding resolution" in window
    assert "#1411" in window


# ---------------------------------------------------------------------------
# auto-dev-plan.md Step 4a Resolution gate and Step 4b/1c wiring
# ---------------------------------------------------------------------------


def test_plan_resolution_gate_sits_between_impact_gate_and_malformed_tally() -> None:
    c = _cmd("auto-dev-plan.md")
    assert (
        c.index(THREE_WAY)
        < c.index(IMPACT_GATE)
        < c.index(RESOLUTION_GATE)
        < c.index(MALFORMED_VERIFIED_TALLY)
        < c.index(MALFORMED_REC_TALLY)
    )


def test_plan_resolution_gate_runs_after_impact_gate_and_skips_routed_items() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=700)
    assert "one the Impact gate did not already route" in window
    assert "Before applying the `Verified:`-based split" in window


def test_plan_resolution_gate_absorbs_regardless_of_verified_value() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=2600)
    assert (
        "regardless of its `Verified:` value, including one that is missing, "
        "malformed, `NO`, or `DEFER`"
    ) in window


def test_plan_resolution_gate_requires_identifier_and_verbatim_quote() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=3200)
    assert "verbatim substring" in window
    assert "collapsing whitespace runs" in window
    assert "`R<n>`" in window
    assert "`row:<round>`" in window
    assert "only `row:<round>` identifiers resolve" in window
    assert "never from `.cw/context.json`" in window


def test_plan_resolution_gate_failed_check_falls_through_with_friction_line() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=3400)
    assert "falls through to the three-way split above unchanged" in window
    assert "unmatched premise resolution: <claim> — <identifier>" in window
    assert "fail-closed" in window
    assert (
        "where no identifier parsed, `<identifier>` is the raw `Resolution:` text, "
        "trimmed"
    ) in window


def test_plan_resolution_gate_never_lands_in_deferred() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=3400)
    assert "premise cannot be both resolution-gated and land in `deferred`" in window
    assert "even when its `Verified:` reads `DEFER`" in window


def test_plan_resolution_gate_reuses_self_verified_plumbing() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=3400)
    assert "reuses every existing self_verified mechanism unchanged" in window
    assert "key on bucket membership" in window


def test_plan_resolution_gate_leaves_malformed_verified_count_alone() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=3400)
    assert "neither adds nor removes anything from `malformed_verified_count`" in window


def test_plan_resolution_gate_names_wire_shape_resolution_not_resolves() -> None:
    window = _after(_cmd("auto-dev-plan.md"), RESOLUTION_GATE, span=3400)
    assert "`verified: true`" in window
    assert "`resolution` key" in window
    assert "`resolves`" in window
    assert "not an alias" in window
    assert "stays parked" in window


def test_plan_step4b_self_verified_bullet_cites_resolution_evidence() -> None:
    bullet = _step4b_bullet("If `self_verified` is non-empty")
    assert (
        "when routed via the Resolution gate (#1411), the resolution identifier "
        "plus its verbatim quote"
    ) in bullet
    # Impact-Reason alternative must survive the extension.
    assert "Impact-Reason:" in bullet


def test_plan_friction_highlights_format_for_binding_resolution() -> None:
    bullet = _step4b_bullet("For each `self_verified` premise")
    assert (
        "`self-verified premise (binding resolution): <claim> — <identifier>`" in bullet
    )
    assert "Resolution gate (#1411)" in bullet
    # The two existing line forms stay.
    assert "self-verified premise: <claim>" in bullet
    assert "self-verified premise (no impact): <claim>" in bullet


def test_plan_no_orphan_invariant_covers_resolution_gated_items() -> None:
    bullet = _step4b_bullet("**Completed no-orphan invariant (#1683).**")
    assert (
        "Resolution-gated `self_verified` items get identical treatment (#1411)"
        in bullet
    )


def test_plan_interactive_premises_skip_askuserquestion_for_resolved() -> None:
    line = _line_starting(
        _cmd("auto-dev-plan.md"), "   - **`PREMISES TO VERIFY — N items`** →"
    )
    assert "passes the Step 4a Resolution gate" in line
    assert "skips AskUserQuestion (#1411)" in line
    assert "whose `Resolution:` fails a check is still asked about" in line


def test_plan_scan_prompt_tells_pm_about_binding_resolutions() -> None:
    window = _step1c_prompt_must_include_window()
    assert "**Binding resolutions (#1411):**" in window
    assert "Resolution: <identifier>" in window
    assert "product-manager-reviewer.md" in window


def test_plan_resolution_gate_adds_no_new_el_tag() -> None:
    raw = _cmd("auto-dev-plan.md", raw=True)
    assert (
        raw.index(PLAN_ROUND_COUNTER)
        < raw.index(RESOLUTION_GATE)
        < raw.index(PLAN_FINGERPRINT_RULE)
    )
    assert "<!-- el: plan.resolution -->" not in raw


# ---------------------------------------------------------------------------
# Headless contract and parser docstring
# ---------------------------------------------------------------------------


def test_headless_contract_producer_note_1411() -> None:
    contract = _contract()
    window = _after(contract, PRODUCER_NOTE, span=1800)
    assert "`resolution`" in window
    assert "`resolves`" in window
    assert "not an alias" in window
    assert "self-verified premise (binding resolution)" in window
    assert "no schema change" in window
    assert contract.index(PRODUCER_NOTE) < contract.index(PARSER_NOTE)


def test_headless_contract_parser_note_no_longer_defers_to_1411() -> None:
    contract = _contract()
    assert "is deferred to #1411" not in contract
    assert "landed in #1411" in _after(contract, PARSER_NOTE, span=2200)


def test_premises_resolution_docstring_no_longer_defers_to_1411() -> None:
    text = RESOLVER.read_text()
    assert "is deferred to #1411" not in text
    assert "landed in #1411" in text


def test_headless_contract_premises_row_still_lists_resolution_key() -> None:
    row = _line_starting(
        _contract(), "| `premises` | `premises_pending_verification` |"
    )
    assert "`resolution`" in row


# ---------------------------------------------------------------------------
# Wire shape against the real #1325 parser
# ---------------------------------------------------------------------------


def test_wire_shape_verified_true_with_resolution_key_is_downgraded() -> None:
    p = _premises_pending_payload()
    p["premises"] = [
        _premise_item(
            claim="Gating is per-tenant",
            verified=True,
            resolution='R2 — "adopt as stated"',
        )
    ]
    result = parse_stdout(_wrap_sentinel(p))
    assert isinstance(result, AutoDevResult)
    assert result.status == "stage_complete"
    assert result.premises == []
    assert len(result.friction_highlights) == 1


def test_wire_shape_resolves_keyed_variant_stays_parked() -> None:
    p = _premises_pending_payload()
    p["premises"] = [
        _premise_item(verified=True, resolves='R2 — "adopt as stated"'),
    ]
    result = parse_stdout(_wrap_sentinel(p))
    assert result.status == "premises_pending_verification"
    assert len(result.premises) == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"verified": True}, {"resolution": 'R2 — "x"'}],
)
def test_wire_shape_half_pairs_stay_parked(kwargs: dict[str, object]) -> None:
    p = _premises_pending_payload()
    p["premises"] = [_premise_item(claim="c", **kwargs)]
    result = parse_stdout(_wrap_sentinel(p))
    assert result.status == "premises_pending_verification"
