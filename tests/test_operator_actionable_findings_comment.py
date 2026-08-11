"""Guard tests: the OPERATOR ACTIONABLE route for a non-anchorable MUST_FIX (#1817).

Pure-markdown assertions over the auto-dev pipeline instruction files, mirroring
``test_blocking_findings_comment.py``'s ``read_text()`` + literal-substring/window
convention. ``_cmd``/``_doc`` are duplicated locally per the established
convention; ``_after`` is imported from ``test_auto_dev_preflight_resolutions``
rather than duplicated.

Background: a reviewer finding whose remedy lies entirely outside the diff — an
acceptance criterion demanding a follow-up ticket that was never filed (#1764) —
had no honest way to be expressed. Reviewers invented a fake ``file`` value,
which ``_classify_finding`` then mechanically rejected as ``unknown_file``,
routing a genuine MUST_FIX into #1714's operator park with an unreliable anchor
instead of into an actionable operator checklist. This adds an explicit
``no_diff_anchor`` marker, a 4th adjudication bucket, and a tracker comment
under the fixed header ``## Operator-Actionable Review Findings``.
"""

from pathlib import Path

from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
DOCS = ROOT / "docs"

HEADER = "## Operator-Actionable Review Findings"
BLOCKING_HEADER = "## Blocking Review Findings"
RULE_REFERENCE = "operator-actionable findings comment rule"
SENTINEL = "operator actionable findings posted: review_operator_actionable"
EXIT_REASON = "review_operator_actionable"

STEP_3C_ANCHOR = "### Step 3c: Verify the `fixed` claims against the diff (#1805)"
CHECKPOINT_3A_CLOSING_ANCHOR = (
    "**Headless:** Always run reviewers, then adjudicate every finding per "
    "Checkpoint 3a"
)

ROW_OPERATOR_ACTIONABLE_ANCHOR = (
    "| S3 accepted MUST_FIX finding whose remedy is outside the diff "
    '(`no_diff_anchor`) | EXIT `blocked` with `blocker.reason: '
    '"review_operator_actionable"`'
)
MEANING_ROW_OPERATOR_ACTIONABLE_ANCHOR = (
    "| `review_operator_actionable` | An accepted MUST_FIX finding carrying "
    "`no_diff_anchor: true`"
)


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _doc(name: str) -> str:
    return (DOCS / name).read_text()


def _checkpoint3a_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("### Checkpoint 3a: Adjudicate every finding")
    end = content.index("**Small scope + NO_ISSUES")
    return content[start:end]


def _checkpoint3a_closing_paragraph() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index(CHECKPOINT_3A_CLOSING_ANCHOR)
    end = content.index("### Step 3b: Fix Loop")
    return content[start:end]


def _step3c_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index(STEP_3C_ANCHOR)
    end = content.index("## Stage 3 Completion (headless only)")
    return content[start:end]


def _step1a_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1a: Check for Existing Plan")
    end = content.index("### Step 1b:")
    return content[start:end]


# ---------------------------------------------------------------------------
# 1. The reviewer output contract documents the marker and its "N/A" literal.
# ---------------------------------------------------------------------------


def test_review_findings_schema_snippet_documents_no_diff_anchor() -> None:
    """The REVIEW_FINDINGS block reviewers must emit carries the new field."""
    content = _cmd("auto-dev-review.md")
    window = _after(content, "<<<REVIEW_FINDINGS", span=1200)
    assert "no_diff_anchor" in window


def test_reviewer_guidance_pins_the_na_file_literal() -> None:
    """Round-5 Q5: the fixed literal, not a per-reviewer convention."""
    content = _cmd("auto-dev-review.md")
    window = _after(content, "<<<REVIEW_FINDINGS", span=3000)
    assert '"file": "N/A"' in window
    assert "no_diff_anchor" in window
    assert "line_start" in window


# ---------------------------------------------------------------------------
# 2. Checkpoint 3a declares the 4th bucket with both exclusion axes.
# ---------------------------------------------------------------------------


def test_checkpoint3a_declares_operator_actionable_bucket() -> None:
    section = _checkpoint3a_section()
    assert "OPERATOR ACTIONABLE" in section
    assert "no_diff_anchor" in section


def test_operator_actionable_bucket_is_never_fix_now_eligible() -> None:
    section = _checkpoint3a_section()
    window = _after(section, "OPERATOR ACTIONABLE", span=1400)
    assert "FIX NOW" in window
    assert "never eligible" in window.lower()


def test_operator_actionable_bucket_excludes_non_deferrable_findings() -> None:
    """Decision A1: NON_DEFERRABLE wins outright and routes to plan_deviation."""
    section = _checkpoint3a_section()
    window = _after(section, "OPERATOR ACTIONABLE", span=1400)
    assert "NON_DEFERRABLE" in window
    assert "plan_deviation" in window


def test_operator_actionable_bucket_is_must_fix_scoped() -> None:
    """Decision C2: a SHOULD_FIX no_diff_anchor finding goes through DEFER."""
    section = _checkpoint3a_section()
    window = _after(section, "OPERATOR ACTIONABLE", span=1400)
    assert "MUST_FIX" in window
    assert "SHOULD_FIX" in window
    assert "DEFER" in window


