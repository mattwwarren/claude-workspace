"""Guard tests (#1890): every headless stage doc that terminates a session
must carry both (a) frame-emission discipline ("validating is not
emitting") and (b) the no-interactive-escalation warning ("no listener,
never ask a question") in its own Stage-N-Completion section — not only in
the chained-path Appendix of auto-dev.md.

Forensics (2026-08-16 dead-flat sessions, #1886): #1833 validated a sentinel
and narrated "Emitting the final result" without ever emitting the literal
frame; #1750 detected a real blocker and escalated it as an interactive
question in a headless session with no listener instead of the `blocked`
sentinel. Both gaps are model-adherence, not schema — this pins the prose
that closes them.
"""

from tests.conftest import _appendix, _cmd

FRAME_DISCIPLINE_ANCHOR = "Validating is not emitting"
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
