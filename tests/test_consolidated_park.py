"""Guard tests: consolidated park — single-exit rule for Stage 1 (#1650).

Pure-markdown assertions over the auto-dev pipeline instruction files,
following the ``read_text()`` + literal-substring/window convention of
``test_auto_dev_preflight_resolutions.py`` / ``test_plan_persistence.py``.
``_cmd`` is imported from ``tests.conftest`` (#1787); ``_after`` is imported
from ``test_auto_dev_preflight_resolutions``.

Background: Stage 1's human-gated exits fired serially — the Step 1c parks
happened before the Step 1f plan-quality stations ever ran, and the
large-scope ``plan_pending_approval`` exit posted the plan without folding
station findings in. Each gate that tripped cost one operator round (mean
park latency 9-13h). The consolidated park finishes ALL plan-phase analysis
before any human-gated exit and posts ONE ``## Pending Verification Scan``
comment, so tickets converge in two rounds instead of 3-6.
"""

from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after

PARK_ANCHOR = "**Consolidated park (single-exit rule, #1650).**"


def _step1c_headless_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("4. **Headless mode:**")
    end = content.index("### Step 1d:")
    return content[start:end]


def _step1a_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1a: Check for Existing Plan")
    end = content.index("### Step 1b:")
    return content[start:end]


def _checkpoint1_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Checkpoint 1 (Plan Approval)")
    end = content.index("### Step 1e:")
    return content[start:end]


def _park_block() -> str:
    """The consolidated-park procedure.

    #1879 relocated it to ``auto-dev-plan-appendix.md``: a park only happens
    when a gate decides to exit for a human, which a converging round never
    does, so it is rare-path. Step 1c in the core doc keeps the trigger
    condition and the appendix pointer; every assertion below follows the
    content rather than being dropped.
    """
    return _after(_appendix("plan"), PARK_ANCHOR, span=3600)


# ---------------------------------------------------------------------------
# 1. The rule exists and runs the full remaining analysis before exit.
# ---------------------------------------------------------------------------


def test_consolidated_park_runs_scope_and_stations_before_exit() -> None:
    """The park runs Step 1d classification and the Step 1f stations first."""
    block = _park_block()
    assert "Run Step 1d scope classification on the draft" in block
    assert "Run the Step 1f stations" in block
    assert "**advisory mode**" in block


def test_consolidated_park_advisory_mode_never_blocks() -> None:
    """Advisory stations append no markers, run no revision, never block."""
    block = _park_block()
    assert "no signoff marker is appended" in block
    assert "no revision cycle (Step 1f.4) runs" in block
    assert "MUST_FIX must NOT convert the park into `blocked`" in block


def test_consolidated_park_friction_block_skips_station() -> None:
    """A friction-BLOCKed station is skipped with a note, never agent_block."""
    block = _park_block()
    assert "friction-BLOCKs is skipped with a note" in block
    assert "never escalated to `agent_block` from this path" in block


# ---------------------------------------------------------------------------
# 2. One comment, fixed ordering.
# ---------------------------------------------------------------------------


def test_consolidated_park_posts_one_comment_with_ordered_sections() -> None:
    """The single comment carries parks, advisory findings, approval, draft."""
    block = _park_block()
    assert "Post ONE comment under the `## Pending Verification Scan` header" in block
    findings = block.index(
        "### Advisory plan-review findings (address in the same round)"
    )
    approval = block.index("### Approval requested")
    draft = block.index("### Draft plan (unreviewed — context only)")
    assert findings < approval < draft


def test_consolidated_park_approval_section_is_large_scope_only() -> None:
    """The approval sub-section appears only when Step 1d classified Large."""
    block = _park_block()
    window = _after(block, "### Approval requested", span=200)
    assert "clears both gates on re-entry" in window
    assert "classified the draft Large" in block


def test_consolidated_park_advisory_section_omitted_when_clean() -> None:
    """The advisory sub-section is omitted on NO_ISSUES / marker-skip."""
    block = _park_block()
    assert "omit this sub-section when both stations returned NO_ISSUES" in block


# ---------------------------------------------------------------------------
# 3. Status, sentinel, and payload contracts unchanged.
# ---------------------------------------------------------------------------


def test_consolidated_park_keeps_exit_statuses() -> None:
    """Consolidation changes the comment, never the exit status."""
    block = _park_block()
    assert "The exit **status** is unchanged by consolidation" in block


def test_consolidated_park_sentinel_line_in_friction_highlights() -> None:
    """The sentinel appends the consolidated-park summary line, no schema change."""
    block = _park_block()
    assert (
        "consolidated park: <a> ambiguities, <p> premises, "
        "<f> advisory findings, scope <tier>"
    ) in block
    assert "no schema change" in block


