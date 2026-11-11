"""Guard tests: reviewers see the plan's scope, adjudication cross-checks it
(#2101).

Pure-markdown assertions over ``auto-dev-review.md``. Mirrors the
``read_text()`` + literal-substring/window convention of
``test_plan_persistence.py``. ``_cmd`` is imported from ``tests.conftest``
(#1787); ``_after`` is imported from ``test_auto_dev_preflight_resolutions``
rather than duplicated, since that file already defines and exports it.

Background: a plan's ``## Decisions`` can explicitly exclude a file from a
ticket's scope, but reviewers never saw the plan's file set, and
consolidation never cross-checked a finding's file against it — so a
reviewer's MUST_FIX on an explicitly-excluded file could reach FIX NOW
unchallenged. This adds a ``## Planned File Set`` block to every reviewer
prompt, a mechanical ``in_plan_scope`` tag stamped by ``cw review
consolidate --plan``, and a Checkpoint 3a bucket-sort rule — (4d), inserted
between the existing (4b) spec-citation cross-check and (4c) operator
send-back cross-check — that routes an out-of-scope finding to DEFER instead
of FIX NOW.
"""

from tests._shared.workflow_text import _cmd
from tests.test_auto_dev_preflight_resolutions import _after

CONSOLIDATE_INVOCATION_ANCHOR = (
    'cw review consolidate --documents-from .cw/review-findings/ --base "$FORK_POINT"'
)


def _every_reviewer_prompt_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("**Every reviewer prompt must include:**")
    end = content.index("**Output contract.**")
    return content[start:end]


def _checkpoint3a_prepass_section() -> str:
    content = _cmd("auto-dev-review.md")
    start = content.index("**Non-deferrable pre-pass (run BEFORE bucket assignment):**")
    end = content.index("Adjudication assigns each finding a disposition.")
    return content[start:end]


# ---------------------------------------------------------------------------
# 1. The reviewer-prompt template carries the Planned File Set block.
# ---------------------------------------------------------------------------


def test_reviewer_prompt_template_carries_planned_file_set_bullet() -> None:
    section = _every_reviewer_prompt_section()
    assert "## Planned File Set" in section


def _planned_file_set_bullet() -> str:
    """The Planned File Set bullet only: up to the Decided Constraints bullet."""
    section = _every_reviewer_prompt_section()
    start = section.index("- **`## Planned File Set` block")
    end = section.index("- **`## Decided Constraints` block")
    return section[start:end]


def _decided_constraints_bullet() -> str:
    """The Decided Constraints bullet only: up to the PROJECT_RUBRICS bullet."""
    section = _every_reviewer_prompt_section()
    start = section.index("- **`## Decided Constraints` block")
    end = section.index("- **`PROJECT_RUBRICS` block**")
    return section[start:end]


def test_planned_file_set_bullet_inlines_files_modified_only() -> None:
    """#2984: the block carries the file inventory, not a Decisions filter."""
    bullet = _planned_file_set_bullet()
    assert "## Files Modified" in bullet
    # The file-exclusion filter is the root cause #2984 removes: it dropped
    # behavioural decisions and let reviewers re-litigate them.
    assert "explicitly exclude or defer a file" not in bullet
    assert "<verbatim matching lines" not in bullet
    assert "## Adopted Assumptions" not in bullet


def test_planned_file_set_bullet_caps_out_of_scope_severity_at_should_fix() -> None:
    section = _every_reviewer_prompt_section()
    window = _after(section, "## Planned File Set", span=1400)
    assert "SHOULD_FIX" in window
    assert "MUST_FIX" in window
    assert "consequence" in window
    assert "coordinating session adjudicates" in window
    assert "not explicitly named as in scope by a decided constraint below" in window


def test_decided_constraints_bullet_is_in_every_reviewer_prompt() -> None:
    """#2984: the new block names the sources and the contest marker."""
    section = _every_reviewer_prompt_section()
    assert "## Decided Constraints" in section
    bullet = _decided_constraints_bullet()
    assert "## Decisions" in bullet
    assert "## Adopted Assumptions" in bullet
    assert "contests_decision" in bullet
    assert "cw review decided" in bullet


def test_decided_constraints_bullet_is_not_gated_on_plan_md() -> None:
    """It also carries `cw review constrain` entries, so an adopted row gets it."""
    bullet = _decided_constraints_bullet()
    assert "always, including an adopted row with no plan" in bullet
    assert "only when `.cw/plan.md` exists" not in bullet


def test_orientation_does_not_record_plan_decisions() -> None:
    """#2984: the review stage never records plan decisions."""
    content = _cmd("auto-dev-review.md")
    assert "record-plan-decisions" not in content


def test_output_contract_lists_contests_decision() -> None:
    content = _cmd("auto-dev-review.md")
    start = content.index("**Output contract.**")
    end = content.index("### Checkpoint 3a")
    contract = content[start:end]
    assert '"contests_decision": ""' in contract
    assert "Leave it empty on every other finding" in contract


