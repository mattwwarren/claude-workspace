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
MARKER = "<!-- auto-dev-preflight-resolutions -->"
BLOCKER_HEADER = "## Multi-Marker Gate Blocked"
PENDING_HEADER = "## Pending Verification Scan"
PREMISES_NONEMPTY_PARKED_EXIT = (
    "`unverified` non-empty AND `parked` non-empty → EXIT "
    "`premises_pending_verification`"
)


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _agent(name: str) -> str:
    return (AGENTS / name).read_text()


def _skill(name: str) -> str:
    return (SKILLS / name).read_text()


def _step1b_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1b: Generate Plan")
    end = content.index("### Step 1c:")
    return content[start:end]


def _nearby(content: str, anchor: str, span: int = 400) -> str:
    idx = content.index(anchor)
    return content[max(0, idx - span) : idx + len(anchor)]


def _after(content: str, anchor: str, span: int = 400) -> str:
    idx = content.index(anchor)
    return content[idx : idx + span]


def test_plan_multi_marker_newest_wins_no_exit() -> None:
    """>1 marker comment resolves newest-wins instead of hard-EXITing (#1654)."""
    section = _step1b_section()
    window = _after(section, "**>1 marker comment** → ", span=700)
    assert "newest-wins, no exit" in window
    assert "latest created timestamp" in window
    assert "ignore every older marker-bearing comment" in window


def test_plan_multi_marker_appends_friction_highlight() -> None:
    """The newest-wins branch logs the multi-marker friction_highlights entry."""
    section = _step1b_section()
    window = _after(section, "**>1 marker comment** → ", span=900)
    assert (
        "multi-marker: <N> resolution comments found — newest (by timestamp) "
        "used; consolidate via /harden-ticket when convenient"
    ) in window
    assert "friction_highlights" in window


def test_plan_multi_marker_proceeds_under_exactly_one_branch() -> None:
    """Newest-wins proceeds under the existing exactly-1-marker branch."""
    section = _step1b_section()
    window = _after(section, "**>1 marker comment** → ", span=1200)
    assert '"exactly 1 marker comment" branch' in window


def test_plan_multi_marker_keeps_consolidation_with_harden_ticket() -> None:
    """Timestamp selection only — content consolidation stays /harden-ticket's job."""
    section = _step1b_section()
    window = _after(section, "**>1 marker comment** → ", span=1800)
    assert "do NOT build content-merge/supersession logic here" in window
    assert "`/harden-ticket`'s job" in window


def test_plan_refuse_message_survives_only_as_historical_note() -> None:
    """The old refuse message remains documented only as the hard-EXIT era note."""
    section = _step1b_section()
    window = _nearby(section, REFUSE, span=400)
    assert "former hard EXIT" in window


def test_plan_setup_greps_preflight_marker() -> None:
    """Step 1b setup greps the pre-flight resolutions marker."""
    assert MARKER in _cmd("auto-dev-plan.md")


def test_marker_consistent_between_producer_and_consumer() -> None:
    """harden-ticket posts the same marker literal auto-dev-plan greps for.

    A typo in either file breaks pre-flight-resolution detection silently —
    exactly the drift class this ticket exists to close.
    """
    assert MARKER in _skill("harden-ticket/SKILL.md")
    assert MARKER in _cmd("auto-dev-plan.md")


def test_multi_marker_blocker_has_distinct_header() -> None:
    """The historical blocker header stays documented (and tally-excluded)."""
    assert BLOCKER_HEADER in _step1b_section()


def test_multi_marker_blocker_forbids_literal_marker() -> None:
    """Hard-EXIT-era blocker comments never carried the literal marker (#967).

    Retained as a historical note after #1654's newest-wins retirement — it is
    what makes excluding `## Multi-Marker Gate Blocked` comments from the
    marker tally safe.
    """
    section = _step1b_section()
    assert "MUST NOT contain the literal pre-flight resolutions marker" in section