# ---------------------------------------------------------------------------
# 3. The ADJUDICATIONS recording bullets gain the new outcome.
# ---------------------------------------------------------------------------


def test_adjudications_recording_bullets_include_operator_action() -> None:
    section = _checkpoint3a_section()
    window = _after(section, "**Recording adjudication:**", span=1600)
    assert 'outcome: "operator_action"' in window
    assert "rationale" in window


def test_adjudication_json_shape_lists_operator_action_outcome() -> None:
    """The copy-me JSON snippet's outcome enum grows the 4th value."""
    section = _checkpoint3a_section()
    assert "<fix|reject|defer|operator_action>" in section


# ---------------------------------------------------------------------------
# 4. The new comment rule exists, names its own header, and posts its sentinel.
# ---------------------------------------------------------------------------


def test_operator_actionable_comment_rule_declared_once() -> None:
    content = _cmd("auto-dev-review.md")
    assert content.count(HEADER) == 1
    section = _checkpoint3a_section()
    assert HEADER in section
    assert RULE_REFERENCE in section


def test_operator_actionable_comment_rule_documents_checklist_format() -> None:
    """Adopted Assumption 3: GitHub-native task-list syntax, role noted inline."""
    section = _checkpoint3a_section()
    window = _after(section, HEADER, span=1200)
    assert "- [ ]" in window
    assert "suggested_fix" in window
    assert "reviewer_role" in window


def test_operator_actionable_comment_rule_appends_friction_sentinel() -> None:
    """Adopted Assumption 4: mirrors the `blocking findings posted:` idiom."""
    section = _checkpoint3a_section()
    assert SENTINEL in section
    window = _after(section, SENTINEL, span=200)
    assert "friction_highlights" in window


def test_operator_actionable_comment_trigger_is_decoupled_from_blocker_reason() -> None:
    """Round-5 Q1: the comment posts on the ADJUDICATIONS entry, not the exit."""
    section = _checkpoint3a_section()
    window = _after(section, HEADER, span=1400)
    assert "ADJUDICATIONS" in window
    assert "blocker.reason" in window


# ---------------------------------------------------------------------------
# 5. The override is wired at Step 3c — NOT the Checkpoint 3a closing paragraph.
# ---------------------------------------------------------------------------


def test_operator_action_override_wired_at_step_3c() -> None:
    """Round-5 Q2 regression guard: the WHERE claim must not drift back."""
    section = _step3c_section()
    assert EXIT_REASON in section
    assert "operator_action" in section
    assert RULE_REFERENCE in section or HEADER in section


def test_operator_action_override_not_wired_at_checkpoint3a_closing() -> None:
    """The companion negative check for the stale wiring location."""
    paragraph = _checkpoint3a_closing_paragraph()
    assert EXIT_REASON not in paragraph


def test_step3c_scopes_the_override_to_its_two_funnelled_exits() -> None:
    """Round-6 P1: cycle-5 exhaustion never flows through Step 3c."""
    section = _step3c_section()
    assert "sparse-feedback skip" in section
    assert "cycle 5" in section or "cycle-5" in section
    assert "review_blocked" in section


def test_step3c_override_routes_to_blocked_on_user() -> None:
    section = _step3c_section()
    window = _after(section, EXIT_REASON, span=900)
    assert "blocked" in window
    assert "stage3_review" in window
    assert "BLOCKED_ON_USER" in window


# ---------------------------------------------------------------------------
# 6. The #1815 header is never re-spelled while being cross-referenced.
# ---------------------------------------------------------------------------


def test_blocking_findings_header_still_declared_exactly_once() -> None:
    content = _cmd("auto-dev-review.md")
    assert content.count(BLOCKING_HEADER) == 1


# ---------------------------------------------------------------------------
# 7. Step 1a's plan-detection exclusion parenthetical grows a fourth header.
# ---------------------------------------------------------------------------


def test_step1a_excludes_operator_actionable_header_from_plan_detection() -> None:
    section = _step1a_section()
    window = _after(section, "Pipeline-authored comment exclusion (#1650):", span=350)
    assert HEADER in window
    assert BLOCKING_HEADER in window
    assert "## Pending Verification Scan" in window
    assert "## Multi-Marker Gate Blocked" in window


# ---------------------------------------------------------------------------
# 8. auto-dev.md's decision + blocker.reason tables carry the new exit.
# ---------------------------------------------------------------------------


def test_auto_dev_decision_table_has_operator_actionable_row() -> None:
    content = _cmd("auto-dev.md")
    assert ROW_OPERATOR_ACTIONABLE_ANCHOR in content


def test_auto_dev_blocker_reason_table_has_operator_actionable_row() -> None:
    content = _cmd("auto-dev.md")
    window = _after(content, MEANING_ROW_OPERATOR_ACTIONABLE_ANCHOR, span=600)
    assert "BLOCKED_ON_USER" in window
    assert "operator" in window.lower()


# ---------------------------------------------------------------------------
# 9. docs/headless-contract.md's blocker.reason table carries the new row.
# ---------------------------------------------------------------------------


def test_headless_contract_has_operator_actionable_row() -> None:
    content = _doc("headless-contract.md")
    window = _after(content, MEANING_ROW_OPERATOR_ACTIONABLE_ANCHOR, span=600)
    assert "no_diff_anchor" in window
    assert "stage3_review" in window
