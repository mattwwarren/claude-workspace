"""Guard tests (#1890): every headless stage doc that terminates a session
must carry both (a) frame-emission discipline ("recording is not framing" —
formerly "validating is not emitting", reworded when #2382 made
``cw result emit`` the pre-frame step; see ``test_sentinel_emit_rule.py``)
and (b) the no-interactive-escalation warning ("no listener, never ask a
question") in its own Stage-N-Completion section — not only in the
chained-path Appendix of auto-dev.md.

Forensics (2026-08-16 dead-flat sessions, #1886): #1833 validated a sentinel
and narrated "Emitting the final result" without ever emitting the literal
frame; #1750 detected a real blocker and escalated it as an interactive
question in a headless session with no listener instead of the `blocked`
sentinel. Both gaps are model-adherence, not schema — this pins the prose
that closes them.
"""

import pytest

from tests.conftest import _COMMANDS_ROOT, _appendix, _cmd

FRAME_DISCIPLINE_ANCHOR = "Recording is not framing"
FRAME_DISCIPLINE_DETAIL = "final characters of this same message"
NO_ESCALATION_ANCHOR = "no listener"
NO_ESCALATION_DETAIL = 'status: "blocked"'

RESOLUTION_FIELDS = ("resolution_consumed", "resolution_evidence")


def _section(content: str, start_anchor: str, end_anchor: str | None) -> str:
    start = content.index(start_anchor)
    end = content.index(end_anchor, start) if end_anchor else len(content)
    return content[start:end]


def _plan_completion() -> str:
    return _section(
        _cmd("auto-dev-plan.md"), "## Stage 1 Completion (headless only)", None
    )


def _impl_completion() -> str:
    return _section(
        _cmd("auto-dev-impl.md"), "## Stage 2 Completion (headless only)", None
    )


def _review_completion() -> str:
    return _section(
        _cmd("auto-dev-review.md"), "## Stage 3 Completion (headless only)", None
    )


def _finalize_completion() -> str:
    return _section(
        _cmd("auto-dev-finalize.md"), "## Stage 4+5 Completion (headless only)", None
    )


def _intake_discipline() -> str:
    return _section(
        _cmd("auto-dev-intake.md"),
        "### Sentinel-Emission Discipline",
        "## Pre-flight: Origin Sync Check",
    )


def _monolith_headless_mode() -> str:
    return _section(_cmd("auto-dev.md"), "## Headless Mode", "### Gate-Collapse Table")


def _monolith_appendix() -> str:
    return _section(
        _cmd("auto-dev.md"),
        "## Appendix: Structured Output",
        "### `plan_source` Values (closed)",
    )


SECTIONS_WITH_FRAME_DISCIPLINE = {
    "auto-dev-plan.md Stage 1 Completion": _plan_completion,
    "auto-dev-impl.md Stage 2 Completion": _impl_completion,
    "auto-dev-review.md Stage 3 Completion": _review_completion,
    "auto-dev-finalize.md Stage 4+5 Completion": _finalize_completion,
    "auto-dev-intake.md Sentinel-Emission Discipline": _intake_discipline,
    "auto-dev.md Appendix": _monolith_appendix,
}

SECTIONS_WITH_ESCALATION_WARNING = {
    "auto-dev-plan.md Stage 1 Completion": _plan_completion,
    "auto-dev-impl.md Stage 2 Completion": _impl_completion,
    "auto-dev-review.md Stage 3 Completion": _review_completion,
    "auto-dev-finalize.md Stage 4+5 Completion": _finalize_completion,
    "auto-dev-intake.md Sentinel-Emission Discipline": _intake_discipline,
    "auto-dev.md Headless Mode": _monolith_headless_mode,
}


def test_frame_emission_discipline_present_in_every_terminating_section() -> None:
    for name, getter in SECTIONS_WITH_FRAME_DISCIPLINE.items():
        section = getter()
        assert FRAME_DISCIPLINE_ANCHOR in section, (
            f"{name} missing frame-emission discipline prose"
        )
        assert FRAME_DISCIPLINE_DETAIL in section, (
            f"{name} missing 'final characters' detail"
        )