def test_gate_tally_excludes_self_authored_blocker() -> None:
    """Defense in depth: the marker tally excludes the pipeline's own blocker header."""
    section = _step1b_section()
    assert "exclude any comment bearing the pipeline's own blocker header" in section
    assert BLOCKER_HEADER in section


def test_harden_directs_superseding_comment() -> None:
    """Re-harden posts a fresh superseding comment, not an append."""
    assert "## Pre-flight Resolutions (operator) — supersedes all prior" in _skill(
        "harden-ticket/SKILL.md"
    )


def test_harden_marker_strip_is_hygiene_not_load_bearing() -> None:
    """harden-ticket documents marker-stripping as hygiene after #1654."""
    content = _skill("harden-ticket/SKILL.md")
    assert "newest by\ncreated timestamp" in content or (
        "newest by created timestamp" in " ".join(content.split())
    )
    assert "no longer load-bearing" in " ".join(content.split())


def test_harden_drops_accretion_guidance() -> None:
    """The 'append to the resolution comment' guidance is gone (newline-normalized)."""
    normalized = " ".join(_skill("harden-ticket/SKILL.md").split())
    assert "append to the resolution comment" not in normalized


def test_step1b_receives_all_comments() -> None:
    """Step 1b's context bullet passes ALL ticket comments, mirroring Step 1c."""
    assert "ALL ticket comments in chronological order" in _step1b_section()


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
    section = _step1b_section()
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
    anchor = "omit `## Pre-flight Resolution Conformance` entirely"
    preceding = _nearby(_cmd("auto-dev-plan.md"), anchor)
    assert "## Binding Pre-flight Resolutions" in preceding


# ---------------------------------------------------------------------------
# Zero-comment context.json staleness (#952)
# ---------------------------------------------------------------------------


def test_intake_step3_fetch_includes_comments() -> None:
    """Step 3 single-ticket github-issues fetch requests comments."""
    assert "gh issue view <n> --json title,body,state,url,comments" in _cmd(
        "auto-dev-intake.md"
    )


def test_intake_table_row_includes_comments() -> None:
    """The op-mapping 'Fetch ticket body' row (github-issues) also fetches comments."""
    content = _cmd("auto-dev-intake.md")
    row = next(ln for ln in content.splitlines() if "Fetch ticket body" in ln)
    assert "title,body,state,url,comments" in row


def test_plan_live_fetch_every_invocation() -> None:
    """Plan Stage 1 mandates a comments+body live-fetch on every invocation."""
    assert (
        "live-fetch the ticket comments AND the ticket body on every invocation"
        in _cmd("auto-dev-plan.md")
    )


def test_plan_marker_source_pinned_to_live_fetch() -> None:
    """Step 1b marker grep is pinned to the live fetch, excluding the cache."""
    assert "NEVER the `.cw/context.json` `comments` array" in _cmd("auto-dev-plan.md")


def test_plan_cached_comments_is_snapshot_only() -> None:
    """The cached comments array is documented as a Stage-0 provenance snapshot."""
    assert "Stage-0 provenance snapshot only" in _cmd("auto-dev-plan.md")


def test_intake_warn_names_needs_attention() -> None:
    """The intake WARN names the session.needs_attention event and its reason."""
    window = _nearby(_cmd("auto-dev-intake.md"), "comments_fetch_failed")
    assert "session.needs_attention" in window


def test_intake_warn_documents_empty_undetectable() -> None:
    """Intake documents that a fetch-succeeds-but-empty case is not detectable."""
    assert (
        "NOT detectable from within this stage without a second independent source"
        in _cmd("auto-dev-intake.md")
    )


def test_intake_linear_list_comments_before_0d() -> None:
    """Linear mode mandates list_comments before Step 0d, not model initiative."""
    content = _cmd("auto-dev-intake.md")
    assert "list_comments(<id>)" in content
    assert "mandatory op that MUST run before Step 0d" in content


