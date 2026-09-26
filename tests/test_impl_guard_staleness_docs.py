"""Doc-structure guards for the impl Pre-Stage Detector Guard staleness/regress
override (#1794) — mirrors tests/test_scope_conformance_gate_docs.py's pairing
of a script-behavior test file with a prose-wiring test file.
"""

from cw.auto_dev_result import IMPL_COMMENTS_UNREADABLE_AFTER_REGRESS_BLOCKER_REASON
from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after


def _guard_section() -> str:
    """The detector guard's resume dispositions + staleness/regress check.

    #1879 relocated this block to ``auto-dev-impl-appendix.md``: it applies
    only when ``detect_current_stage()`` reports that the ticket already
    carries branch work, which a fresh dispatch never does. The core doc keeps
    the detector call and the trigger sentence; every assertion below follows
    the content to its new home rather than being dropped.
    """
    content = _appendix("impl")
    start = content.index("## Pre-Stage Detector Guard: resume dispositions")
    end = content.index("\n## ", start)
    return content[start:end]


def test_core_doc_keeps_detector_call_and_appendix_trigger() -> None:
    """Detection stays on the common path; only the resume branch moved."""
    content = _cmd("auto-dev-impl.md")
    assert "run `detect_current_stage()`" in content
    assert "Pre-Stage Detector Guard: resume dispositions" in content
    assert "never re-implement over existing branch work by default" in content


def test_guard_invokes_staleness_script() -> None:
    section = _guard_section()
    assert ".claude/scripts/check_impl_guard_staleness.py" in section
    assert "--head-commit-at" in section
    assert "--comments-file" in section
    assert "--regressed-into-stage" in section


def test_guard_reads_regressed_into_stage_from_queue_metadata() -> None:
    section = _guard_section()
    assert "queue_metadata.regressed_into_stage" in section


def test_guard_past_s2_common_case_still_short_circuits() -> None:
    """Inverse of the bug: no new evidence -> unchanged fast path (AC3)."""
    section = _guard_section()
    assert "advance to that stage's entry point; do not re-implement" in section


def test_guard_stale_branch_resumes_instead_of_advancing() -> None:
    section = _guard_section()
    assert "the trailer's premise" in section
    assert "do NOT advance to the next stage's entry point" in section
    assert "Resume from current branch HEAD; do not reset" in section


def test_guard_stale_branch_requires_fresh_trailer() -> None:
    section = _guard_section()
    assert "must append a fresh `Auto-Dev-Stage: impl-complete` trailer" in section


def test_guard_stale_comments_delivered_as_binding_instructions() -> None:
    """AC4 (R4): pins the exact clause that hands live-fetched comments to the
    Stage 2 agent as binding -- not merely "a fetch was attempted"."""
    section = _guard_section()
    assert "as new, binding instructions to read and act on" in section


def test_guard_known_limitation_cites_1801() -> None:
    """#1801 evaluated and accepted the no-sentinel-death gap this sentence
    describes -- the prose cross-references the ticket that made that call."""
    section = _guard_section()
    assert "Known limitation" in section
    assert "#1801" in section


def test_guard_fails_open_on_script_exit_2() -> None:
    """A malformed input must not block the pipeline — exit 2 degrades to the
    unchanged short-circuit behaviour, with a friction breadcrumb."""
    section = _guard_section()
    assert "impl_guard_staleness_check_failed" in section
    assert "fail open" in section


def test_orientation_live_fetches_comments_not_cache() -> None:
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=900)
    assert "MUST live-fetch the ticket comments on every invocation" in window
    assert "Stage 0 does NOT re-run between pipeline stages" in window


def test_orientation_cites_per_stage_dispatch_mechanism() -> None:
    content = _cmd("auto-dev-impl.md")
    assert "src/cw/executor.py" in content


def test_guard_resolves_repo_local_then_global_script_path() -> None:
    """Repo-local first, then the copy install-skills.sh ships (#2141).

    The repo-local candidate is asserted in its ``$GUARD_ROOT``-anchored
    spelling, not as a bare substring: a cwd-relative probe would satisfy the
    looser assertion while silently missing the repo-local copy whenever the
    cwd is not the worktree root (review round 2).
    """
    section = _guard_section()
    assert '"$GUARD_ROOT/.claude/scripts/check_impl_guard_staleness.py"' in section
    assert '"$HOME/.claude/scripts/check_impl_guard_staleness.py"' in section
    assert "for candidate in .claude/scripts/" not in section


def test_guard_absent_from_both_locations_skips_non_blocking() -> None:
    """Absent from both locations keeps the fail-open short-circuit (#2141).

    Distinct from the pre-existing exit-2 (``impl_guard_staleness_check_failed``)
    branch, which is unchanged: a missing file coincidentally exits 2 too, but
    it is not an unparseable timestamp and must not be labelled as one.
    """
    section = _guard_section()
    assert "check_impl_guard_staleness: script absent, skipped" in section
    assert "impl_guard_staleness_check_failed" in section


def test_guard_greps_cw_script_version_marker_and_headless_blocks_on_stale() -> None:
    """File-staleness is a hard stop, and is NOT the script's own `stale` field.

    The resolver's marker check (is this copy of the script current?) precedes
    and is independent of the script's ``stale: true/false`` JSON verdict (are
    the impl comments newer than HEAD?). The two must not collapse into one
    concept: no HEADLESS BLOCK line may also carry the fail-open verdict.
    """
    section = _guard_section()
    assert "cw-script-version" in section
    assert "HEADLESS BLOCK" in section
    stale_lines = [line for line in section.splitlines() if "HEADLESS BLOCK" in line]
    assert stale_lines
    assert all("stale: false" not in line for line in stale_lines)


def test_guard_rematerializes_context_json() -> None:
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=1400)
    assert "overwrite `.cw/context.json`" in window
    assert "materialized_by_session" in window


def test_orientation_regressed_comments_fetch_failure_hard_blocks() -> None:
    """#2415: a comments-fetch failure on an IMPL entry reached via
    `_stage_regress` hard-blocks instead of the generic WARN-and-continue --
    a regress exists specifically to act on newer comments, so continuing on
    a stale cached array would defeat it. The non-regress WARN branch must
    survive unchanged alongside the new hard-block branch."""
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=2800)
    assert (
        f'blocker.reason: "{IMPL_COMMENTS_UNREADABLE_AFTER_REGRESS_BLOCKER_REASON}"'
        in window
    )
    assert "queue_metadata.regressed_into_stage" in window
    assert "impl_comments_fetch_failed" in window
    assert "a stale-but-real array is better evidence than none" in window