def test_consolidated_park_payload_arrays_unchanged() -> None:
    """ambiguities/premises payload arrays keep their parked/unverified subsets."""
    block = _park_block()
    assert "Result-payload rules are untouched" in block
    assert (
        "advisory findings travel in the comment and `friction_highlights` only"
        in block
    )


def test_consolidated_park_requires_draft_in_hand() -> None:
    """Advisory stations run only when a draft plan exists in hand."""
    block = _park_block()
    assert "only when a draft plan exists in hand" in block
    assert "an exit with no plan keeps its existing comment shape" in block


# ---------------------------------------------------------------------------
# 4. The three exit clauses route through the park.
# ---------------------------------------------------------------------------


def test_step4c_exits_route_through_consolidated_park() -> None:
    """All three Step 4c park exits reference the consolidated park."""
    section = _step1c_headless_section()
    for anchor in (
        "`parked` non-empty AND `unverified` empty → EXIT "
        "`ambiguities_pending_resolution`",
        "`unverified` non-empty AND `parked` empty → EXIT "
        "`premises_pending_verification`",
        "`unverified` non-empty AND `parked` non-empty → EXIT "
        "`premises_pending_verification`",
    ):
        window = _after(section, anchor, span=300)
        assert "consolidated park" in window, anchor


def test_checkpoint1_large_exit_routes_through_consolidated_park() -> None:
    """The headless generated+large exit routes through the consolidated park."""
    section = _checkpoint1_section()
    assert (
        "EXIT `plan_pending_approval` **through the Step 1c consolidated park**"
        in section
    )


# ---------------------------------------------------------------------------
# 5. Step 1a never mistakes the parked comment's draft for an existing plan.
# ---------------------------------------------------------------------------


def test_step1a_excludes_pipeline_authored_comments_from_plan_detection() -> None:
    """Draft text inside a Pending Verification Scan comment is not a plan."""
    section = _step1a_section()
    assert "MUST NOT be treated as an existing plan" in section
    assert "### Draft plan (unreviewed — context only)" in section
    assert "resume of pipeline drafts goes through `.cw/plan-draft.md` only" in section


# ---------------------------------------------------------------------------
# 6. Resumed Large drafts still require approval evidence (no approval slip).
# ---------------------------------------------------------------------------


def test_checkpoint1_resumed_large_draft_requires_approval_evidence() -> None:
    """A resumed Large draft auto-skips approval only with an approving reply."""
    section = _checkpoint1_section()
    assert "Large-scope carve-out on the resumed-draft path (#1650)" in section
    assert "requires approval evidence in the live-fetched comments" in section
    assert "must not slip through approval by being resumed" in section


# ---------------------------------------------------------------------------
# 7. auto-dev.md decision-table rows describe the consolidated exits.
# ---------------------------------------------------------------------------


def test_auto_dev_decision_rows_reference_consolidated_park() -> None:
    """The three S1 exit rows in auto-dev.md route through the park."""
    content = _cmd("auto-dev.md")
    for row_anchor in (
        "| S1 plan, no Linear plan, large |",
        "| S1 ambiguity scan, ambiguities found (parked) |",
        "| S1 ambiguity scan, non-empty `PREMISES TO VERIFY` (unverified) |",
    ):
        window = _after(content, row_anchor, span=400)
        assert "consolidated park (#1650)" in window, row_anchor


# ---------------------------------------------------------------------------
# 8. Approval evidence is tracker-neutral: `cw dev-queue approve` stamps
#    queue_metadata.plan_approved_at, which Checkpoint 1 must honor alongside
#    an approving tracker reply (the GitHub-only `--post-marker` comment can
#    never reach a Linear-tracked ticket).
# ---------------------------------------------------------------------------


def test_checkpoint1_accepts_row_side_plan_approval_evidence() -> None:
    """The Large-scope carve-out names the queue_metadata record as evidence."""
    section = _checkpoint1_section()
    window = _after(
        section, "requires approval evidence in the live-fetched comments", span=900
    )
    assert "`queue_metadata.plan_approved_at`" in window
    assert "`.claude/cw-context.json`" in window
    assert "`cw dev-queue approve`" in window
    assert "Either source alone is sufficient" in window
    assert "Absent both, EXIT `plan_pending_approval` again" in window


def test_consolidated_park_names_cw_approve_as_comment_equivalent() -> None:
    """The park comment's `### Approval requested` ask tells the operator
    `cw dev-queue approve` clears the gate without a tracker comment."""
    appendix = _appendix("plan")
    window = _after(appendix, "`### Approval requested`", span=500)
    assert "cw dev-queue approve <ticket> -c <client>" in window
    assert "`plan_approved_at`" in window
