"""Guard tests: resolutions posted against a large-scope approval park are
applied on resume, not silently dropped (#2376).

Background: a resumed Large-scope draft that already carried row-path
approval evidence (`plan_approved_at` + `plan_approved_fingerprint ==
draft_fp`) skipped straight past Step 1c's ambiguity scan via Checkpoint 1's
existing fast path -- but nothing on the resume path ever re-checked whether
a `<!-- auto-dev-preflight-resolutions -->` comment or body edit had been
posted *after* that approval. The resolutions were never folded into the
draft, and the resumed round re-parked unrevised. This adds a 4th leading
bookkeeping line (`plan-stage-resolutions-applied`) to `.cw/plan-draft.md`
and a new Step 1a.0b that detects the delta, revises the draft when one
exists, and only then re-evaluates the fingerprint fast path -- so a
post-approval resolutions delta always takes precedence over the fast path.

Pure-markdown assertions over the auto-dev pipeline instruction files,
following the ``read_text()`` + literal-substring/window convention of
``test_auto_dev_preflight_resolutions.py`` / ``test_plan_persistence.py`` /
``test_plan_stage_settlement.py``. ``_cmd``/``_appendix`` are imported from
``tests.conftest`` (#1787/#1879); ``_after``/``_nearby`` from
``test_auto_dev_preflight_resolutions``; ``_fingerprint_rule_section`` from
``test_plan_approval_fingerprint_binding``; ``_step1a_section``/
``_step1c_headless_section`` from ``test_plan_persistence`` -- no third
private copy of any of these.
"""

from tests.conftest import _REPO_ROOT, _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after
from tests.test_plan_approval_fingerprint_binding import _fingerprint_rule_section
from tests.test_plan_persistence import _step1a_section, _step1c_headless_section

STEP1A0B_CORE_ANCHOR = (
    "0b. **Binding-resolutions delta + approved-fingerprint fast path "
    "(resumed rounds only):**"
)
STEP1A0B_APPENDIX_HEADING = (
    "## Step 1a.0b: binding-resolutions delta + approved-fingerprint "
    "fast path (resumed rounds only)"
)
STEP1C0_APPENDIX_HEADING = (
    "## Step 1c.0: round-cap read and settlement folding (resumed rounds only)"
)
STEP1C0_CORE_GATING = "Fires only when Step 1a.0's resume branch fired this dispatch"


def _headless_contract() -> str:
    return (_REPO_ROOT / "docs" / "headless-contract.md").read_text(encoding="utf-8")


def _step1a0b_appendix_section() -> str:
    appendix = _appendix("plan")
    start = appendix.index(STEP1A0B_APPENDIX_HEADING)
    end = appendix.index(STEP1C0_APPENDIX_HEADING)
    return appendix[start:end]


# ---------------------------------------------------------------------------
# Core-doc trigger stub (Step 1a item "0b.")
# ---------------------------------------------------------------------------


def test_step1a_item_0b_immediately_follows_item_0() -> None:
    """Item '0b.' sits between item '0.' and item '1.' in Step 1a."""
    section = _step1a_section()
    idx_0 = section.index("0. **Resume check")
    idx_0b = section.index(STEP1A0B_CORE_ANCHOR)
    idx_1 = section.index("1. **Tracked tickets:**")
    assert idx_0 < idx_0b < idx_1


def test_step1a_item_0b_cites_appendix_section_by_exact_name() -> None:
    """The core-doc stub names the appendix file and the exact section title."""
    section = _step1a_section()
    window = _after(section, STEP1A0B_CORE_ANCHOR, span=700)
    assert ".claude/commands/auto-dev-plan-appendix.md" in window
    assert f'"{STEP1A0B_APPENDIX_HEADING[3:]}"' in window


def test_step1a_item_0b_gating_matches_step1c0_gating_phrase() -> None:
    """Step 1a.0b's gate is the identical phrase Step 1c.0 uses (consistency)."""
    section = _step1a_section()
    window = _after(section, STEP1A0B_CORE_ANCHOR, span=300)
    assert STEP1C0_CORE_GATING in window


def test_step1a_first_unconditionally_sentence_now_names_step1a0b() -> None:
    """Item 0's *first* 'unconditionally' sentence now mentions Step 1a.0b.

    The Checkpoint-origin note's own 'unconditionally' occurrence (second in
    the item) is a separately pinned, byte-identical window -- see the
    regression pin test below -- and is untouched.
    """
    section = _step1a_section()
    first_unconditionally = section.index("unconditionally")
    window = section[max(0, first_unconditionally - 250) : first_unconditionally + 50]
    assert "Step 1a.0b" in window


