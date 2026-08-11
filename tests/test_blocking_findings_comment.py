"""Guard tests: blocked exits post their MUST_FIX findings to the tracker (#1815).

Pure-markdown assertions over the auto-dev pipeline instruction files,
following the ``read_text()`` + literal-substring/window convention of
``test_consolidated_park.py`` / ``test_plan_persistence.py``. ``_cmd`` is
duplicated locally per the established convention; ``_after``/``_nearby`` are
imported from ``test_auto_dev_preflight_resolutions`` rather than duplicated.

Background: a `plan_unreviewable`, `plan_unsound`, or `review_blocked`
headless exit carries the blocking MUST_FIX finding(s) only inside the
`blocked` sentinel's structured payload — no tracker comment is ever posted,
so the next round (or a human triaging the ticket) has no visibility into
*why* the plan/review was rejected without separately digging through the
session transcript. This adds a single shared, greppable header,
`## Blocking Review Findings`, posted by all three exits — the same idiom
`## Pending Verification Scan` already uses for the two Step 1c park exits.
"""

from pathlib import Path

from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"
DOCS = ROOT / "docs"

HEADER = "## Blocking Review Findings"
RULE_REFERENCE = "blocking-findings comment rule"

UNREVIEWABLE_PERSISTS_ANCHOR = (
    "**MUST_FIX persists after 1 revision cycle, headless** → EXIT "
    '`blocked` with `blocker.reason: "plan_unreviewable"`.'
)
UNSOUND_FIRST_CYCLE_ANCHOR = (
    "**MUST_FIX, 1st cycle, headless** → EXIT `blocked` with "
    '`blocker.reason: "plan_unsound"`.'
)
UNSOUND_PERSISTS_ANCHOR = (
    "**MUST_FIX persists after 1 revision cycle, headless** → EXIT "
    '`blocked` with `blocker.reason: "plan_unsound"`.'
)

MECHANICAL_REJECT_ANCHOR = (
    "`.rejected[].raw` carries the original `file`/`line_start`/`summary` — "
    "surface them in the exit's `blocker.details` so the operator can "
    "adjudicate manually."
)
CYCLE5_HARD_EXIT_ANCHOR = (
    '**Headless:** EXIT `blocked` with `blocker.reason: "review_blocked"`. '
    "The `friction_highlights` field will contain the per-cycle escalation "
    "notes from cycles"
)

ROW_PLAN_UNREVIEWABLE_ANCHOR = (
    "| S1 spec review, MUST_FIX persists after 1 revision | EXIT `blocked` "
    'with `blocker.reason: "plan_unreviewable"`'
)
ROW_PLAN_UNSOUND_ANCHOR = (
    "| S1 soundness review, MUST_FIX, headless OR persists after 1 revision "
    '| EXIT `blocked` with `blocker.reason: "plan_unsound"`'
)
ROW_REVIEW_BLOCKED_ANCHOR = (
    "| S3 action list non-empty after 5 fix cycles | EXIT `blocked` with "
    '`blocker.reason: "review_blocked"`'
)

MEANING_ROW_REVIEW_BLOCKED_ANCHOR = (
    "| `review_blocked` | MUST_FIX findings persisted after 5 fix-loop "
    "cycles (the hard cap)"
)
MEANING_ROW_PLAN_UNREVIEWABLE_ANCHOR = (
    "| `plan_unreviewable` | Plan Reviewer (spec station) returned MUST_FIX "
    "both before and after a single Step 1f.4 revision cycle — the plan "
    "needs human triage, not another auto-revision. No branch created"
)
MEANING_ROW_PLAN_UNSOUND_ANCHOR = (
    "| `plan_unsound` | Plan Soundness Reviewer returned a MUST_FIX "
    "(direction contradicts a codified `ARCHITECTURE.md` §7/§8 rule) in a "
    "headless run, or it persisted after a Step 1f.4 revision cycle — the "
    "chosen direction needs human judgment. No branch created"
)

