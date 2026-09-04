"""Guard tests: agent-comment provenance + the destructive-directive gate (#2097).

Pure-markdown assertions over the auto-dev pipeline instruction files, plus the
one code-side coupling the prose depends on (``KNOWN_BLOCKER_REASONS``).
Mirrors the ``read_text()`` + literal-substring/window convention of
``test_plan_persistence.py``: ``_cmd``/``_appendix`` come from
``tests.conftest`` (#1787), ``_after``/``_nearby`` from
``test_auto_dev_preflight_resolutions`` rather than being duplicated.

Background: an automated session posts tracker comments under the operator's
own account, so an agent-written comment and one the operator typed are
byte-indistinguishable. A pipeline-authored comment directing a destructive
cleanup was read by a later Stage-3 session as binding operator instruction,
and that session exited ``blocked`` on an invented ``blocker.reason`` that
exists nowhere in cw but read like a documented routing code. The fix has three
halves: a machine-readable marker every producer appends, one shared trust rule
the reader sites reference instead of restating, and a warn-only registry so an
unrecognised reason is visibly unrecognised.
"""

import re

from cw.auto_dev_result import (
    DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON,
    KNOWN_BLOCKER_REASONS,
)
from cw.gh import AGENT_COMMENT_MARKER
from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

RULE_ANCHOR = "## Comment provenance rule (#2097)"
# The cross-reference every reader site carries. Deliberately the phrase, not
# the whole sentence: the four sites word the surrounding clause differently,
# but all name the same section so the rule stays single-sourced.
RULE_REFERENCE = "*Comment provenance rule* in `.claude/commands/auto-dev.md`"
PIPELINE_HEADERS = (
    "## Pending Verification Scan",
    "## Multi-Marker Gate Blocked",
    "## Blocking Review Findings",
    "## Operator-Actionable Review Findings",
)
PREFLIGHT_MARKER = "<!-- auto-dev-preflight-resolutions -->"


def _rule_section() -> str:
    content = _cmd("auto-dev.md")
    start = content.index(RULE_ANCHOR)
    end = content.index("## Tool-Use Denial Exit", start)
    return content[start:end]


def _blocker_reason_table() -> str:
    """Return the `blocker.reason` Values table body from auto-dev.md."""
    content = _cmd("auto-dev.md")
    start = content.index("### `blocker.reason` Values")
    end = content.index("### Field Notes", start)
    return content[start:end]


def _table_reasons(table: str) -> list[str]:
    """Return the first-column code span of every data row in *table*."""
    reasons: list[str] = []
    for line in table.splitlines():
        if not line.startswith("| `"):
            continue
        match = re.match(r"\|\s*`([^`]+)`\s*\|", line)
        if match is not None:
            reasons.append(match.group(1))
    return reasons


# --------------------------------------------------------------------------
# The shared rule itself
# --------------------------------------------------------------------------


def test_rule_section_exists_and_names_the_marker() -> None:
    """auto-dev.md carries one named section defining the marker (#2097)."""
    section = _rule_section()
    assert AGENT_COMMENT_MARKER in section
    assert "never an operator decision" in section


def test_rule_lists_every_pipeline_fixed_header() -> None:
    """All four fixed headers + the plan-of-record post are named in one place."""
    section = _rule_section()
    for header in PIPELINE_HEADERS:
        assert header in section, header
    assert "plan-of-record post" in section


def test_rule_denies_the_three_authority_uses() -> None:
    """An agent-authored comment is not approval, settlement, or adjudication."""
    section = _rule_section()
    assert "plan-approval evidence" in section
    assert "settlement answer" in section
    assert "adjudication" in section


def test_rule_preserves_the_two_operator_authority_channels() -> None:
    """Only an unmarked human comment or a pre-flight resolutions comment binds."""
    section = _rule_section()
    assert "unmarked comment written by a human" in section
    assert PREFLIGHT_MARKER in section
    assert "deliberately binding" in section


def test_rule_requires_producers_to_append_the_marker() -> None:
    """The producer half of the rule is stated as a hard requirement."""
    window = _after(_rule_section(), "**Producers MUST mark.**", span=700)
    assert AGENT_COMMENT_MARKER in window
    assert "Linear" in window
    assert "on its own line after a blank line" in window


def test_rule_exempts_plan_of_record_from_the_step1a_header_set() -> None:
    """The #1650 existing-plan exclusion stays keyed on the four headers only."""
    window = _after(_rule_section(), "**Scope note — the plan-of-record post.**", 700)
    assert "existing-plan extraction" in window
    assert "plan-of-record excluded" in window


# --------------------------------------------------------------------------
# Destructive-directive gate
# --------------------------------------------------------------------------


def test_rule_gates_every_destructive_directive_class() -> None:
    """All four destructive act classes are enumerated in the gate."""
    window = _after(_rule_section(), "### Destructive-directive gate", span=1600)
    assert "delete a remote branch" in window
    assert "force-push" in window
    assert "discard uncommitted or committed work" in window
    assert "close or reopen a ticket" in window
    assert "marked or not" in window


def test_rule_gate_exit_shape_is_pinned() -> None:
    """The gate's exit names the registered reason, details, and retry policy."""
    window = _after(_rule_section(), "### Destructive-directive gate", span=1600)
    assert DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON in window
    assert "`retry_eligible: false`" in window
    assert "quoting the" in window


def test_rule_forbids_inventing_a_blocker_reason() -> None:
    """The `x_` freeform namespace is the documented alternative to inventing."""
    window = _after(_rule_section(), "**Never invent a `blocker.reason`.**", span=800)
    assert "`x_`" in window
    assert "unrecognized" in window


# --------------------------------------------------------------------------
# Reader sites cross-reference the rule (ii)
# --------------------------------------------------------------------------