def test_step1a_supersession_guard_checkpoint_origin_window_unchanged() -> None:
    """Regression pin (not new logic): the Checkpoint-origin-note window Step
    1a.0's second 'unconditionally' sits in is left byte-identical -- see
    ``test_plan_persistence.py::test_step1a_supersession_guard_covers_checkpoint_origin``,
    run unmodified as part of the regression suite.
    """
    section = _step1a_section()
    window = _after(
        section,
        "an approved `.cw/plan.md` always wins over a stale draft.",
        span=700,
    )
    assert "presence-based" in window
    assert "no separate checkpoint-specific guard is needed" in window


# ---------------------------------------------------------------------------
# Appendix section: existence + ordering
# ---------------------------------------------------------------------------


def test_step1a0b_appendix_section_exists_before_step1c0() -> None:
    """The new appendix section is inserted strictly before Step 1c.0's."""
    appendix = _appendix("plan")
    assert appendix.index(STEP1A0B_APPENDIX_HEADING) < appendix.index(
        STEP1C0_APPENDIX_HEADING
    )


def test_step1a0b_resolutions_detection_cites_step1b_setup_not_restated() -> None:
    """Sub-step 1 cites Step 1b setup's marker-discovery by name and does not
    restate the newest-wins / body-precedence rules verbatim."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Resolutions-delta detection.**", span=600)
    assert "Step 1b setup" in window
    assert "newest-wins" in window
    assert "body-over-comment precedence" in window
    # Verbatim restatement absence: Step 1b setup's own long-form sentences
    # must not be duplicated here -- only cited by name.
    assert (
        "Treat the marker-bearing comment with the latest created timestamp "
        "as the sole authoritative comment-channel source"
    ) not in section
    assert (
        "The body is the preferred channel: if the body's resolutions "
        "section carries the marker, the body's copy is authoritative"
    ) not in section


# ---------------------------------------------------------------------------
# Delta-comparison rule (sub-step 2)
# ---------------------------------------------------------------------------


def test_delta_rule_marker_absent_concrete_source_is_delta() -> None:
    """Marker absent + a concrete current source is a delta."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Delta comparison.**", span=700)
    assert (
        "marker absent + current source concrete → **delta** "
        "(this resolutions source has never been folded in)" in window
    )


def test_delta_rule_marker_absent_no_source_is_bootstrap_none() -> None:
    """Marker absent + no source is a bootstrap no-delta, persisted as none."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Delta comparison.**", span=700)
    assert (
        "marker absent + current source `none` → no delta; persist the "
        "marker as `source=none` (bootstrap)" in window
    )


def test_delta_rule_marker_present_differs_is_delta_both_subcases() -> None:
    """Marker present with a different value is a delta -- covers both a
    newer comment and a body edit."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Delta comparison.**", span=700)
    assert (
        "marker present with value X, current source Y, X ≠ Y → **delta** "
        "(a newer resolutions source, or a body edit)" in window
    )


# ---------------------------------------------------------------------------
# Revision branch (sub-step 3)
# ---------------------------------------------------------------------------


def test_revision_branch_cites_step1f4_and_format_only_precedent_by_name() -> None:
    """The revision branch cites Step 1f.4's re-spawn contract and the
    Format-only-revision independent-axis precedent, both by name."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**On delta: revise.**", span=600)
    assert "Step 1f.4" in window
    assert "Format-only revision (defense-in-depth) precedent" in window
    assert "independent of, and does not consume, the standard 1-cycle" in window


def test_revision_branch_invalidates_both_signoff_markers() -> None:
    """A resolutions redirect invalidates BOTH plan-spec and plan-soundness
    signoff markers -- either station's prior verdict can be implicated."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**On delta: revise.**", span=900)
    assert "`plan-spec-reviewed`" in window
    assert "`plan-soundness-reviewed`" in window
    assert "Invalidates BOTH" in window


def test_revision_branch_states_one_attempt_per_detected_delta_cap() -> None:
    """The revision cycle is capped at 1 attempt per detected delta."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**On delta: revise.**", span=900)
    assert "Capped at **1 attempt per detected delta**" in window


def test_revision_branch_telemetry_states_no_new_events_and_cites_step1f3() -> None:
    """A bare successful revision emits no new stage.entered/stage.errored;
    the eventual-MUST_FIX case is covered by Step 1f.3's existing exhaustion
    stage.errored, cited by name -- no silent telemetry gap (R3)."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Telemetry:**", span=500)
    assert "none beyond the best-effort checkpoint write" in window
    assert (
        "a bare successful revision emits no new `stage.entered`/`stage.errored`"
        in window
    )
    assert "Step 1f.3's existing `stage.errored` emission (unchanged) covers it" in (
        window
    )