def test_no_interactive_escalation_warning_present_in_every_terminating_section() -> (
    None
):
    for name, getter in SECTIONS_WITH_ESCALATION_WARNING.items():
        section = getter()
        assert NO_ESCALATION_ANCHOR in section, f"{name} missing 'no listener' warning"
        assert NO_ESCALATION_DETAIL in section, (
            f"{name} missing blocked-status routing detail"
        )


def test_intake_discipline_subsection_precedes_all_three_standalone_exits() -> None:
    """The discipline text is read before any of Stage 0's three standalone EXITs.

    #1879 moved all three EXIT sentinels into ``auto-dev-intake-appendix.md``
    (each fires only on a rare condition -- origin divergence, a failed fetch,
    a pre-existing open PR). The ordering property still has to hold on the
    core file the worker actually reads first, so it is asserted here against
    the appendix-Read trigger sentence that now stands in each EXIT's place.
    """
    content = _cmd("auto-dev-intake.md")
    discipline_pos = content.index("### Sentinel-Emission Discipline")
    p3_exit_pos = content.index("Origin Sync Check divergence handling")
    fetch_failure_exit_pos = content.index("Fetch-failure signature mirror")
    open_pr_exit_pos = content.index("Open-PR self-check (#1862)")
    assert discipline_pos < p3_exit_pos
    assert discipline_pos < fetch_failure_exit_pos
    assert discipline_pos < open_pr_exit_pos


def test_intake_appendix_retains_all_three_exit_sentinel_reasons() -> None:
    """The three relocated EXIT sentinels keep their exact ``reason`` literals.

    Companion to the test above: the ordering assertion moved to the core
    doc's trigger sentences, so the literals themselves are pinned here at
    their new home. Together the two tests assert everything the single
    pre-#1879 test asserted.
    """
    appendix = _appendix("intake")
    assert '"reason": "local_main_diverged_from_origin"' in appendix
    assert '"reason": "operator_unavailable"' in appendix
    assert '"reason": "pr_already_open"' in appendix


def test_plan_md_resolution_fields_preserved_verbatim() -> None:
    """Binding constraint (#1896/#1897 pre-flight): do not disturb the
    resolution_consumed/resolution_evidence sentinel fields while adding
    the new prose around them."""
    section = _plan_completion()
    for field in RESOLUTION_FIELDS:
        assert f'"{field}"' in section
    assert "resolution_evidence`" in section


# ---------------------------------------------------------------------------
# #2135 AC3: every EXIT in auto-dev-plan.md names a registered status/reason,
# and the code-side park-header set cannot drift from the prompt-side rule.
# ---------------------------------------------------------------------------

PLAN_EXIT_STATUSES = frozenset(
    {
        "ambiguities_pending_resolution",
        "blocked",
        "forbidden_area",
        "no_op",
        "plan_pending_approval",
        "premises_pending_verification",
        "scope_exceeded",
    }
)


def test_plan_doc_every_exit_names_a_status_and_registered_reason() -> None:
    """A new EXIT bullet, or an unregistered reason, must fail loud (#2135).

    The #2135 repro was a plan-stage park that left no sentinel at all. The
    producer-side prose already names an explicit status at every EXIT; what
    was missing is anything pinning that invariant, so an added EXIT could
    silently introduce a status or blocker reason cw does not know.
    """
    import re
    import typing

    from cw.auto_dev_result import KNOWN_BLOCKER_REASONS, Status

    plan_doc = _cmd("auto-dev-plan.md")
    statuses = set(re.findall(r"EXIT\s+`([a-z_]+)`", plan_doc))
    assert statuses == PLAN_EXIT_STATUSES
    known_statuses = set(typing.get_args(Status))
    assert statuses <= known_statuses

    reasons = set(re.findall(r'blocker\.reason: "([a-z_]+)"', plan_doc))
    assert reasons
    assert reasons <= set(KNOWN_BLOCKER_REASONS)

    blocked_exits = len(re.findall(r"EXIT\s+`blocked`", plan_doc))
    reasoned_blocked_exits = len(
        re.findall(r"EXIT\s+`blocked`\s+with\s+`blocker\.reason: ", plan_doc)
    )
    assert blocked_exits == reasoned_blocked_exits


