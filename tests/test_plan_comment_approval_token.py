"""Doc guards for #2074 — the comment-path approval token, no-double-duty
disqualification, and staleness guard.

The Large-scope resumed-draft carve-out in Checkpoint 1 (auto-dev-plan.md)
used to treat ANY post-park operator comment as plan approval, including a
comment that only resolved a parked ambiguity. These guards pin the prose
that closes that gap. Deliberately separate from
test_plan_approval_fingerprint_binding.py (#2102): that file is scoped to
fingerprint-binding for the row/comment paths' equality checks; this ticket's
fix does not bind the comment-path token to draft_fp at all (see the plan's
Ambiguity 1) and instead adds three independent content/provenance/ordering
checks.

`_checkpoint1_section()` is shared through `tests.conftest`, alongside the
other command-prose readers.
"""

from tests.conftest import _appendix, _checkpoint1_section
from tests.test_auto_dev_preflight_resolutions import _after

_APPROVAL_MARKER = "<!-- auto-dev-comment-approval -->"


def test_checkpoint1_comment_path_requires_explicit_token() -> None:
    """The old unqualified sentence is gone, and the literal token appears."""
    section = _checkpoint1_section()
    assert _APPROVAL_MARKER in section
    assert "Already draft-scoped by construction" not in section
    assert "an operator reply approving the plan posted after" not in section


def test_checkpoint1_comment_path_rejects_prose_only_approval() -> None:
    """Plain-English approval ('approved', 'LGTM') is explicitly insufficient."""
    section = _checkpoint1_section()
    assert '"approved"' in section
    assert "fail closed" in section.lower()


def test_checkpoint1_never_names_audit_marker_literal() -> None:
    """The write-only #2194 marker string is never repeated in this section.

    Protects tests/test_plan_approval_fingerprint_binding.py::
    test_checkpoint1_comparison_cites_named_fingerprint_rule, which asserts
    the same absence over the same section — this is the exact MUST_FIX
    collision from the first plan-review round, pinned so it cannot regress.
    """
    section = _checkpoint1_section()
    assert "auto-dev-plan-approved" not in section
    assert "SHA-256" not in section


def test_checkpoint1_token_distinct_from_audit_marker() -> None:
    """The new token is described as distinct from the write-only #2194 CLI
    marker, without repeating that marker's literal HTML-comment grammar."""
    section = _checkpoint1_section()
    window = _after(section, _APPROVAL_MARKER, span=500)
    assert "post-marker" in window
    assert "audit-only" in window
    assert "never comment-path evidence" in window
    assert "auto-dev-plan-approved" not in window


def test_checkpoint1_disqualifies_settlement_source_comment() -> None:
    """A single comment can never both settle an item and approve the plan."""
    section = _checkpoint1_section()
    assert "No double duty" in section
    assert "Step 1c.0 step 3" in section
    window = _after(section, "settlement-source comment", span=200)
    assert "settled" in window


def test_checkpoint1_staleness_guard_names_all_fixed_headers() -> None:
    """A later park/block comment voids a stale comment-path approval."""
    section = _checkpoint1_section()
    window = _after(section, "Staleness guard", span=700)
    assert "Comment provenance rule" in window
    assert "## Pending Verification Scan" in window
    assert "## Multi-Marker Gate Blocked" in window
    assert "## Blocking Review Findings" in window
    assert "## Operator-Actionable Review Findings" in window


def test_checkpoint1_fingerprint_mismatch_subcase_untouched() -> None:
    """The row-path-only #2102 mismatch case is not disturbed by this ticket."""
    section = _checkpoint1_section()
    assert "**Fingerprint mismatch sub-case (#2102)" in section
    assert "quote both fingerprints" in section


def test_checkpoint1_row_side_evidence_window_still_intact() -> None:
    """The existing #2102 row-side evidence window (test_consolidated_park.py's
    test_checkpoint1_accepts_row_side_plan_approval_evidence) still holds once
    the comment-path detail block is inserted lower in the same section."""
    section = _checkpoint1_section()
    window = _after(
        section, "the AUTO-SKIP additionally requires approval evidence", span=2400
    )
    assert "`queue_metadata.plan_approved_at`" in window
    assert "`.claude/cw-context.json`" in window
    assert "`cw dev-queue approve`" in window
    assert "Either source is sufficient" in window
    assert "Absent both, EXIT `plan_pending_approval` again" in window


def test_appendix_approval_requested_prints_literal_token() -> None:
    """The park comment has to show the operator the exact string to paste."""
    window = _after(_appendix("plan"), "### Approval requested", span=900)
    assert _APPROVAL_MARKER in window
    assert "plan_approved_fingerprint" in window  # #2102 text still present
    assert "auto-dev-plan-approved" not in window  # protects the #2102 test


def test_appendix_approval_requested_states_separate_comment_requirement() -> None:
    section = _after(_appendix("plan"), "### Approval requested", span=900)
    assert "separate comment" in section.lower() or "its own comment" in section.lower()


def test_step1c0_cross_references_disqualification() -> None:
    """Step 1c.0 step 3 points back at Checkpoint 1's no-double-duty rule."""
    content = _appendix("plan")
    window = _after(
        content,
        "Locate **all** ordinary ticket comments posted after that park comment",
        span=1200,
    )
    assert "#2074" in window
    assert "Checkpoint 1" in window
    assert "Locate **all** ordinary ticket comments" in window
    assert "independent candidate" in window
    assert "token-bearing comment" in window