HEADLESS_CONTRACT_REVIEW_BLOCKED_ANCHOR = (
    "| `review_blocked` | MUST_FIX findings persisted after 5 fix-loop "
    "cycles (the hard cap)."
)


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _doc(name: str) -> str:
    return (DOCS / name).read_text()


def _step1a_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1a: Check for Existing Plan")
    end = content.index("### Step 1b:")
    return content[start:end]


def _step1f3_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("**Step 1f.3 — Gating:**")
    end = content.index("**Codify lessons")
    return content[start:end]


def _checkpoint3a_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("### Checkpoint 3a: Adjudicate every finding")
    end = content.index("**Small scope + NO_ISSUES")
    return content[start:end]


# ---------------------------------------------------------------------------
# 1. Step 1a excludes the new header from plan detection (AC3 / #1650 idiom).
# ---------------------------------------------------------------------------


def test_step1a_excludes_blocking_findings_header_from_plan_detection() -> None:
    """The Step 1a exclusion parenthetical grows a third fixed header."""
    section = _step1a_section()
    window = _after(section, "Pipeline-authored comment exclusion (#1650):", span=250)
    assert HEADER in window
    assert "## Pending Verification Scan" in window
    assert "## Multi-Marker Gate Blocked" in window


# ---------------------------------------------------------------------------
# 2. Step 1f.3 declares the shared comment-posting rule once.
# ---------------------------------------------------------------------------


def test_step1f3_declares_blocking_findings_comment_rule() -> None:
    """The declared rule names the header and distinguishes it from park headers."""
    section = _step1f3_section()
    assert HEADER in section
    assert "## Pending Verification Scan" in section
    assert "## Multi-Marker Gate Blocked" in section
    assert "Sentinel: append `blocking findings posted:" in section
    assert "friction_highlights" in section


# ---------------------------------------------------------------------------
# 3/4. Each of the 3 headless blocked-exit bullets posts the finding.
# ---------------------------------------------------------------------------


def test_step1f3_plan_unreviewable_exit_posts_blocking_findings() -> None:
    """The plan_unreviewable persists-exit posts the Plan Reviewer MUST_FIX."""
    section = _step1f3_section()
    window = _after(section, UNREVIEWABLE_PERSISTS_ANCHOR, span=700)
    assert "Plan Reviewer" in window
    assert RULE_REFERENCE in window
    assert ".cw/plan-draft.md" in window


def test_step1f3_plan_unsound_exits_post_blocking_findings() -> None:
    """Both plan_unsound headless exits post the Plan Soundness Reviewer MUST_FIX."""
    section = _step1f3_section()

    first_cycle_window = _after(section, UNSOUND_FIRST_CYCLE_ANCHOR, span=700)
    assert "Plan Soundness Reviewer" in first_cycle_window
    assert RULE_REFERENCE in first_cycle_window

    persists_window = _after(section, UNSOUND_PERSISTS_ANCHOR, span=700)
    assert "Plan Soundness Reviewer" in persists_window
    assert RULE_REFERENCE in persists_window


# ---------------------------------------------------------------------------
# 5. The existing "Do NOT post the plan text" sentences survive (AC2).
# ---------------------------------------------------------------------------


def test_blocking_findings_rule_still_forbids_posting_plan_text() -> None:
    """Lines 347/355's existing 'Do NOT post ... to Linear' sentences are untouched.

    Line 357 (plan_unsound persists-after-cycle) carries no such sentence
    today and this ticket does not add one — see the plan's Touch-point
    Contract for line 357. Coverage for that bullet is the posting-sentence
    assertion in test_step1f3_plan_unsound_exits_post_blocking_findings above.
    """
    section = _step1f3_section()

    unreviewable_window = _after(section, UNREVIEWABLE_PERSISTS_ANCHOR, span=400)
    assert "Do NOT post the stale plan to Linear." in unreviewable_window

    unsound_first_cycle_window = _after(section, UNSOUND_FIRST_CYCLE_ANCHOR, span=400)
    assert "Do NOT post the plan to Linear." in unsound_first_cycle_window