# -- GitHub #2135: the park-comment stamp rule and its one wiring ------------

PARK_STAMP_COMMAND = "cw signal-park"
PARK_STAMP_RULE_REF = "*Park-comment stamp rule* in `.claude/commands/auto-dev.md`"
# The stage docs this ticket wires. An INVENTORY, deliberately not a
# completeness scan -- see test_park_stamp_is_wired_only_where_registered.
PARK_STAMP_WIRED_DOCS = frozenset(
    {
        "auto-dev-plan.md",
        "auto-dev-plan-appendix.md",
        "auto-dev-review.md",
        "auto-dev-review-appendix.md",
    }
)


def _paragraph_containing(content: str, anchor: str) -> str:
    """Return the whole blank-line-delimited paragraph *around* *anchor*.

    The file's ``_section`` slices anchor-to-anchor and
    ``test_prep_pr_ship_it_layouts._paragraph_at`` starts AT its anchor;
    neither can express "the paragraph this phrase sits inside", which is what
    the negative pins below need — a stamp clause bleeding into a neighbouring
    sentence of the same paragraph must fail them.
    """
    index = content.index(anchor)
    start = content.rfind("\n\n", 0, index)
    start = 0 if start == -1 else start + 2
    end = content.find("\n\n", index)
    return content[start : len(content) if end == -1 else end]


def _park_stamp_rule_section() -> str:
    return _section(
        _cmd("auto-dev.md"),
        "## Park-comment stamp rule (#2135)",
        "## Sentinel emit rule (#2382)",
    )


def test_park_stamp_rule_section_states_the_contract() -> None:
    """F1: the rule section is the worker-facing contract for the stamp."""
    rule = _park_stamp_rule_section()

    for phrase in (
        PARK_STAMP_COMMAND,
        "`park_comment_marker`",
        "`.claude/cw-context.json`",
        "recorded claim",
        "not an observation",
        "posted its park comment",
        "tracker-agnostic",
        "Linear",
        "carve-out from the Tool-Use Denial Exit",
        "is **not** a `tool_denied` exit",
        "a non-zero exit is ignored",
        "No such command",
        "version skew",
        "park marker NOT recorded",
        "emit the sentinel you were already about to emit, **unchanged**",
    ):
        assert phrase in rule, phrase

    for not_stamped in (
        "`no_op`",
        "`scope_exceeded`",
        "`forbidden_area`",
        "`stale_dispatch`",
        "`tool_denied`",
    ):
        assert not_stamped in rule, not_stamped

    assert "Accepted limitation" in rule
    assert "dies between deciding the exit and running the stamp" in rule


def test_park_stamp_rule_sits_after_the_tool_use_denial_exit() -> None:
    """F1: the carve-out must read after the rule it carves out of."""
    doc = _cmd("auto-dev.md")

    assert doc.index("## Tool-Use Denial Exit") < doc.index(
        "## Park-comment stamp rule (#2135)"
    )
    assert doc.index("## Park-comment stamp rule (#2135)") < doc.index(
        "## Guard Matrix"
    )


def test_denial_exit_carries_the_signal_park_exception() -> None:
    """F2: the Denial Exit itself names the exception, not only the rule below."""
    denial = _section(
        _cmd("auto-dev.md"),
        "## Tool-Use Denial Exit",
        "## Park-comment stamp rule (#2135)",
    )

    assert "**Exception (#2135)" in denial
    assert PARK_STAMP_COMMAND in denial