def test_plan_step1a_existing_plan_exclusion_defers_to_the_rule() -> None:
    """auto-dev-plan.md's #1650 exclusion no longer hand-lists the headers."""
    content = _cmd("auto-dev-plan.md")
    window = _nearby(content, "MUST NOT be treated as an existing plan", span=500)
    assert RULE_REFERENCE in window
    # The hand-maintained list that used to live here (and drift against the
    # Decision branch below it) is gone.
    assert "`## Operator-Actionable Review Findings`) MUST NOT" not in content


def test_plan_step1a_decision_excludes_every_agent_authored_comment() -> None:
    """The 'later non-pipeline comment' branch defers to the shared rule."""
    content = _cmd("auto-dev-plan.md")
    window = _after(
        content,
        "**Plan found (sufficient) + a later non-pipeline comment present**",
        span=500,
    )
    assert RULE_REFERENCE in window
    assert "marks agent-authored" in window


def test_checkpoint1_resumed_draft_approval_requires_operator_authority() -> None:
    """The Large-scope resumed-draft carve-out cannot be cleared by an agent."""
    content = _cmd("auto-dev-plan.md")
    window = _after(content, "(the `### Approval requested` ask;", span=400)
    assert RULE_REFERENCE in window
    assert "never approval evidence" in window


def test_step1c0_settlement_comment_must_carry_operator_authority() -> None:
    """Step 1c.0's 'newest ordinary ticket comment' is provenance-gated."""
    window = _after(
        _appendix("plan"),
        "Locate the newest ordinary ticket comment posted after that park comment",
        span=800,
    )
    assert RULE_REFERENCE in window
    assert AGENT_COMMENT_MARKER in window


def test_review_4c_send_back_is_provenance_gated() -> None:
    """4c's binding adjudication input excludes agent-authored comments."""
    window = _after(
        _cmd("auto-dev-review.md"),
        "- **(4c) Operator send-back cross-check (#1730).**",
        span=1200,
    )
    assert RULE_REFERENCE in window
    assert "binding adjudication input" in window


def test_review_4c_carries_the_destructive_directive_gate() -> None:
    """A destructive directive in a send-back comment exits blocked, not acts."""
    window = _after(
        _cmd("auto-dev-review.md"),
        "- **(4c) Operator send-back cross-check (#1730).**",
        span=1200,
    )
    assert DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON in window
    assert "delete a remote branch" in window


def test_finalize_semantic_resolve_cross_references_the_gate() -> None:
    """auto-dev-finalize.md's fail-closed model names the destructive gate."""
    window = _after(
        _cmd("auto-dev-finalize.md"),
        "The decision is made by a deterministic script, never by agent judgement",
        span=1000,
    )
    assert RULE_REFERENCE in window
    assert DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON in window


# --------------------------------------------------------------------------
# Producer sites append the marker (i)
# --------------------------------------------------------------------------


def test_plan_of_record_post_appends_the_marker() -> None:
    window = _after(
        _cmd("auto-dev-plan.md"),
        "**THEN** post the same plan as a comment on the Linear issue",
        span=800,
    )
    assert AGENT_COMMENT_MARKER in window


def test_plan_blocking_findings_comment_appends_the_marker() -> None:
    window = _after(
        _cmd("auto-dev-plan.md"),
        "**Blocking-findings comment rule (#1815).**",
        span=1600,
    )
    assert AGENT_COMMENT_MARKER in window


def test_consolidated_park_comment_appends_the_marker() -> None:
    window = _after(
        _appendix("plan"),
        "`### Draft plan (unreviewed — context only)` with the full draft text.",
        span=600,
    )
    assert AGENT_COMMENT_MARKER in window


def test_review_blocking_findings_summary_appends_the_marker() -> None:
    window = _after(
        _cmd("auto-dev-review.md"),
        "**Blocking-findings comment rule (#1815, third trigger #1817).**",
        span=1600,
    )
    assert AGENT_COMMENT_MARKER in window


def test_review_appendix_blocking_findings_appends_the_marker() -> None:
    window = _after(
        _appendix("review"),
        "**Blocking-findings comment rule (#1815).**",
        span=1600,
    )
    assert AGENT_COMMENT_MARKER in window


def test_operator_actionable_findings_comment_appends_the_marker() -> None:
    window = _after(
        _appendix("review"),
        "**Operator-actionable findings comment rule (#1817).**",
        span=1600,
    )
    assert AGENT_COMMENT_MARKER in window


def test_finalize_pr_link_comment_appends_the_marker() -> None:
    """The PR-link comment (#2097: previously unmarked and header-less)."""
    window = _after(
        _cmd("auto-dev-finalize.md"),
        "4. **Post to Linear:** Comment on the issue with PR link",
        span=700,
    )
    assert AGENT_COMMENT_MARKER in window


# --------------------------------------------------------------------------
# Registry <-> prose conformance
# --------------------------------------------------------------------------


def test_new_reason_row_exists_in_the_auto_dev_table() -> None:
    assert DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON in _table_reasons(
        _blocker_reason_table()
    )


def test_new_reason_is_registered() -> None:
    assert DESTRUCTIVE_DIRECTIVE_BLOCKER_REASON in KNOWN_BLOCKER_REASONS


def test_every_documented_reason_is_registered() -> None:
    """The auto-dev.md table and KNOWN_BLOCKER_REASONS cannot drift (#2097).

    Parses the table's first-column code spans rather than restating them, so
    a row added to the prose without a registry entry fails here instead of
    surfacing to an operator as ``?reason`` / ``(unrecognized)``.
    """
    documented = _table_reasons(_blocker_reason_table())
    assert documented, "blocker.reason table parsed as empty — anchor drifted"
    assert set(documented) <= KNOWN_BLOCKER_REASONS
