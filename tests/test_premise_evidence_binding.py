"""Guard tests: Self-Verified premises bound to the check that made them (#2343).

Pure-markdown assertions over the Product Manager Reviewer spec, the plan-stage
command and its appendix, and the headless contract, following the
``read_text()`` + literal-substring/window convention of
``test_premise_impact_gate.py``.

Background: a stale premise carried forward in ``## Self-Verified Premises`` was
re-stamped "verified" without a fresh check, and a failed re-verification did
not downgrade it. The fix stamps each reviewer ``Verified: YES`` with an
``Observed-at`` / ``Kind`` / ``Tool-call`` triple, validates the shape and the
dispatch time window with ``.claude/scripts/check_premise_evidence.py``, and
reconciles stale carried-forward entries through the existing PENDING-stub
machinery. The script proves freshness shape only; it never reads a transcript.
"""

import re
from pathlib import Path

from tests._shared.workflow_text import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_plan_operator_fold import WORKING_PLAN_RULE, _paragraph_at
from tests.test_plan_stage_settlement import (
    _step1c_headless_section,
    _step1c_section,
    _step4b_bullet,
    _step4b_section,
)
from tests.test_premise_impact_gate import _contract, _pm, _premises_block

ROOT = Path(__file__).parent.parent

FRESHNESS_ANCHOR = "**Premise freshness pre-pass (#2343).**"
BINDING = (
    "**Evidence-binding check (checked third, `Verified: YES`-routed items only, "
    "#2343).**"
)
RECONCILIATION = "**Stale-premise reconciliation (#2343).**"
RESOLUTION_GATE = (
    "**Resolution gate (checked second, after the Impact gate and ahead of the "
    "three-way split, #1411).**"
)
MALFORMED_VERIFIED_TALLY = (
    "**Malformed-verified tally (additive, no new bucket, #2432).**"
)
PROMPT_MUST_INCLUDE = "**Prompt must include:**"
SETTLED_HEADING_BOLD = "**Settled items from prior rounds (do not re-raise):**"
RE_VERIFY_HEADING = "## Premises To Re-Verify (stale carried-forward, #2343)"

STUB_BULLET = "**`unverified`-bucket stub-identity match (#1683).**"
NO_ORPHAN = "**Completed no-orphan invariant (#1683).**"
SELF_VERIFIED_NEEDLE = "If `self_verified` is non-empty"
STEP4B_NEEDLES = (
    "If `deferred` is non-empty",
    STUB_BULLET,
    SELF_VERIFIED_NEEDLE,
    "For each `self_verified` premise",
    NO_ORPHAN,
)

ENTRY_TEMPLATE = (
    "- <claim> — Evidence: <citation> — Depends on: <dependency> — Route: "
    "VERIFIED|IMPACT|RESOLUTION — Observed-at: <YYYY-MM-DDTHH:MM:SSZ> — Kind: "
    "STATIC|LIVE — Tool-call: <ToolName>: <input> [— Observed-attempt: <n>]"
)
LEGACY_RULE = (
    "a legacy entry is parsed by the first form that matches, in this order: (a) the "
    "multi-line `- Claim:` block (claim = the text after `Claim:`; evidence = the "
    "`Evidence:` line; depends-on = the `Plan depends on this for:` or `Plan depends "
    "on it for:` line); (b) the single-line `<claim> — Evidence: ... — Depends "
    "on: ...` form; (c) the older one-line `<claim> — ...` prose (claim = text "
    "before the first ` — `)."
)
FRICTION_ABSENT = "check_premise_evidence: script absent, skipped"
FRICTION_STALE = "check_premise_evidence: script stale, skipped"
FRICTION_CONTEXT = "check_premise_evidence: context unavailable, scan-block skipped"
FRICTION_EXIT2 = "check_premise_evidence: exit 2 (usage or parse error), skipped"
FRICTION_UNPARSEABLE = (
    "unparseable legacy self-verified premise left untouched: <first line of the "
    "entry, trimmed>"
)
SEAM_NEEDLE = (
    "`## Self-Verified Premises` entries are carried byte-for-byte by every revision "
    "or fold"
)
SEAM_PRECEDES = (
    "The text must be non-empty; on any failure the prior draft is kept and nothing "
    "is written."
)


def _core() -> str:
    return _cmd("auto-dev-plan.md")