# ---------------------------------------------------------------------------
# 6. auto-dev-review.md Checkpoint 3a declares the same shared rule.
# ---------------------------------------------------------------------------


def test_checkpoint3a_declares_blocking_findings_comment_rule() -> None:
    """Checkpoint 3a's rule names the header, cross-references the same surface."""
    section = _checkpoint3a_section()
    assert HEADER in section
    assert "the same surface" in section
    assert "reviewer_role" in section
    assert "suggested_fix" in section
    assert (
        "Sentinel: append `blocking findings posted: review_blocked` to "
        "`friction_highlights`" in section
    )


# ---------------------------------------------------------------------------
# 7/8. Both review_blocked exit sites post the comment.
# ---------------------------------------------------------------------------


def test_review_blocked_mechanical_reject_exit_posts_blocking_findings() -> None:
    """The #1714 mechanically-rejected MUST_FIX bullet posts a tracker comment."""
    content = _cmd("auto-dev-review.md")
    window = _after(content, MECHANICAL_REJECT_ANCHOR, span=300)
    assert RULE_REFERENCE in window


def test_review_blocked_cycle5_hard_exit_posts_blocking_findings() -> None:
    """The cycle-5 hard-exit headless bullet posts a tracker comment."""
    content = _cmd("auto-dev-review.md")
    window = _after(content, CYCLE5_HARD_EXIT_ANCHOR, span=400)
    assert RULE_REFERENCE in window
    assert "Checkpoint 3a" in window


# ---------------------------------------------------------------------------
# 9. Both review_blocked sites share the one declared header, not two shapes.
# ---------------------------------------------------------------------------


def test_review_blocked_both_sites_share_same_header() -> None:
    """The header is declared exactly once; both exit sites reference the rule."""
    content = _cmd("auto-dev-review.md")
    assert content.count(HEADER) == 1

    mechanical_window = _after(content, MECHANICAL_REJECT_ANCHOR, span=300)
    cycle5_window = _after(content, CYCLE5_HARD_EXIT_ANCHOR, span=400)
    assert RULE_REFERENCE in mechanical_window
    assert RULE_REFERENCE in cycle5_window


# ---------------------------------------------------------------------------
# 10/11. auto-dev.md decision rows + blocker.reason meaning rows updated.
# ---------------------------------------------------------------------------


def test_auto_dev_decision_rows_mention_blocking_findings_comment() -> None:
    """The three S1/S3 decision rows note the new tracker comment."""
    content = _cmd("auto-dev.md")
    for row_anchor in (
        ROW_PLAN_UNREVIEWABLE_ANCHOR,
        ROW_PLAN_UNSOUND_ANCHOR,
        ROW_REVIEW_BLOCKED_ANCHOR,
    ):
        window = _after(content, row_anchor, span=200)
        assert "blocking findings" in window.lower(), row_anchor


def test_auto_dev_blocker_reason_table_mentions_blocking_findings_comment() -> None:
    """The blocker.reason meaning-table rows note the new tracker comment."""
    content = _cmd("auto-dev.md")
    for row_anchor in (
        MEANING_ROW_REVIEW_BLOCKED_ANCHOR,
        MEANING_ROW_PLAN_UNREVIEWABLE_ANCHOR,
        MEANING_ROW_PLAN_UNSOUND_ANCHOR,
    ):
        window = _after(content, row_anchor, span=350)
        assert "blocking findings" in window.lower(), row_anchor


# ---------------------------------------------------------------------------
# 12. docs/headless-contract.md's review_blocked row updated.
# ---------------------------------------------------------------------------


def test_headless_contract_review_blocked_description_updated() -> None:
    """The headless-contract review_blocked row notes the new tracker comment."""
    content = _doc("headless-contract.md")
    window = _after(content, HEADLESS_CONTRACT_REVIEW_BLOCKED_ANCHOR, span=200)
    assert "blocking findings" in window.lower()
