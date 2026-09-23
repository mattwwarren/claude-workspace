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
    """Interactive mode keeps the background/poll path (regression guard)."""
    section = _step7_section()
    assert "switch to background" in section
    assert "run_in_background: true" in section
    assert "gate-elapsed" in section
    assert "output_file" in section
    assert "poll_ceiling_s" in section
    interactive_at = section.index("**Interactive", section.index("**Headless:**"))
    assert section.index("switch to background") > interactive_at