def test_gate_collapse_row_carries_the_signal_park_parenthetical() -> None:
    """F2: the reason-table row must not read as an unqualified tool_denied."""
    row = next(
        line
        for line in _cmd("auto-dev.md").splitlines()
        if "Tool call denied by auto-mode classifier" in line
    )

    assert PARK_STAMP_COMMAND in row


@pytest.mark.parametrize(
    ("doc", "anchor"),
    [
        pytest.param(
            "auto-dev-plan.md",
            "**THEN** post the same plan as a comment",
            id="plan-of-record-post",
        ),
        pytest.param(
            "auto-dev-plan.md",
            "Absent both, EXIT",
            id="no-evidence-re-park",
        ),
        pytest.param(
            "auto-dev-review.md",
            "It already carries its own",
            id="voided-review-findings",
        ),
        pytest.param(
            "auto-dev-finalize.md",
            "**Post to Linear:** Comment on the issue with PR link",
            id="finalize-pr-link",
        ),
        pytest.param(
            "auto-dev-review.md",
            "Clean/SHOULD_FIX + large → EXIT `review_pending_approval`",
            id="review-pending-approval",
        ),
        pytest.param(
            "auto-dev-review.md",
            "A Stage 3 pass whose diff against the base measures empty must exit"
            " `empty_diff_blocked`",
            id="empty-diff-blocked",
        ),
    ],
)
def test_allowlisted_posts_are_never_stamped(doc: str, anchor: str) -> None:
    """F3: a comment post that is not a park comment must not carry the stamp.

    The two auto-dev-plan.md anchors are live guards -- this ticket edits that
    doc -- and the review/finalize anchors guard the follow-up wiring (#2228).
    """
    assert PARK_STAMP_COMMAND not in _paragraph_containing(_cmd(doc), anchor)


def test_park_stamp_is_wired_only_where_registered() -> None:
    """F4: an INVENTORY pin, not a completeness scan.

    It proves this ticket wired exactly the plan stage's consolidated park and
    (per #2228) the review stage's blocking-findings and operator-actionable
    comment rules -- nothing else. It does NOT prove every park path is
    stamped: a new park path added to another stage doc is not caught, and it
    does not scan ``.claude/skills/**``, ``.claude/agents/**`` or sibling
    command docs such as ``prep-pr.md``. A future wiring ticket extends the
    registry in the same commit as its doc edit.
    """
    wired = {
        path.name
        for path in sorted(_COMMANDS_ROOT.glob("auto-dev*.md"))
        if path.name != "auto-dev.md" and PARK_STAMP_COMMAND in path.read_text("utf-8")
    }

    assert wired == set(PARK_STAMP_WIRED_DOCS)


def _consolidated_park_section() -> str:
    return _section(
        _appendix("plan"),
        "**Consolidated park (single-exit rule, #1650).**",
        "## Why an inline ambiguity scan",
    )


def test_consolidated_park_step_3a_stamps_after_the_post() -> None:
    """F5: the one wiring. Step 3a follows the comment post and precedes the
    draft-persistence step, so every consolidated-park exit shares one clause."""
    section = _consolidated_park_section()
    step_3a = _section(section, "   3a. **Park marker (#2135):**", "   4. Persist")

    assert PARK_STAMP_COMMAND in step_3a
    assert PARK_STAMP_RULE_REF in step_3a
    assert "once the step 3 comment has posted successfully" in step_3a
    assert "ignore that and emit the exit sentinel unchanged" in step_3a
    assert "Never run it for a `tool_denied` exit" in step_3a

    assert (
        section.index("   3. Post ONE comment")
        < section.index("   3a. **Park marker (#2135):**")
        < section.index("   4. Persist")
    )


def test_plan_core_pointer_names_the_stamp() -> None:
    """F5: the core doc's Step 1c pointer paragraph points at step 3a."""
    pointer = _paragraph_containing(
        _cmd("auto-dev-plan.md"), "**Consolidated park (single-exit rule, #1650).**"
    )

    assert PARK_STAMP_COMMAND in pointer
    assert PARK_STAMP_RULE_REF in pointer