# ---------------------------------------------------------------------------
# Body-fold resolutions invisible to the response check (#980)
# ---------------------------------------------------------------------------


def test_plan_body_field_precedes_snapshot_anchor() -> None:
    """The cached-body callout sits right before the Stage-0 snapshot anchor."""
    assert "and the cached `body` field" in _nearby(
        _cmd("auto-dev-plan.md"), "Stage-0 provenance snapshot only"
    )


def test_plan_live_fetch_rule_covers_body() -> None:
    """The Orientation live-fetch rule is retitled to cover comments AND body."""
    assert "Comments and body are live, not cached" in _cmd("auto-dev-plan.md")


def test_plan_body_fetch_op_named() -> None:
    """The github-issues fetch op is named with the body field included."""
    assert "`gh issue view <n> --json body,comments`" in _cmd("auto-dev-plan.md")


def test_step1b_greps_body_resolutions_section() -> None:
    """Step 1b setup also greps the live-fetched issue BODY's resolutions section."""
    assert (
        "grep the live-fetched issue BODY's resolutions section for the same marker"
        in _step1b_section()
    )


def test_step1b_body_markers_excluded_from_tally() -> None:
    """Body markers are excluded from the >1-marker comment tally."""
    assert "body markers are EXCLUDED from that tally" in _step1b_section()


def test_step1b_dual_channel_echo_does_not_trip_gate() -> None:
    """The sanctioned dual-channel (comment + body) echo must not trip the gate."""
    assert "must NOT trip the gate" in _step1b_section()


def test_step1b_marker_source_excludes_cached_body_too() -> None:
    """The marker source pin excludes the cached body field too, not just comments."""
    assert (
        "NEVER the `.cw/context.json` `comments` array or cached `body` field"
        in _cmd("auto-dev-plan.md")
    )


def test_step1b_body_copy_is_authoritative() -> None:
    """When the body carries the marker, the body's copy is authoritative."""
    assert "the body's copy is authoritative" in _step1b_section()


def test_step1b_body_list_not_double_injected() -> None:
    """The body's list is used without separately injecting the comment's copy."""
    assert (
        "use the body's list and do not separately inject the comment's copy"
        in _step1b_section()
    )


def test_step1b_updated_at_named_as_coarse_trigger() -> None:
    """updatedAt is documented as at most a coarse re-read trigger."""
    section = _step1b_section()
    assert "as at most a coarse re-read trigger" in section
    assert "the failure asymmetry favors over-reading" in section


def test_step1c_ambiguities_exit_uses_pending_header() -> None:
    """The parked-non-empty headless exit posts under the pinned pending-verify header.

    Anchor updated for #1032: the trigger now keys on `parked` non-empty rather
    than raw `AMBIGUITIES` presence (see Step 4c partition rewrite). Anchor
    updated again for #1192: the premises half of the condition is now
    `unverified` empty rather than raw premises-block presence.
    """
    content = _cmd("auto-dev-plan.md")
    window = _after(
        content,
        "`parked` non-empty AND `unverified` empty → EXIT "
        "`ambiguities_pending_resolution`",
        span=700,
    )
    assert PENDING_HEADER in window


def test_step1c_premises_exit_uses_pending_header() -> None:
    """The premises-present headless exit posts under the pinned header too.

    Anchor updated for #1032: the trigger now reads on `parked` state rather
    than raw `PREMISES TO VERIFY` presence alone (see Step 4c partition rewrite).
    """
    content = _cmd("auto-dev-plan.md")
    window = _after(content, PREMISES_NONEMPTY_PARKED_EXIT)
    assert PENDING_HEADER in window


def test_step1c_pending_header_mirrors_gate_blocked_idiom() -> None:
    """The pinned header is documented as mirroring the Multi-Marker Gate idiom."""
    assert "mirroring the `## Multi-Marker Gate Blocked`" in _cmd("auto-dev-plan.md")


