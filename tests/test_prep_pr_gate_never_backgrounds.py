"""Doc-structure guards for #2251: /prep-pr Step 7 must never background a
quality gate in headless mode.

Pre-#2251, Step 7 re-issued a gate that outran its foreground ceiling with
``run_in_background: true`` and waited for the harness's completion
notification. #2157 already established that a raw Bash background call has no
Stop-hook ``background_tasks`` tracking, so in a headless DAEMON session the
notification lands as an unconsumed ``queue-operation`` enqueue record and the
turn never resumes -- the finalize wedge. These guards pin the headless
override (fail on foreground timeout, cross-referencing auto-dev.md's Worker
Execution Discipline) and that interactive mode keeps its background/poll path.
"""

from tests.conftest import _cmd
from tests.test_auto_dev_preflight_resolutions import _after

_FOREGROUND_TIMEOUT_ANCHOR = "**If the foreground call itself times out"


def _step7_section() -> str:
    content = _cmd("prep-pr.md")
    start = content.index("## Step 7: Run Quality Gates")
    end = content.index("### Loop Control", start)
    return content[start:end]


def _headless_override_window() -> str:
    """Text of Step 7's headless foreground-timeout branch, up to the
    interactive branch that follows it."""
    section = _step7_section()
    trigger = section.index(_FOREGROUND_TIMEOUT_ANCHOR)
    start = section.index("**Headless:**", trigger)
    end = section.index("**Interactive", start)
    return section[start:end]


def _interactive_only_window() -> str:
    """Text of Step 7's interactive-only background/poll branch, scoped to
    exclude both the Step 7 preamble (whose gate-timeout paragraph mentions
    ``poll_ceiling_s`` for both modes) and the headless branch that precedes
    it (which mentions ``run_in_background: true`` too, as the thing it
    forbids) -- review round 1 (#2251): an unscoped ``phrase in section``
    check for either literal is satisfied by that earlier occurrence alone
    and proves nothing about whether the interactive branch itself survived.
    """
    section = _step7_section()
    headless_at = section.index("**Headless:**")
    start = section.index("**Interactive (no `--headless`):**", headless_at)
    end = section.index("\n5. If it", start)
    return section[start:end]


def test_prep_pr_step7_headless_forbids_backgrounding() -> None:
    """The foreground-timeout trigger must carry a headless override that
    forbids ``run_in_background`` and cites Worker Execution Discipline."""
    section = _step7_section()
    near_trigger = _after(section, _FOREGROUND_TIMEOUT_ANCHOR, span=400)
    assert "**Headless:**" in near_trigger

    window = _headless_override_window()
    assert "run_in_background" in window
    assert "Worker Execution Discipline" in window
    assert "background continuation" in window


def test_prep_pr_step7_headless_block_fires_without_background_switch() -> None:
    """In headless mode the ``gate_timeout`` block fires directly off the
    foreground timeout -- no background re-issue, no ``gate-elapsed`` poll."""
    window = _headless_override_window()
    assert "gate_timeout" in window
    assert "immediately" in window
    assert "Never" in window or "never" in window
    assert "Skip" in window
    assert "gate-elapsed" not in window


def test_prep_pr_step7_interactive_backgrounding_still_intact() -> None:
    """Interactive mode keeps the background/poll path (regression guard),
    scoped strictly to the interactive-only window -- not the Step 7 preamble
    or the headless branch, either of which can independently satisfy an
    unscoped ``phrase in section`` check (review round 1, #2251)."""
    window = _interactive_only_window()
    assert "switch to background" in window
    assert "run_in_background: true" in window
    assert "gate-elapsed" in window
    assert "output_file" in window
    assert "poll_ceiling_s" in window


def test_prep_pr_step7_interactive_window_excludes_headless_and_preamble() -> None:
    """Mutation guard (#2251 review round 1): ``poll_ceiling_s`` (Step 7's own
    gate-timeout preamble, shared by both modes) and ``run_in_background:
    true`` (the headless branch's own forbidding text) both occur BEFORE the
    interactive-only window starts. An assertion against the whole Step 7
    section, unscoped, would pass on either earlier occurrence alone even if
    the interactive branch itself were deleted -- proving the regression test
    above is not vacuously satisfied."""
    section = _step7_section()
    window = _interactive_only_window()
    before_window = section[: section.index(window)]
    assert "poll_ceiling_s" in before_window
    assert "run_in_background: true" in before_window


# --- #2758: gate-pass skip and the configurable ceiling ---------------------

_STAMP_CHECK_CALL = '"$PREP_PR_STATE" gate-pass check'
_STAMP_RECORD_CALL = '"$PREP_PR_STATE" gate-pass record'
_RECORD_ANCHOR = "**Recording a pass.**"


def _between(text: str, start_anchor: str, end_anchor: str) -> str:
    start = text.index(start_anchor)
    return text[start : text.index(end_anchor, start)]


def _check_paragraph() -> str:
    return _between(
        _step7_section(), "For each gate, first check", "Before running each gate"
    )


def _record_paragraph() -> str:
    section = _step7_section()
    return section[section.index(_RECORD_ANCHOR) :]


def test_prep_pr_step7_gate_pass_order() -> None:
    """Check first (before the ceiling fetch), record only after the gate ran."""
    section = _step7_section()
    check = section.index(_STAMP_CHECK_CALL)
    timeout = section.index('"$PREP_PR_STATE" gate-timeout')
    for_each = section.index("For each gate:")
    record = section.index(_STAMP_RECORD_CALL)
    assert check < timeout < for_each < record


def test_prep_pr_step7_gate_pass_paragraphs_never_background() -> None:
    """The skip path is a plain foreground call: no background, no poll."""
    for paragraph in (_check_paragraph(), _record_paragraph()):
        assert "run_in_background" not in paragraph
        assert "gate-elapsed" not in paragraph
    assert "do not pass the command" in _check_paragraph()


def test_prep_pr_step7_headless_window_has_no_gate_pass() -> None:
    """R2 guard: the #2251 headless override text is untouched by #2758."""
    assert "gate-pass" not in _headless_override_window()


def test_prep_pr_step7_gate_timeout_paragraph_explains_clamp() -> None:
    paragraph = _between(
        _step7_section(), "Pass `<gate-name>` exactly", "For each gate:"
    )
    assert "clamped" in paragraph
    assert "quality_gates.timeouts" in paragraph


def test_prep_pr_step10_reads_gate_skips_before_clean() -> None:
    content = _cmd("prep-pr.md")
    step10 = _between(content, "## Step 10: Clean Up", "\n## ")
    status = step10.index('"$PREP_PR_STATE" status')
    assert status < step10.index('"$PREP_PR_STATE" clean')
    assert "gate_skips" in step10
