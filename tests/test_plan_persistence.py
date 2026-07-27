"""Guard tests: persist plan drafts across blocked headless attempts (#1510).

Pure-markdown assertions over the auto-dev pipeline instruction files. Mirrors
the ``read_text()`` + literal-substring/window convention of
``test_auto_dev_preflight_resolutions.py`` / ``test_plan_format_only_findings.py``.
``_cmd`` is duplicated locally per the established convention (no shared
module for it — confirmed in both ``test_auto_dev_preflight_resolutions.py``
and ``test_plan_format_only_findings.py``); ``_after``/``_nearby`` are
imported from ``test_auto_dev_preflight_resolutions`` rather than duplicated,
since that file already defines and exports them.

Background: when the headless Stage-1 plan pipeline exits `blocked` with
`plan_unreviewable` or `plan_unsound` after a Step 1f.3 MUST_FIX-persists-
after-1-cycle, the in-progress (not-yet-approved) plan text used to be
discarded entirely. This adds a new local persistence artifact,
`.cw/plan-draft.md`, written on those three headless blocked exits, so a
subsequent retry attempt can resume from it (Step 1a) instead of
regenerating the plan from scratch. `.cw/plan.md` itself (the Stage-2
implementation contract) is never touched by this change — it remains
written only by Step 1g.
"""

from pathlib import Path

from tests.test_auto_dev_preflight_resolutions import _after, _nearby

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"

DRAFT_FILE = ".cw/plan-draft.md"
PLAN_FILE = ".cw/plan.md"

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


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


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


def _step1e_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1e: Pre-flight Already-Satisfied Check")
    end = content.index("### Step 1f:")
    return content[start:end]


def _step1f3_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("**Step 1f.3 — Gating:**")
    end = content.index("**Codify lessons")
    return content[start:end]


def _step1g_section() -> str:
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Step 1g: Persist Plan + Post to Linear")
    end = content.index("## Stage 1 Completion")
    return content[start:end]


# ---------------------------------------------------------------------------
# 1. Draft filename in all three Step 1f.3 headless blocked-exit clauses.
# ---------------------------------------------------------------------------


def test_step1f3_all_three_blocked_exits_write_draft() -> None:
    """All 3 headless blocked-exit clauses instruct writing the draft file."""
    section = _step1f3_section()

    unreviewable_window = _after(section, UNREVIEWABLE_PERSISTS_ANCHOR, span=500)
    assert DRAFT_FILE in unreviewable_window
    assert "write the plan's current text" in unreviewable_window

    unsound_first_cycle_window = _after(section, UNSOUND_FIRST_CYCLE_ANCHOR, span=500)
    assert DRAFT_FILE in unsound_first_cycle_window
    assert "write the plan's current text" in unsound_first_cycle_window

    unsound_persists_window = _after(section, UNSOUND_PERSISTS_ANCHOR, span=500)
    assert DRAFT_FILE in unsound_persists_window
    assert "write the plan's current text" in unsound_persists_window


# ---------------------------------------------------------------------------
# 2. Draft filename in Step 1a's new read/resume instruction.
# ---------------------------------------------------------------------------


def test_step1a_resume_check_names_draft_file() -> None:
    """Step 1a's new resume-check bullet names the draft file and resumes."""
    section = _step1a_section()
    assert DRAFT_FILE in section
    assert "resuming, not regenerating" in section
    assert "skip Step 1b's generation entirely" in section


def test_step1a_resume_check_records_friction_highlights() -> None:
    """Resuming from a persisted draft is recorded in friction_highlights.

    `plan_source` stays the existing "generated" literal on a resume (no new
    enum value per the ticket's resolution), so friction_highlights is the
    only sentinel-visible signal that a plan was resumed rather than freshly
    generated.
    """
    section = _step1a_section()
    assert "friction_highlights" in section
    window = _after(section, "resuming, not regenerating", span=300)
    assert "friction_highlights" in window


# ---------------------------------------------------------------------------
# 3. Step 1a supersession-guard text: draft ignored whenever plan.md exists,
#    regardless of timestamps.
# ---------------------------------------------------------------------------


def test_step1a_supersession_guard_ignores_timestamps() -> None:
    """The ordering guard ignores the draft whenever plan.md exists, any timestamp."""
    section = _step1a_section()
    window = _after(section, "**Supersession/ordering guard:**", span=400)
    assert PLAN_FILE in window
    assert "already exists" in window
    assert f"ignore `{DRAFT_FILE}`" in window
    assert "regardless of either file's timestamp" in window