# ---------------------------------------------------------------------------
# Fingerprint fast-path check (sub-step 4)
# ---------------------------------------------------------------------------


def test_fast_path_evidence_check_cites_checkpoint1_row_path_not_rederived() -> None:
    """The fast-path evidence check cites Checkpoint 1's row-path clause by
    name instead of re-deriving new evidence prose."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Fingerprint fast-path check", span=600)
    assert "Checkpoint 1's existing row-path evidence check by name" in window
    assert "`queue_metadata.plan_approved_at`" in window
    assert "`queue_metadata.plan_approved_fingerprint`" in window
    assert "do not re-derive it here" in window


def test_fast_path_skip_names_both_ambiguity_scan_and_step1c0_machinery() -> None:
    """The fast-path skip names BOTH Step 1c's re-scan AND Step 1c.0's
    round-cap/settlement-folding machinery -- not just 'the scan'."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Match → fast path.**", span=400)
    assert "Skip Step 1c's ambiguity/premise re-scan" in window
    assert "Step 1c.0's round-cap/settlement-folding machinery" in window
    assert "entirely" in window


def test_fast_path_telemetry_emits_skipped_stage_in_place_of_complete() -> None:
    """The fast path's telemetry fires `s1_ambiguity_scan_skipped` with
    `reason: approved_fingerprint_match`, explicitly in place of (not in
    addition to) `s1_ambiguity_scan_complete`."""
    section = _step1a0b_appendix_section()
    window = _after(section, "**Match → fast path.**", span=900)
    assert '\\"stage\\":\\"s1_ambiguity_scan_skipped\\"' in window
    assert '\\"reason\\":\\"approved_fingerprint_match\\"' in window
    assert "in place of, not in addition to, `s1_ambiguity_scan_complete`" in window


def test_delta_revision_sub_step_ordered_before_fast_path_sub_step() -> None:
    """Sub-step 3 (delta/revision) is textually positioned before sub-step 4
    (fast-path evidence check) -- a post-approval delta takes precedence
    with no extra special-casing needed."""
    section = _step1a0b_appendix_section()
    assert section.index("**On delta: revise.**") < section.index(
        "**Fingerprint fast-path check"
    )


# ---------------------------------------------------------------------------
# docs/headless-contract.md closed-enum registration
# ---------------------------------------------------------------------------


def test_headless_contract_stage_enum_includes_ambiguity_scan_skipped() -> None:
    """§10.2's closed stage-identifier enum lists the new marker."""
    content = _headless_contract()
    window = _after(content, "### 10.2 Stage Identifiers (closed enum)", span=500)
    assert "`s1_ambiguity_scan_skipped`" in window


def test_headless_contract_documents_reason_field_scoped_to_skipped() -> None:
    """§10.3 documents the `reason` field, scoped to the new stage only,
    mirroring the existing `malformed_recommendation_count` bullet format."""
    content = _headless_contract()
    window = _after(content, "### 10.3 Payload Schema", span=2000)
    assert "`reason` (str) — `s1_ambiguity_scan_skipped` only" in window
    assert "`approved_fingerprint_match`" in window


# ---------------------------------------------------------------------------
# Bookkeeping-line order + fingerprint-rule strip list (core doc)
# ---------------------------------------------------------------------------


def test_bookkeeping_line_order_documents_resolutions_applied_as_last() -> None:
    """The Bookkeeping-line order note documents the new 4th line as always
    last, alongside the three pre-existing pinned phrases (regression + new,
    combined in the same window)."""
    section = _cmd("auto-dev-plan.md")
    window = _after(
        section,
        "**Settlement marker grammar (`plan-stage-settled`, #1683).**",
        span=1900,
    )
    assert "round-counter line is always first" in window
    assert "is always second" in window
    assert "every `plan-stage-settled` marker line is appended after both" in window
    assert "never onto the fingerprint's own line" in window
    assert "`plan-stage-resolutions-applied` line, when persisted, is always last" in (
        window
    )


def test_fingerprint_computation_strips_resolutions_applied_line_too() -> None:
    """The Computation paragraph's strip-list now includes the 4th marker
    alongside the three pre-existing ones (regression + new)."""
    section = _fingerprint_rule_section()
    window = _after(section, "**Computation.**", span=700)
    assert "`<!-- plan-stage-scan-round: N -->` round-counter line" in window
    assert "`<!-- plan-stage-last-evaluated: ... -->` fingerprint line" in window
    assert "`<!-- plan-stage-settled: ... -->` marker line" in window
    assert "`<!-- plan-stage-resolutions-applied: ... -->` line" in window