def test_harden_documents_marker_moves_with_body_fold() -> None:
    """harden-ticket documents the marker moves with resolutions folded into body."""
    content = _skill("harden-ticket/SKILL.md")
    assert "pre-flight resolutions HTML-comment marker" in content
    assert "moves with them" in content


# ---------------------------------------------------------------------------
# Adopt-assumption fast path for the ambiguity gate (#1032)
# ---------------------------------------------------------------------------


def _step1c_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1c: Ambiguity Verification")
    end = content.index("### Step 1d:")
    return content[start:end]


def test_mode1_output_has_recommendation_field() -> None:
    """Mode 1 format requires a Recommendation sub-bullet with ADOPT/PARK tokens."""
    content = _agent("product-manager-reviewer.md")
    assert "Recommendation: ADOPT" in content
    assert "PARK" in content


def test_mode1_recommendation_park_reasons_listed() -> None:
    """The PARK bar names product/scope, public-contract, destructive-action reasons."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "Recommendation: ADOPT", span=400)
    assert "public-contract shape" in window
    assert "destructive-action semantics" in window
    assert "cannot confidently recommend a side" in window


def test_mode1_recommendation_missing_field_documented_as_parked() -> None:
    """A missing/malformed Recommendation line documents defaulting to PARK."""
    content = _agent("product-manager-reviewer.md")
    assert "a missing or malformed `Recommendation` line" in content
    assert "is treated as PARK downstream" in content


def test_ambiguity_pre_flight_recommendation_is_mandatory_and_typed() -> None:
    """Step 1b's Ambiguity pre-flight mirrors the PM Reviewer's mandatory framing."""
    section = _step1b_section()
    assert "Recommendation is mandatory on every item — never omit it." in section
    assert (
        "Recommendation: ADOPT — <why safe to auto-adopt> | "
        "PARK — <why a human must decide>"
    ) in section
    assert "is treated as PARK downstream" in section


def test_step1c_has_adopted_assumptions_section() -> None:
    """Step 1c's partition introduces a plan-body Adopted Assumptions section."""
    assert "## Adopted Assumptions" in _step1c_section()


def test_step1c_partition_splits_by_recommendation() -> None:
    """Step 1c splits ambiguity items into adopted/parked by their Recommendation."""
    section = _step1c_section()
    assert "split its items by each item's `Recommendation:` sub-bullet" in section
    assert "`adopted`" in section
    assert "`parked`" in section


def test_step1c_partition_missing_recommendation_defaults_to_parked() -> None:
    """A missing/malformed Recommendation defaults an item to parked, no exceptions."""
    section = _step1c_section()
    assert (
        "a missing `Recommendation:` sub-bullet, or any unparseable/malformed "
        "value all default-safe to parked"
    ) in section


def test_step1c_all_adopt_does_not_exit() -> None:
    """An all-adopt scan (parked empty, unverified empty) auto-continues, not exits."""
    section = _step1c_section()
    assert (
        "`parked` empty AND `unverified` empty → AUTO-CONTINUE to Step 1d." in section
    )
    assert (
        "functionally `NO_AMBIGUITIES`/`no premises pending` even though the "
        "raw scans returned items" in section
    )


def test_step1c_sentinel_carries_only_parked() -> None:
    """The comment/sentinel ambiguities field carries only parked, never raw."""
    section = _step1c_section()
    assert (
        "include only the `parked` items in the result payload under "
        "`ambiguities` — never the raw N-item list"
    ) in section


def test_step1c_original_ambiguities_section_collapsed_after_partition() -> None:
    """A pre-existing plan-body Ambiguities section is rewritten, not left raw."""
    section = _step1c_section()
    assert (
        "rewrite that section's contents in place: literal `NO_AMBIGUITIES` "
        "when `parked` is empty"
    ) in section
    assert (
        "must never be left displaying the full unpartitioned list alongside "
        "`## Adopted Assumptions`"
    ) in section


