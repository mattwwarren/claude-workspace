"""Doc guards for #2102 — plan-approval evidence bound to the approved draft.

`plan_approved_at` records *that* an approval happened, never *which draft* it
was given for, so a resumed draft that changed after approval slips through
Checkpoint 1's Large-scope carve-out unchallenged. These guards pin the prose
that closes that gap: a single named fingerprint rule, producer instructions
citing it at every plan-stage sentinel emission, and Checkpoint 1's
equality-gated consumption of the row-side evidence.

Marker-comment language is deliberately absent throughout: the
`<!-- auto-dev-plan-approved -->` comment is a write-only audit record that no
Stage 1 decision logic reads, and this ticket does not make it an evidence
source.
"""

from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

_FINGERPRINT_RULE_NAME = "Plan-draft fingerprint rule"


def _plan_doc() -> str:
    return _cmd("auto-dev-plan.md")


def _checkpoint1_section() -> str:
    content = _plan_doc()
    start = content.index("### Checkpoint 1 (Plan Approval)")
    end = content.index("### Step 1e:")
    return content[start:end]


def test_checkpoint1_plan_approved_at_requires_fingerprint_match() -> None:
    """The headline #2102 change: a non-null `plan_approved_at` is no longer
    sufficient on its own — it must be accompanied by a matching fingerprint."""
    section = _checkpoint1_section()
    assert "`queue_metadata.plan_approved_fingerprint`" in section
    assert "Either source alone is sufficient" not in section
    # The row-side clause names the equality condition, not mere non-nullness.
    window = _after(section, "`queue_metadata.plan_approved_at`", span=900)
    assert "plan_approved_fingerprint" in window
    assert "equal" in window


def test_checkpoint1_mismatch_reparks_with_both_fingerprints_quoted() -> None:
    """A mismatch is its own EXIT sub-case, and the re-park comment has to name
    both values so the operator can see what changed under the approval."""
    section = _checkpoint1_section()
    assert "mismatch" in section
    window = _nearby(section, "quoting both fingerprints", span=700)
    assert "plan_pending_approval" in window


def test_plan_draft_fingerprint_computed_from_stripped_bookkeeping_lines() -> None:
    """The rule hashes plan text, not bookkeeping: a round-counter increment or
    a new settlement marker must not read as a changed draft."""
    content = _plan_doc()
    window = _after(content, f"### {_FINGERPRINT_RULE_NAME}", span=1200)
    assert "SHA-256" in window
    assert "plan-stage-scan-round" in window
    assert "plan-stage-last-evaluated" in window
    assert "plan-stage-settled" in window
    assert "strip" in window.lower()


def test_consolidated_park_approval_requested_names_fingerprint() -> None:
    """The `### Approval requested` ask tells the operator the approval is
    bound to this draft — so they know a later edit revokes it."""
    window = _after(_appendix("plan"), "### Approval requested", span=900)
    assert "plan_approved_fingerprint" in window
    assert "auto-dev-plan-approved" not in window


def test_stage1_completion_template_includes_plan_draft_fingerprint_key() -> None:
    """The standalone headless sentinel template is the producer contract; a
    field missing from it is a field no standalone round ever emits."""
    content = _plan_doc()
    template = _after(content, "## Stage 1 Completion (headless only)", span=3000)
    assert '"plan_draft_fingerprint"' in template


def test_stage1_completion_producer_instruction_cites_named_fingerprint_rule() -> None:
    """One definition of the computation, cited by name — not restated per
    emission site, where the copies would drift."""
    content = _plan_doc()
    window = _after(content, "## Stage 1 Completion (headless only)", span=6000)
    assert f"*{_FINGERPRINT_RULE_NAME}*" in window
    assert "every park exit" in window
    assert "null" in window


def test_checkpoint1_comparison_cites_named_fingerprint_rule() -> None:
    """Checkpoint 1 cites the same single rule, and binds only the row-side
    evidence pair — the marker comment is not an evidence source."""
    section = _checkpoint1_section()
    assert f"*{_FINGERPRINT_RULE_NAME}*" in section
    assert "auto-dev-plan-approved" not in section
    # The rule is defined once, not restated inside Checkpoint 1.
    assert "SHA-256" not in section
