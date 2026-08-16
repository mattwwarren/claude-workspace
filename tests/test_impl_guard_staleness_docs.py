"""Doc-structure guards for the impl Pre-Stage Detector Guard staleness/regress
override (#1794) — mirrors tests/test_scope_conformance_gate_docs.py's pairing
of a script-behavior test file with a prose-wiring test file.
"""

from pathlib import Path

from tests.test_auto_dev_preflight_resolutions import _after

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ROOT / ".claude" / "commands"


# NOTE: another local copy of the `_cmd(name)` helper that
# tests/test_scope_conformance_gate_docs.py and ~9 other test files already
# carry. Consolidating it into conftest.py is deliberately NOT attempted here —
# it is tracked separately as #1787.
def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text(encoding="utf-8")


def _guard_section() -> str:
    content = _cmd("auto-dev-impl.md")
    start = content.index("### Pre-Stage Detector Guard")
    end = content.index("**Headless only — before spawning Stage 2 agent")
    return content[start:end]


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


def test_guard_rematerializes_context_json() -> None:
    content = _cmd("auto-dev-impl.md")
    window = _after(content, "**Comments are live, not cached", span=1400)
    assert "overwrite `.cw/context.json`" in window
    assert "materialized_by_session" in window