def test_step1c_combined_premises_exit_keys_on_parked_not_raw_ambiguities() -> None:
    """The combined-exit trigger keys on parked non-empty, not raw AMBIGUITIES."""
    section = _step1c_section()
    assert PREMISES_NONEMPTY_PARKED_EXIT in section
    assert "If both `AMBIGUITIES` and `PREMISES TO VERIFY` are present" not in section


def test_step1c_all_adopt_plus_premises_exits_premises_only() -> None:
    """All-adopt plus premises exits premises-only, no parked/ambiguities pair."""
    section = _step1c_section()
    assert (
        "`unverified` non-empty AND `parked` empty → EXIT "
        "`premises_pending_verification` **through the consolidated park "
        "above**. Persist the draft per the draft-persistence rule above "
        "before posting. Post ONLY the `unverified` premises"
    ) in section


def test_step1c_partition_is_headless_only() -> None:
    """The interactive AskUserQuestion AMBIGUITIES branch has no partition language."""
    content = _cmd("auto-dev-plan.md")
    window = _after(
        content,
        "**`AMBIGUITIES — N items`** → present each ambiguity to the user via "
        "AskUserQuestion",
        span=400,
    )
    for token in ("Recommendation", "adopted", "parked"):
        assert token not in window


def test_step1c_ambiguities_exit_anchor_preserved() -> None:
    """The ambiguities_pending_resolution EXIT anchor survives the restructure."""
    assert "EXIT `ambiguities_pending_resolution`" in _step1c_section()


def test_adopted_assumptions_placed_at_plan_body_insertion_point() -> None:
    """Adopted Assumptions inserts after Conformance if present, else before Ambig."""
    content = _cmd("auto-dev-plan.md")
    assert (
        "insert a `## Adopted Assumptions` section into the plan body — "
        "immediately AFTER `## Pre-flight Resolution Conformance` if that "
        "section is present, else immediately before `## Ambiguities`"
    ) in content


def test_adopted_assumptions_has_fallback_anchor_when_neither_section_exists() -> None:
    """Standalone-PM-Reviewer-spawn path defines a fallback anchor, never drops it."""
    section = _step1c_section()
    assert "if the plan body has **neither** section" in section
    assert (
        "insert `## Adopted Assumptions` as the first section immediately "
        "after the plan's title/summary"
    ) in section
    assert "never silently drop it" in section


def test_stage_entered_adopted_count_is_literal_substitution_not_shell_var() -> None:
    """The stage.entered payload uses a literal placeholder, not $ADOPTED_COUNT."""
    section = _step1c_section()
    payload_line = next(
        line for line in section.splitlines() if "adopted_count" in line
    )
    assert "$ADOPTED_COUNT" not in payload_line
    assert r"\"adopted_count\":<N>" in payload_line
    assert "substitute the computed `len(adopted)` integer directly" in section


# ---------------------------------------------------------------------------
# Recommendation-field omission invisible to the consumer (#1274)
# ---------------------------------------------------------------------------


def test_stage_entered_payload_has_malformed_recommendation_count() -> None:
    """The stage.entered payload carries malformed_recommendation_count too."""
    section = _step1c_section()
    payload_line = next(
        line for line in section.splitlines() if "adopted_count" in line
    )
    assert r"\"malformed_recommendation_count\":<M>" in payload_line
    assert "malformed_recommendation_count" in section.replace(payload_line, "", 1)


def test_step1c_malformed_recommendation_tally_is_additive_only() -> None:
    """The new Step 4a tally paragraph is additive, not a third partition bucket."""
    section = _step1c_section()
    assert "tally `malformed_recommendation_count`" in section
    assert (
        "excluding items that parked via a well-formed, explicit "
        "`Recommendation: PARK — ...` line"
    ) in section
    assert "does not introduce a third partition bucket" in section


def test_step1c_malformed_recommendation_note_is_count_only() -> None:
    """The Pending Verification Scan comment note is count-only, no reclassification."""
    section = _step1c_section()
    assert "not because of a genuine PARK decision" in section
    assert "count-only note — no per-item classification" in section