def test_where_it_is_computed_is_count_agnostic() -> None:
    """R1: the ordinal 'fourth bookkeeping line'/'three-line grammar' phrasing
    is gone, replaced with count-agnostic language."""
    section = _fingerprint_rule_section()
    window = _after(section, "**Where it is computed.**", span=300)
    assert "fourth bookkeeping line" not in window
    assert "three-line grammar" not in window
    assert "additional bookkeeping line" in window
    assert "bookkeeping-line grammar" in window


# ---------------------------------------------------------------------------
# Draft-persistence / draft-rewrite rule updates (core doc)
# ---------------------------------------------------------------------------


def test_draft_persistence_rule_names_all_three_step1f3_exit_clauses() -> None:
    """The miscounted 'two Step 1f.3 writes' is now three, named explicitly:
    plan_unreviewable, plus TWO distinguishable plan_unsound mentions (1st
    cycle, persists-after-cycle) -- not a bare 'two' count anywhere nearby."""
    section = _step1c_headless_section()
    window = _after(section, "**Draft-persistence rule", span=700)
    assert "three Step 1f.3 headless `blocked` exit writes" in window
    assert "`plan_unreviewable`" in window
    assert "`plan_unsound` on its 1st MUST_FIX cycle" in window
    assert "`plan_unsound` when MUST_FIX persists after that cycle" in window
    assert "the two Step 1f.3" not in window


def test_draft_persistence_rule_cites_draft_rewrite_rule_not_templates() -> None:
    """The bookkeeping-line restatement is replaced with a citation to the
    Draft-rewrite rule -- the literal marker templates are gone from this
    specific window, proving removal rather than mere supplementation."""
    section = _step1c_headless_section()
    window = _after(section, "**Draft-persistence rule", span=1300)
    assert "carried forward per the Draft-rewrite rule below" in window
    assert "never dropped on a rewrite" in window
    assert "<!-- plan-stage-scan-round: N -->" not in window
    assert "<!-- plan-stage-last-evaluated: ... -->" not in window
    assert "<!-- plan-stage-settled: ... -->" not in window


def test_draft_rewrite_rule_names_step1a0b_as_third_rewrite_site() -> None:
    """The Draft-rewrite rule names Step 1a.0b's checkpoint as a third named
    rewrite site, alongside the two pre-existing ones."""
    section = _step1c_headless_section()
    window = _after(section, "**Draft-rewrite rule", span=900)
    assert "Step 1b checkpoint above" in window
    assert "Step 1f.4 post-revision checkpoint below" in window
    assert "Step 1a.0b's resolutions-revision checkpoint" in window


def test_draft_rewrite_rule_template_list_includes_resolutions_applied() -> None:
    """The Draft-rewrite rule's bookkeeping-line template list includes the
    4th `plan-stage-resolutions-applied` template alongside the 3
    pre-existing ones."""
    section = _step1c_headless_section()
    window = _after(section, "**Draft-rewrite rule", span=900)
    assert "<!-- plan-stage-scan-round: N -->" in window
    assert "<!-- plan-stage-last-evaluated: ... -->" in window
    assert "<!-- plan-stage-settled: ... -->" in window
    assert "<!-- plan-stage-resolutions-applied: ... -->" in window


def test_step1f4_checkpoint_note_drops_restated_bookkeeping_list() -> None:
    """Step 1f.4's post-revision checkpoint note no longer restates 'round
    counter, fingerprint, settlement markers' while still citing the
    draft-rewrite rule by name (lowercase, matching the existing convention)."""
    section = _cmd("auto-dev-plan.md")
    window = _after(
        section,
        "**Headless only — checkpoint the revised draft (#1778).**",
        span=900,
    )
    assert "round counter, fingerprint, settlement markers" not in window
    assert "draft-rewrite rule" in window


# ---------------------------------------------------------------------------
# resolution_consumed/resolution_evidence scoping exclusion (core doc)
# ---------------------------------------------------------------------------


def test_resolution_scoping_rule_excludes_step1a0b_revision_too() -> None:
    """The #1896/#2098 scoping rule excludes Step 1a.0b's resolutions-delta
    revision as a non-settlement, using the same reasoning pattern as the
    existing Step 1b exclusion -- both present in the same window."""
    section = _cmd("auto-dev-plan.md")
    window = _after(
        section,
        "**`resolution_consumed`/`resolution_evidence` emission rule (#1896).**",
        span=2200,
    )
    # Existing Step 1b exclusion (regression).
    assert "NOT a settlement and never emits these keys" in window
    # New Step 1a.0b exclusion, same reasoning pattern.
    assert "Step 1a.0b's resolutions-delta revision" in window
    assert "is excluded for the identical reason" in window
    assert "never emits `resolution_consumed`/`resolution_evidence` either" in (window)
