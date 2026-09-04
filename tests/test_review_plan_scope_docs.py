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

from tests.conftest import _cmd
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


def test_planned_file_set_bullet_inlines_files_modified_and_decisions() -> None:
    section = _every_reviewer_prompt_section()
    window = _after(section, "## Planned File Set", span=800)
    assert "## Files Modified" in window
    assert "## Decisions" in window
    assert "## Adopted Assumptions" in window


def test_planned_file_set_bullet_caps_out_of_scope_severity_at_should_fix() -> None:
    section = _every_reviewer_prompt_section()
    window = _after(section, "## Planned File Set", span=1400)
    assert "SHOULD_FIX" in window
    assert "MUST_FIX" in window
    assert "consequence" in window
    assert "coordinating session adjudicates" in window


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
# 3. The consolidate invocation passes --plan .cw/plan.md.
# ---------------------------------------------------------------------------


def test_consolidate_invocation_passes_plan_flag() -> None:
    content = _cmd("auto-dev-review.md")
    full_line = _after(content, CONSOLIDATE_INVOCATION_ANCHOR, span=120)
    assert "--plan .cw/plan.md" in full_line


def test_consolidate_invocation_appears_exactly_once() -> None:
    content = _cmd("auto-dev-review.md")
    assert content.count(CONSOLIDATE_INVOCATION_ANCHOR) == 1