# ---------------------------------------------------------------------------
# #1651: Verified: DEFER — runtime-only premises verify at implementation
# start with halt-and-report instead of parking the run.
# ---------------------------------------------------------------------------


def test_defer_token_gated_on_all_three_conditions() -> None:
    """The producer allows DEFER only when runtime-only + cheap check + safe."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "**Deferred verification (`Verified: DEFER`)", span=1600)
    assert "ONLY when ALL three conditions hold" in window
    assert "verifiable only at runtime" in window
    assert "START of implementation" in window
    assert "halt-and-report, never a silent fallback" in window


def test_defer_runtime_only_excludes_in_session_answers() -> None:
    """Anything answerable from docs/source/in-session command meets YES, not DEFER."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "**(a) Runtime-only:**", span=400)
    assert "meets the `YES` bar instead" in window


def test_defer_requires_check_and_mismatch_fields() -> None:
    """A DEFER item must carry In-implementation check: and On mismatch: fields."""
    content = _agent("product-manager-reviewer.md")
    assert "In-implementation check:" in content
    assert "On mismatch:" in content
    window = _after(content, "A DEFER item MUST carry", span=400)
    assert "`In-implementation check:`" in window
    assert "`On mismatch:`" in window


def test_defer_missing_field_fails_closed_to_no() -> None:
    """A DEFER missing either required field is treated as NO downstream."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "A DEFER missing either field", span=300)
    assert "treated as `NO` downstream" in window
    # The consumer-side mandatory-field paragraph mirrors the same default.
    assert (
        "or a `DEFER` missing its `In-implementation check:` or "
        "`On mismatch:` sub-bullet, is treated as NO downstream" in content
    )


def test_defer_excludes_operator_intent_questions() -> None:
    """Operator-intent questions never route through DEFER (non-goal guard)."""
    content = _agent("product-manager-reviewer.md")
    window = _after(content, "Operator-intent questions never qualify", span=300)
    assert "condition (a) excludes them by construction" in window


def test_defer_output_format_names_all_three_tokens() -> None:
    """The premises output format offers YES | NO | DEFER."""
    content = _agent("product-manager-reviewer.md")
    assert "| DEFER — <why the fact is runtime-only" in content


def test_step1c_premise_partition_is_three_way() -> None:
    """The consumer partitions premises self_verified / deferred / unverified."""
    section = _step1c_section()
    window = _after(section, "**Same partition, mirrored for premises", span=1200)
    assert "`self_verified`" in window
    assert "`deferred`" in window
    assert "`unverified`" in window
    assert "a `DEFER` missing either required sub-bullet" in window
    assert "`deferred` never parks" in window


def test_step1c_deferred_premises_plan_section_and_leading_step() -> None:
    """Deferred premises get a plan section and a leading implementation step."""
    section = _step1c_section()
    assert "## Deferred Premises" in section
    window = _after(section, "insert an explicit leading step", span=500)
    assert "before any dependent work" in window
    assert "existing `blocked` exit with the premise named" in window
    assert "never a silent fallback" in window


def test_step1c_deferred_unrunnable_check_is_mismatch() -> None:
    """A deferred check that cannot run counts as a mismatch, not a pass."""
    section = _step1c_section()
    assert "cannot even be run" in section
    assert "is a mismatch for this purpose, not a pass" in section


def test_step1c_deferred_premises_audited_in_friction_highlights() -> None:
    """Each deferred premise leaves a friction_highlights audit line."""
    section = _step1c_section()
    assert "deferred premise: <claim> — checked at implementation start" in section


def test_step1c_exit_gating_ignores_deferred() -> None:
    """Step 4c keys on parked + unverified only; deferred never gates."""
    section = _step1c_section()
    assert "`deferred` never gates" in section
    window = _after(section, "Keys on `parked` and `unverified` ONLY", span=600)
    assert "`self_verified` and/or `deferred`" in window
