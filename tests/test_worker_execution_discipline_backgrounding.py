"""Doc-structure guards for #2157: headless workers/orchestrator must never
background a pipeline-dependent Bash call and end their turn "waiting for the
notification" — there is no Stop-hook `background_tasks` tracking for a raw
Bash call the way there is for an Agent-tool subagent spawn, so that turn
never resumes.

Pins the Worker Execution Discipline broadening (binds the orchestrator's own
direct Bash calls, not only spawned-agent prompts), the two concrete gap
closures (Mitigation 1's gate test-command, Step 1g's tracker-comment post),
and the disambiguation sentence added to every existing Async-dispatch note.
"""

from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after


def _worker_execution_discipline_section() -> str:
    content = _cmd("auto-dev.md")
    start = content.index("## Worker Execution Discipline")
    end = content.index("## Comment provenance rule")
    return content[start:end]


def test_worker_execution_discipline_binds_orchestrator_not_just_agent_prompts() -> (
    None
):
    """The section must state it also binds the orchestrator's own direct Bash
    calls, not only text injected into spawned-agent prompts — the #2157
    incidents both happened in orchestrator-run Bash calls (a gate test
    command, a tracker-comment post), which the pre-#2157 "every agent
    prompt" framing did not cover."""
    section = _worker_execution_discipline_section()
    assert "orchestrator's own direct Bash calls" in section


def test_worker_execution_discipline_forbids_backgrounding_pipeline_bash() -> None:
    """The section must forbid `run_in_background` / a harness-offered
    background continuation for any Bash call the pipeline depends on, and
    name the reason: no Stop-hook `background_tasks` tracking for a raw Bash
    call, unlike an Agent-tool subagent spawn."""
    section = _worker_execution_discipline_section()
    assert "run_in_background" in section
    assert "background continuation" in section
    assert "background_tasks" in section
    assert "Agent tool" in section or "Agent-tool" in section


def test_async_dispatch_notes_disambiguate_agent_tool_scope() -> None:
    """Every existing Async-dispatch note must be followed, within a bounded
    window, by a sentence scoping "ending the turn is safe" to Agent-tool
    subagent spawns only, pointing back at Worker Execution Discipline for
    the raw-Bash rule."""
    sites = [
        (_cmd("auto-dev-plan.md"), "**Async dispatch note (verified 2026-08-19).**"),
        (_cmd("auto-dev-impl.md"), "**Async dispatch note (verified 2026-08-19).**"),
        (
            _appendix("impl"),
            "## Async dispatch: why never to busy-wait on the impl agent",
        ),
        (_cmd("auto-dev-review.md"), "**Async dispatch note (verified 2026-08-19).**"),
        (
            _appendix("review"),
            "## Parent turns and subagent turns are not symmetric",
        ),
        (_cmd("auto-dev-finalize.md"), "Agent spawns are async unconditionally"),
    ]
    for content, anchor in sites:
        window = _after(content, anchor, span=1800)
        assert "Agent tool's subagent spawn only" in window, anchor
        assert "Worker Execution Discipline" in window, anchor


def test_gate_test_command_never_backgrounds() -> None:
    """auto-dev.md's Mitigation 1 Stage 2 gate block (the `<test_command>
    --tb=short` line) must be wrapped in an explicit `timeout` and instruct
    never accepting a background continuation for this call."""
    content = _cmd("auto-dev.md")
    window = _after(content, 'cd "$TMPWT" && ', span=800)
    assert "timeout" in window
    assert "never accept a background continuation" in window
    assert "IMPL_FAILED" in window


def test_plan_step1g_tracker_post_never_backgrounds() -> None:
    """auto-dev-plan.md's Step 1g tracker-comment-post instructions must
    include the same foreground/timeout/no-backgrounding language."""
    content = _cmd("auto-dev-plan.md")
    window = _after(content, "**THEN** post the same plan as a comment", span=1200)
    assert "timeout" in window
    assert "never" in window.lower()
    assert "background" in window.lower()