# ---------------------------------------------------------------------------
# 4. Checkpoint 1's widened auto-skip condition — one condition, two sources.
# ---------------------------------------------------------------------------


def test_checkpoint1_auto_skip_covers_both_sources_in_one_condition() -> None:
    """The headless auto-skip clause covers tracker-plan AND resumed-draft."""
    section = _checkpoint1_section()
    assert (
        f"if plan in Linear or resumed from `{DRAFT_FILE}` → AUTO-SKIP "
        "plan-approval question"
    ) in section
    # Exactly one auto-skip clause — not a duplicated parallel branch.
    assert section.count("AUTO-SKIP plan-approval question") == 1


# ---------------------------------------------------------------------------
# 5. Step 1g's best-effort deletion instruction, after the plan.md write.
# ---------------------------------------------------------------------------


def test_step1g_deletes_draft_after_successful_write() -> None:
    """Step 1g best-effort deletes the draft after plan.md is written."""
    section = _step1g_section()
    write_idx = section.index("Verify the write (`test -s .cw/plan.md`)")
    delete_idx = section.index(f"delete `{DRAFT_FILE}`")
    assert write_idx < delete_idx

    window = _nearby(section, f"delete `{DRAFT_FILE}`", span=200)
    assert "best-effort" in window
    assert "must NOT fail Step 1g" in section


# ---------------------------------------------------------------------------
# 6. Step 1e's no_op exit best-effort deletion instruction.
# ---------------------------------------------------------------------------


def test_step1e_no_op_deletes_draft() -> None:
    """The no_op exit best-effort deletes the draft on the way out."""
    section = _step1e_section()
    assert f"if `{DRAFT_FILE}` is present, delete it" in section
    assert "best-effort" in section
    assert "must not fail this exit" in section


# ---------------------------------------------------------------------------
# 7. plan.md is still written only by Step 1g — unchanged Stage-2 contract.
# ---------------------------------------------------------------------------


def test_plan_md_still_written_only_by_step1g() -> None:
    """No step other than Step 1g is made to write .cw/plan.md."""
    content = _cmd("auto-dev-plan.md")
    assert content.count("Write the full reviewed plan text verbatim") == 1
    # The write instruction lives inside Step 1g's own section.
    assert "Write the full reviewed plan text verbatim" in _step1g_section()
    # Step 1a's resume check only ever reads/checks existence, never writes.
    step1a = _step1a_section()
    assert "Write the full reviewed plan text" not in step1a


# ---------------------------------------------------------------------------
# 8. Supersession scenario present: both files present → draft ignored,
#    approved plan wins.
# ---------------------------------------------------------------------------


def test_supersession_scenario_documented() -> None:
    """Both files present → draft is ignored, the approved plan.md wins."""
    section = _step1a_section()
    assert f"an approved `{PLAN_FILE}` always wins over a stale draft" in section


# ---------------------------------------------------------------------------
# 9. no_op-clears-draft scenario present: no_op exit + pre-existing draft
#    leaves no draft behind.
# ---------------------------------------------------------------------------


def test_no_op_clears_draft_scenario_documented() -> None:
    """A pre-existing draft must not survive a no_op exit."""
    section = _step1e_section()
    assert "must not survive a `no_op` exit" in section


# ---------------------------------------------------------------------------
# 10. Blocked-exit clauses do NOT carry a deletion instruction (the
#     deliberate write-and-resume exception).
# ---------------------------------------------------------------------------


def test_blocked_exits_do_not_delete_draft() -> None:
    """The 3 headless blocked-exit clauses write the draft but never delete it."""
    section = _step1f3_section()

    unreviewable_window = _after(section, UNREVIEWABLE_PERSISTS_ANCHOR, span=500)
    assert "delete" not in unreviewable_window.lower()

    unsound_first_cycle_window = _after(section, UNSOUND_FIRST_CYCLE_ANCHOR, span=500)
    assert "delete" not in unsound_first_cycle_window.lower()

    unsound_persists_window = _after(section, UNSOUND_PERSISTS_ANCHOR, span=500)
    assert "delete" not in unsound_persists_window.lower()