def _pm_bullet(prefix: str) -> str:
    matches = [line for line in _pm().splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one PM line starting {prefix!r}"
    return matches[0]


def _pre_pass() -> str:
    return _paragraph_at(_step1c_section(), FRESHNESS_ANCHOR)


def _binding() -> str:
    return _paragraph_at(_core(), BINDING)


def _reconciliation() -> str:
    return _step4b_bullet(RECONCILIATION)


# ---------------------------------------------------------------------------
# Product Manager Reviewer spec
# ---------------------------------------------------------------------------


def test_pm_fresh_evidence_rule_exists_once() -> None:
    assert _pm().count("**Fresh-evidence rule (#2343).**") == 1


def test_pm_fresh_evidence_rule_requires_stamp_and_tool_call() -> None:
    window = _after(_pm(), "**Fresh-evidence rule (#2343).**", span=1800)
    assert "date -u +%Y-%m-%dT%H:%M:%SZ" in window
    assert "`Observed-at`" in window
    assert "`Tool-call: <ToolName>: <input>`" in window
    assert "Carried-forward plan text is never evidence" in window


def test_pm_fresh_evidence_rule_defines_kind() -> None:
    window = _after(_pm(), "**Fresh-evidence rule (#2343).**", span=2400)
    assert "`Kind: STATIC`" in window
    assert "`Kind: LIVE`" in window
    assert "defaulting to LIVE on doubt" in window


def test_pm_anti_pattern_is_forbidden_verbatim() -> None:
    window = _after(_pm(), "**Fresh-evidence rule (#2343).**", span=2400)
    assert "re-ran, re-verified or independently" in window
    assert '"this round"' in window
    assert "without a command executed in this session" in window


def test_pm_failed_reverification_is_no_never_carried() -> None:
    window = _after(_pm(), "**Fresh-evidence rule (#2343).**", span=3200)
    assert "`Verified: NO`" in window
    assert "`Reason: re-verification failed: <cause>`" in window
    for cause in (
        "permission-classifier denial",
        "missing credentials",
        "non-zero exit",
        "timeout",
        "unreachable",
    ):
        assert cause in window
    assert "never `YES`, never carried forward, never `DEFER`" in window


def test_pm_output_block_gains_freshness_sub_bullets_after_citation() -> None:
    block = _premises_block()
    citation = block.index("   - Citation: <YES only")
    observed = block.index("   - Observed-at: <YES only")
    kind = block.index("   - Kind: STATIC | LIVE")
    tool_call = block.index("   - Tool-call: <YES only")
    reason = block.index("   - Reason: <NO only")
    assert citation < observed < kind < tool_call < reason
    assert "   - Verified: YES | NO | DEFER\n" in block
    assert "   - Kind: STATIC | LIVE <YES only" in block


def test_pm_reviewer_never_writes_observed_attempt_bullet() -> None:
    assert "   - Observed-attempt" not in _premises_block()
    assert "never writes `Observed-attempt`" in _pm()


def test_pm_consumer_default_extended_after_downstream() -> None:
    pm = _pm()
    assert "a `YES` missing its `Citation:` sub-bullet" in pm
    assert (
        "or a `DEFER` missing its `In-implementation check:` or `On mismatch:` "
        "sub-bullet, is treated as NO downstream"
    ) in pm
    start = pm.index("is treated as NO downstream")
    end = pm.index("a deliberate fail-closed default", start)
    clause = pm[start:end]
    assert "`Observed-at:`" in clause
    assert "`Kind:`" in clause
    assert "`Tool-call:`" in clause
    assert "treated the same way" in clause


def test_pm_suppression_bullet_exempts_re_verify_list() -> None:
    bullet = _pm_bullet(
        "- **Matches an entry in `## Self-Verified Premises` or "
        "`## Deferred Premises`**"
    )
    assert "the claim was already resolved" in bullet
    assert "`## Premises To Re-Verify`" in bullet
    assert "is NOT suppressed" in bullet
    assert "MUST be re-classified" in bullet


def test_pm_existing_resolution_record_pins_still_hold() -> None:
    pm = _pm()
    assert "### Before surfacing: check the plan's own resolution record" in pm
    assert "Note the asymmetry: a PARKED item stays open" in pm
    assert "suppress the candidate ambiguity entirely" in pm
    assert "do not downgrade it to a note" in pm


# ---------------------------------------------------------------------------
# auto-dev-plan.md: Step 1c pre-pass and prompt bullet
# ---------------------------------------------------------------------------


def test_pre_pass_invokes_draft_mode_on_the_working_plan_scratch_copy() -> None:
    para = _pre_pass()
    assert (
        "check_premise_evidence.py draft --plan $SESSION_WT/.cw/pm-working-plan.md "
        "--context .claude/cw-context.json"
    ) in para
    assert "$SESSION_WT/.cw/pm-working-plan.md" in para
    assert "working plan" in para
    assert "--plan .cw/plan-draft.md" not in para
    assert "--plan $SESSION_WT/.cw/plan-draft.md" not in para


def test_pre_pass_sits_immediately_before_prompt_must_include() -> None:
    section = _step1c_section()
    start = section.index(FRESHNESS_ANCHOR)
    anchor = section.index(PROMPT_MUST_INCLUDE)
    assert start < anchor
    assert section[start:anchor].strip() == _pre_pass().strip()
    assert section.count(FRESHNESS_ANCHOR) == 1


def test_pre_pass_is_headless_only_and_a_noop_without_context_file() -> None:
    para = _pre_pass()
    assert "headless" in para
    assert "resume" in para
    assert "no-op" in para
    assert ".claude/cw-context.json" in para
    assert "interactive" in para


def test_pre_pass_resolves_script_with_deviation_and_friction_texts() -> None:
    para = _pre_pass()
    assert "Existence is not enough" in para
    assert "`cw-script-version` 1" in para
    assert "$SESSION_WT/.claude/scripts/check_premise_evidence.py" in para
    assert "$HOME/.claude/scripts/check_premise_evidence.py" in para
    assert "not a HEADLESS BLOCK" in para
    for text in (FRICTION_ABSENT, FRICTION_STALE, FRICTION_EXIT2):
        assert text in para
    assert FRICTION_UNPARSEABLE in para


def test_pre_pass_lists_stale_entries_for_reverification() -> None:
    para = _pre_pass()
    assert RE_VERIFY_HEADING in para
    assert "removed from the plan copy handed to the reviewer" in para
    assert "`context-unavailable`" in para
    assert "also fails open" in para
    assert "preserve the authoritative entries" in para
    assert "skip stale-premise reconciliation" in para


def test_pre_pass_states_working_plan_rule_is_not_violated() -> None:
    para = _pre_pass()
    assert "does not violate the Working-plan rule" in para
    assert "Steps 1f and 1g read the full working plan" in para
    assert "headless Step 4b" in para


def test_three_friction_texts_are_distinct_and_present_in_the_core_doc() -> None:
    core = _core()
    texts = {FRICTION_ABSENT, FRICTION_STALE, FRICTION_CONTEXT}
    assert len(texts) == 3
    for text in texts:
        assert text in core


def test_prompt_bullet_names_re_verify_list_as_required_target() -> None:
    section = _step1c_section()
    window_start = section.index(PROMPT_MUST_INCLUDE)
    window = section[window_start : section.index("2. Parse the agent's output.")]
    lines = [line for line in window.splitlines() if "backstop wins" in line]
    assert len(lines) == 1
    bullet = lines[0]
    assert RE_VERIFY_HEADING in bullet
    assert "required classification target" in bullet
    assert "`## Settled Plan Items`" in bullet
    assert "never exempts the claim from verification" in bullet
    assert SETTLED_HEADING_BOLD not in bullet


def test_prompt_bullet_follows_the_settled_items_bullet() -> None:
    section = _step1c_section()
    lines = section.splitlines()
    settled = next(i for i, ln in enumerate(lines) if SETTLED_HEADING_BOLD in ln)
    mine = next(i for i, ln in enumerate(lines) if "backstop wins" in ln)
    assert mine == settled + 1


def test_do_not_trim_exception_lists_stale_entries_as_agent_copy_only() -> None:
    lines = [
        ln
        for ln in _step1c_section().splitlines()
        if "Do not trim the plan in a way that drops these" in ln
    ]
    assert len(lines) == 1
    assert "(#1593)" in lines[0]
    assert "stale" in lines[0]
    assert "removed from the agent's copy only" in lines[0]


# ---------------------------------------------------------------------------
# auto-dev-plan.md: Step 4a evidence-binding check
# ---------------------------------------------------------------------------


def test_binding_check_sits_between_resolution_gate_and_malformed_tally() -> None:
    core = _core()
    assert (
        core.index(RESOLUTION_GATE)
        < core.index(BINDING)
        < core.index(MALFORMED_VERIFIED_TALLY)
    )
    assert core.count(BINDING) == 1


def test_binding_check_invokes_scan_block_and_routes_unbound() -> None:
    para = _binding()
    assert "check_premise_evidence.py scan-block --file" in para
    assert "$SESSION_WT/.cw/pm-premises-block.txt" in para
    assert "--context .claude/cw-context.json" in para
    assert "Impact-gated" in para
    assert "Resolution-gated" in para
    assert "exempt" in para
    assert "UNBOUND" in para
    assert "moves from `self_verified` to `unverified`" in para
    assert "unbound self-verified premise: <claim> — <reasons>" in para
    assert "`malformed_verified_count`" in para
    assert "unchanged" in para


def test_binding_check_fails_open_on_context_unavailable() -> None:
    para = _binding()
    assert "context-unavailable" in para
    assert "fails open" in para
    assert FRICTION_CONTEXT in para
    assert "no item moves to `unverified` on that reason" in para
    assert "can never park the ticket" in para
    assert "`Observed-at`, `Kind` or `Tool-call`" in para


def test_binding_check_resolves_script_like_the_pre_pass() -> None:
    para = _binding()
    for text in (FRICTION_ABSENT, FRICTION_STALE):
        assert text in para


def test_round_is_defined_as_the_dispatch_not_a_counter() -> None:
    para = _binding()
    assert "`stage_started_at`" in para
    assert "is not `plan-stage-scan-round` or `<k>`" in para


def test_binding_check_names_the_real_context_file_path() -> None:
    para = _binding()
    assert "`stage_started_at` in `.claude/cw-context.json`" in para
    assert ".cw/cw-context.json" not in para


# ---------------------------------------------------------------------------
# auto-dev-plan.md: Step 4b
# ---------------------------------------------------------------------------


def test_self_verified_bullet_carries_the_entry_template_in_place() -> None:
    bullet = _step4b_bullet(SELF_VERIFIED_NEEDLE)
    assert ENTRY_TEMPLATE in bullet
    after_dependency = bullet.index("and what the plan depends on it for.")
    template = bullet.index(ENTRY_TEMPLATE)
    stub = bullet.index("**Stub-identity match (#1683).**")
    assert after_dependency < template < stub
    for pinned in (
        "remove that stub entry from `## Deferred Premises` entirely",
        "a `Verified: YES` reclassification fully confirms the claim, so there is "
        "nothing to update in place",
        "`Verified: YES` sub-bullet's `Citation:` line",
        "when a prior citation had drifted, #2432",
    ):
        assert pinned in bullet


def test_self_verified_bullet_states_field_sources() -> None:
    bullet = _step4b_bullet(SELF_VERIFIED_NEEDLE)
    assert "reviewer's sub-bullets" in bullet
    assert "`.claude/cw-context.json` `attempt`" in bullet
    assert "never by the reviewer" in bullet
    assert "fresh `Observed-at`" in bullet
    assert "omit the freshness fields" in bullet


def test_reconciliation_bullet_is_between_stub_and_no_orphan_bullets() -> None:
    lines = _step4b_section().splitlines()
    recon = next(ln for ln in lines if RECONCILIATION in ln)
    stub = next(ln for ln in lines if STUB_BULLET in ln)
    orphan = next(ln for ln in lines if NO_ORPHAN in ln)
    assert lines.index(recon) == lines.index(stub) + 1
    assert lines.index(orphan) == lines.index(recon) + 1


def test_reconciliation_bullet_matches_none_of_the_five_needles() -> None:
    recon = _reconciliation()
    for needle in STEP4B_NEEDLES:
        assert needle not in recon
        assert RECONCILIATION not in _step4b_bullet(needle)


def test_reconciliation_bullet_buckets_and_synthesized_premise() -> None:
    recon = _reconciliation()
    assert LEGACY_RULE in recon
    assert "`unparseable-legacy`" in recon
    assert "re-verification not performed" in recon
    assert "`claim`" in recon
    assert "`plan_depends_on_it_for`" in recon
    assert "`verify_by`" in recon
    assert "re-run the cited observation this round" in recon
    assert "`Depends on:`" in recon
    assert "`Plan depends on this for:` or `Plan depends on it for:`" in recon
    assert "premises_pending_verification" in recon
    assert "no second builder" in recon
    assert (
        "self-verified premise downgraded: <claim> — re-verification failed: <reason>"
        in recon
    )
    assert 'The old "verified" value is never preserved' in recon


def test_reconciliation_bullet_settled_item_precedence() -> None:
    recon = _reconciliation()
    assert (
        "whether or not the claim also matches a `## Settled Plan Items` entry"
    ) in recon
    assert "backstop wins" not in recon


def test_reconciliation_bullet_applies_to_every_re_verify_entry() -> None:
    recon = _reconciliation()
    assert RE_VERIFY_HEADING in recon
    assert "Draft-rewrite-rule write" in recon


# ---------------------------------------------------------------------------
# auto-dev-plan.md: closed grammar, anchors, Working-plan rule seam
# ---------------------------------------------------------------------------


def test_closed_list_of_plan_stage_markers_is_unchanged() -> None:
    found = set(re.findall(r"plan-stage-[a-z]+(?:-[a-z]+)*", _core()))
    assert found == {
        "plan-stage-approval-revoked",
        "plan-stage-last-evaluated",
        "plan-stage-resolutions-applied",
        "plan-stage-resolutions-attempted",
        "plan-stage-scan-round",
        "plan-stage-settled",
    }


def test_no_new_el_anchors_were_added() -> None:
    root = ROOT / ".claude" / "commands"
    assert (root / "auto-dev-plan.md").read_text().count("<!-- el:") == 44
    assert (root / "auto-dev-plan-appendix.md").read_text().count("<!-- el:") == 7


def test_new_prose_never_mentions_transcripts() -> None:
    for paragraph in (_pre_pass(), _binding(), _reconciliation()):
        assert "transcript" not in paragraph.lower()
    assert "transcript" not in _step4b_bullet(SELF_VERIFIED_NEEDLE).lower()


def test_working_plan_rule_carries_the_self_verified_seam() -> None:
    core = _core()
    assert core.count(SEAM_NEEDLE) == 1
    paragraph = _paragraph_at(_step1c_headless_section(), WORKING_PLAN_RULE)
    assert SEAM_NEEDLE in paragraph
    assert (
        paragraph.index(SEAM_PRECEDES)
        < paragraph.index(SEAM_NEEDLE)
        < paragraph.index("Signoff:")
    )
    assert "\n\n" not in paragraph
    for site in (
        "Step 1c.0a fold",
        "Step 1a.0b resolutions-delta revision",
        "Step 1f.4 revision",
    ):
        assert site in paragraph
    assert "is invalid, so the prior draft is kept" in paragraph
    assert "the draft handed to the spawn" in paragraph
    assert "Step 4b or the stale-premise reconciliation" in paragraph


def test_working_plan_rule_seam_leaves_the_fold_pins_intact() -> None:
    section = _step1c_headless_section()
    paragraph = _paragraph_at(section, WORKING_PLAN_RULE)
    assert section.count(WORKING_PLAN_RULE) == 1
    for pinned in (
        "Step 1d, Step 1f and Step 1g",
        "Transcription is not deferrable",
        "plan-spec-reviewed",
        "plan-soundness-reviewed",
        "a no-change fold is a no-op",
        "Step 1f.4 is excluded from strip-all",
        "every accumulator section the prior draft carried",
        "## Patterns Found",
        "## Touch-point Contract",
        "## Files Modified",
        "## Non-Goals",
        "## Ambiguities",
        "## Adopted Assumptions",
        "## Self-Verified Premises",
        "## Deferred Premises",
        "## Settled Plan Items",
    ):
        assert pinned in paragraph


def test_plan_agent_spawn_prompts_are_not_patched_individually() -> None:
    assert "byte-for-byte" not in _appendix("plan")
    assert _core().count("byte-for-byte") == 1


# ---------------------------------------------------------------------------
# Appendix, auto-dev-impl.md and the headless contract
# ---------------------------------------------------------------------------


def _fast_path_exemption() -> str:
    appendix = _appendix("plan")
    start = appendix.index("**No match (or absent evidence)")
    start = appendix.index("\n", start)
    end = appendix.index("**Scope note on evidence source.**", start)
    return appendix[start:end].strip()


def test_appendix_fast_path_exemption_sentence() -> None:
    sentence = _fast_path_exemption()
    assert sentence
    assert "Self-Verified" in sentence
    assert "fast path" in sentence
    assert "`draft_fp`" in sentence


def test_appendix_fast_path_sentence_avoids_the_negative_pins() -> None:
    sentence = _fast_path_exemption().lower()
    assert sentence
    for banned in ("sha-256", "sha256", "sha256sum", "body_sha"):
        assert banned not in sentence
    assert "resolution_consumed" not in sentence
    assert "resolution_evidence" not in sentence


def test_impl_command_is_untouched_by_the_new_script() -> None:
    impl = _cmd("auto-dev-impl.md", raw=True)
    assert "check_premise_evidence" not in impl


def test_headless_contract_carries_the_2343_producer_note() -> None:
    contract = _contract()
    start = contract.index("**Producer note (#2343")
    assert contract.index("**Producer note (#1411") < start
    note = contract[start : contract.index("\n\n", start)]
    assert "no schema change" in note
    for token in ("`Observed-at`", "`Kind`", "`Tool-call`", "`premises`"):
        assert token in note