# ---------------------------------------------------------------------------
# #2228: the review-stage wiring of the #2135 park-comment stamp into the
# blocking-findings and operator-actionable comment rules.
# ---------------------------------------------------------------------------


def _blocking_findings_rule_section() -> str:
    return _section(
        _appendix("review"),
        "**Blocking-findings comment rule (#1815).**",
        "**Third trigger (#1817):",
    )


def _operator_actionable_rule_section() -> str:
    return _section(
        _appendix("review"),
        "**Operator-actionable findings comment rule (#1817).**",
        "**Its trigger is `ADJUDICATIONS`",
    )


def test_review_blocking_findings_rule_stamps_after_the_post() -> None:
    """F5 (#2228): review_blocked / plan_deviation share one comment-posting
    rule, and this clause is the shared stamp both exits rely on."""
    section = _blocking_findings_rule_section()

    assert PARK_STAMP_COMMAND in section
    assert PARK_STAMP_RULE_REF in section
    assert "once this comment has posted successfully" in section
    assert "ignore that and emit the exit sentinel unchanged" in section
    assert "Never run it for a `tool_denied` exit" in section
    assert "review_blocked" in section
    assert "plan_deviation" in section


def test_review_operator_actionable_rule_stamps_after_the_post() -> None:
    """F5 (#2228): the operator-actionable checklist comment's stamp, plus the
    timing note that the exit itself fires later, at Step 3c."""
    section = _operator_actionable_rule_section()

    assert PARK_STAMP_COMMAND in section
    assert PARK_STAMP_RULE_REF in section
    assert "once this checklist comment has posted successfully" in section
    assert "Never run it for a `tool_denied` exit" in section
    assert "Timing note" in section
    assert "does not fire until Step 3c" in section
    assert "Stamp now, exit later" in section


def test_review_core_blocking_pointer_names_the_stamp() -> None:
    """F5 (#2228): the core doc's Checkpoint 3a pointer to the blocking-
    findings comment rule names the stamp that follows the post."""
    pointer = _paragraph_containing(
        _cmd("auto-dev-review.md"),
        "Blocking-findings comment rule: header, body shape, and the three triggers",
    )

    assert PARK_STAMP_COMMAND in pointer
    assert PARK_STAMP_RULE_REF in pointer


def test_review_core_operator_actionable_pointer_names_the_stamp() -> None:
    """F5 (#2228): the core doc's pointer to the operator-actionable comment
    rule names the stamp that follows the post."""
    pointer = _paragraph_containing(
        _cmd("auto-dev-review.md"),
        "Operator-actionable findings comment rule: header, checklist format,"
        " and trigger",
    )

    assert PARK_STAMP_COMMAND in pointer
    assert PARK_STAMP_RULE_REF in pointer


def test_review_plan_deviation_exit_rule_names_the_stamp_inline() -> None:
    """F5 (#2228): the 4a Exit rule is inline prose, not a deferral -- the
    stamp reference must live directly in its own sentence."""
    exit_rule = _paragraph_containing(
        _cmd("auto-dev-review.md"), "Once that comment has posted successfully"
    )

    assert PARK_STAMP_COMMAND in exit_rule
    assert PARK_STAMP_RULE_REF in exit_rule
    assert '"plan_deviation"' in exit_rule


def test_review_operator_action_override_notes_the_prior_stamp() -> None:
    """F5 (#2228): Step 3c's override does not re-run the stamp -- it only
    needs the pin to see the reference to where it already ran."""
    override = _paragraph_containing(
        _cmd("auto-dev-review.md"),
        "The checklist comment already posted per the operator-actionable"
        " findings comment rule at Checkpoint 3a",
    )

    assert PARK_STAMP_COMMAND in override
    assert PARK_STAMP_RULE_REF in override
    assert "already ran at Checkpoint 3a" in override
    assert "does not run it again" in override
