"""Doc guards for #2102 — plan-approval evidence bound to the approved draft.

`plan_approved_at` records *that* an approval happened, never *which draft* it
was given for, so a resumed draft that changed after approval slips through
Checkpoint 1's Large-scope carve-out unchallenged. These guards pin the prose
that closes that gap: a single named fingerprint rule, producer instructions
citing it at every plan-stage sentinel emission, and Checkpoint 1's
equality-gated consumption of the row-side evidence.

Marker language stays out of every evidence site: the
`<!-- auto-dev-plan-approved: <sha> -->` comment that `cw dev-queue approve
--post-marker` posts (#2194) is a write-only, audit-only record that no Stage 1
decision logic reads. It embeds the draft fingerprint for display, and the
guards below pin that it is never an evidence source.
"""

import pytest

from tests.conftest import _REPO_ROOT, _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

_FINGERPRINT_RULE_NAME = "Plan-draft fingerprint rule"

_PLAN_APPROVED_MARKER_NAME = "auto-dev-plan-approved"

# Docs that must state the marker is audit-only, at the one place each names
# it. `.claude/commands/` prose is read through conftest's `_cmd`; the rest
# are repo-root paths.
_MARKER_DOCS = (
    "README.md",
    "docs/dispatch-runbook.md",
    "docs/session-disposition.md",
    ".claude/commands/auto-dev.md",
)

_COMMANDS_PREFIX = ".claude/commands/"


def _doc_text(relative: str) -> str:
    return (
        _cmd(relative[len(_COMMANDS_PREFIX) :])
        if relative.startswith(_COMMANDS_PREFIX)
        else (_REPO_ROOT / relative).read_text(encoding="utf-8")
    )


def _window_around(
    content: str, anchor: str, before: int = 400, after: int = 700
) -> str:
    """Text on BOTH sides of *anchor*.

    The status-table rows put `audit-only` ahead of the marker string, so the
    forward-only `_after` and backward-only `_nearby` windows imported above
    each miss half of what these guards have to see.
    """
    idx = content.index(anchor)
    return content[max(0, idx - before) : idx + after]


def _plan_doc() -> str:
    return _cmd("auto-dev-plan.md")


def _checkpoint1_section() -> str:
    content = _plan_doc()
    start = content.index("### Checkpoint 1 (Plan Approval)")
    end = content.index("### Step 1e:")
    return content[start:end]


def _fingerprint_rule_section() -> str:
    content = _plan_doc()
    start = content.index(f"### {_FINGERPRINT_RULE_NAME}")
    return content[start : content.index("### Step 1d:", start)]


def _chained_sentinel_window(span: int = 3000) -> str:
    """The chained-monolith `AUTO_DEV_RESULT` example plus the prose under it.

    `auto-dev.md` owns the single final sentinel on the chained `/auto-dev`
    path, so its template is a second producer contract — not an illustration
    of the standalone one.
    """
    content = _cmd("auto-dev.md")
    start = content.index("<<<AUTO_DEV_RESULT\n{")
    return content[start : start + span]


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
    window = _nearby(section, "quote both fingerprints", span=700)
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


def test_chained_monolith_template_includes_plan_draft_fingerprint_key() -> None:
    """The chained path emits its own final sentinel from `auto-dev.md`'s
    template; a key absent there is a key no chained round ever populates,
    which makes the whole binding a no-op on that path."""
    window = _chained_sentinel_window()
    template = window[: window.index("AUTO_DEV_RESULT>>>")]
    assert '"plan_draft_fingerprint"' in template


def test_chained_monolith_template_cites_named_fingerprint_rule() -> None:
    """Same one-definition discipline as the standalone template: cite the
    rule by name, never restate the hashing steps beside the example."""
    window = _chained_sentinel_window()
    assert f"*{_FINGERPRINT_RULE_NAME}*" in window
    assert "SHA-256" not in window
    assert "null" in window


@pytest.mark.parametrize("relative", _MARKER_DOCS)
def test_marker_documented_audit_only(relative: str) -> None:
    """Every doc that names the marker has to say, right there, that it is
    audit-only -- an operator who reads one page and not the others must not
    come away thinking the comment is what makes an approval real (#2194)."""
    window = _window_around(_doc_text(relative), _PLAN_APPROVED_MARKER_NAME)
    assert "audit-only" in window


def test_provenance_rule_excludes_marker_from_evidence() -> None:
    """The comment-provenance rule is where the exclusion has to live: the
    marker is an unmarked operator-authored comment posted right after a park
    comment, so Checkpoint 1's comment path could plausibly read it."""
    window = _after(_cmd("auto-dev.md"), _PLAN_APPROVED_MARKER_NAME, span=900)
    assert "never plan-approval evidence" in window


def test_fingerprint_rule_names_audit_marker_display_only() -> None:
    """The rule now has to account for the marker carrying the fingerprint
    (#2194) without promoting it to a transport: display, never evidence."""
    section = _fingerprint_rule_section()
    assert "audit marker" in section
    assert "display, never evidence" in section


def test_fingerprint_rule_claims_no_comment_transport() -> None:
    """The only evidence transport is sentinel -> session -> dev-queue row ->
    `cw-context.json`. `--post-marker` (#2194) embeds the fingerprint in an
    audit marker for display only; a rule advertising a comment channel as
    evidence points a producer at a path nothing reads."""
    section = _fingerprint_rule_section()
    assert "tracker comment" not in section
    assert "marker comment" not in section
    assert "plan_approved_fingerprint" in section
    assert "queue_metadata" in section


def test_fingerprint_transport_keys_match_their_model_field_names() -> None:
    """The two transport keys are defined once and must keep naming the fields
    they carry: a future field rename fails here instead of silently
    desynchronizing sentinel -> row -> queue_metadata."""
    from cw.auto_dev_result import AutoDevResult
    from cw.models import (
        PLAN_APPROVED_FINGERPRINT_KEY,
        PLAN_DRAFT_FINGERPRINT_KEY,
        TicketTask,
    )

    assert PLAN_DRAFT_FINGERPRINT_KEY in AutoDevResult.model_fields
    assert PLAN_APPROVED_FINGERPRINT_KEY in TicketTask.model_fields


def test_checkpoint1_comparison_cites_named_fingerprint_rule() -> None:
    """Checkpoint 1 cites the same single rule, and binds only the row-side
    evidence pair — the marker comment is not an evidence source."""
    section = _checkpoint1_section()
    assert f"*{_FINGERPRINT_RULE_NAME}*" in section
    assert "auto-dev-plan-approved" not in section
    # The rule is defined once, not restated inside Checkpoint 1.
    assert "SHA-256" not in section