# ---------------------------------------------------------------------------
# 2. (4d) exists between (4b) and (4c), and states the 4a precedence once.
# ---------------------------------------------------------------------------


def test_4d_sits_between_4b_and_4c() -> None:
    section = _checkpoint3a_prepass_section()
    b_idx = section.index("**(4b) Spec-citation cross-check.**")
    d_idx = section.index("**(4d) Plan-scope precedence")
    c_idx = section.index("**(4c) Operator send-back cross-check")
    assert b_idx < d_idx < c_idx


def test_4d_routes_out_of_scope_finding_to_defer_not_fix_now() -> None:
    section = _checkpoint3a_prepass_section()
    window = _after(section, "**(4d) Plan-scope precedence", span=1600)
    assert "in_plan_scope: false" in window
    assert "never eligible for bucket 1 (FIX NOW)" in window
    assert "DEFER (bucket 3" in window


def test_4d_carve_out_for_decisions_naming_file_in_scope() -> None:
    section = _checkpoint3a_prepass_section()
    window = _after(section, "**(4d) Plan-scope precedence", span=1600)
    assert "unless" in window
    assert "## Decisions" in window
    assert "bucket-sort the finding normally" in window


def test_4d_requires_rationale_naming_the_excluding_plan_clause() -> None:
    section = _checkpoint3a_prepass_section()
    window = _after(section, "**(4d) Plan-scope precedence", span=1600)
    assert "rationale must name the plan clause" in window
    assert "follow-up ticket" in window


def test_4d_states_4a_precedence_once() -> None:
    """A finding both NON_DEFERRABLE and out of scope resolves via the 4a
    Exit rule (plan_deviation), not by silently picking one signal."""
    section = _checkpoint3a_prepass_section()
    window = _after(section, "**(4d) Plan-scope precedence", span=1600)
    assert "NON_DEFERRABLE" in window
    assert "plan contradicts itself" in window
    assert "plan_deviation" in window


def test_4d_null_in_plan_scope_carries_no_signal() -> None:
    section = _checkpoint3a_prepass_section()
    window = _after(section, "**(4d) Plan-scope precedence", span=1600)
    assert "null" in window
    assert "no plan was supplied" in window


# ---------------------------------------------------------------------------
# 2b. (4e) Decided-constraint contest (#2984) sits after (4c) and routes a
#     contested decision to DEFER, never FIX or REJECT.
# ---------------------------------------------------------------------------


def test_4e_sits_after_4c() -> None:
    section = _checkpoint3a_prepass_section()
    c_idx = section.index("**(4c) Operator send-back cross-check")
    e_idx = section.index("**(4e) Decided-constraint contest")
    assert c_idx < e_idx


def test_4e_records_defer_and_forbids_fix_reject_and_operator_action() -> None:
    section = _checkpoint3a_prepass_section()
    window = _after(section, "**(4e) Decided-constraint contest", span=2200)
    assert "contests_decision" in window
    assert '`outcome: "defer"`' in window
    assert "never FIX NOW and never REJECT" in window
    assert 'Do NOT record `outcome: "operator_action"`' in window
    assert "review_operator_actionable" in window


def test_4e_states_4a_precedence_once() -> None:
    section = _checkpoint3a_prepass_section()
    window = _after(section, "**(4e) Decided-constraint contest", span=2200)
    assert "Precedence with (4a), stated once" in window
    assert "NON_DEFERRABLE" in window
    assert "plan_deviation" in window


def test_step3c_operator_action_override_has_contest_trigger() -> None:
    content = _cmd("auto-dev-review.md")
    window = _after(content, "**Operator-action override (#1817)", span=2200)
    assert "contests_decision" in window
    assert "operator_actionable" in window
    assert "#1817 ordering" in window


def test_appendix_trigger_paragraph_mentions_4e_contest() -> None:
    appendix = _cmd("auto-dev-review-appendix.md")
    window = _after(appendix, "**Its trigger is `ADJUDICATIONS`", span=1800)
    assert "(4e)" in window
    assert "cw review settle" in window


# ---------------------------------------------------------------------------
# 3. The consolidate invocation passes --plan .cw/plan.md.
# ---------------------------------------------------------------------------


def test_consolidate_invocation_passes_plan_flag() -> None:
    content = _cmd("auto-dev-review.md")
    full_line = _after(content, CONSOLIDATE_INVOCATION_ANCHOR, span=120)
    # #2943: the flag is built into PLAN_ARGS only when .cw/plan.md exists
    # (an adopted row has no plan), so the invocation expands the array.
    assert '"${PLAN_ARGS[@]}"' in full_line
    assert "PLAN_ARGS=(--plan .cw/plan.md)" in content


def test_consolidate_invocation_appears_exactly_once() -> None:
    content = _cmd("auto-dev-review.md")
    assert content.count(CONSOLIDATE_INVOCATION_ANCHOR) == 1
