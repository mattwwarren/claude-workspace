"""Doc-structure guards for the per-gate Completion Artifacts contract (#1788).

The Completion Artifacts block that Stage 2 impl agents, the Stage 3 fix
loop, and the narrative Mitigation-2 copy all reference used to collapse
every configured quality gate into two ambiguous fields ("Mypy result",
"Ruff result"). That silently dropped gates a client actually configured via
``quality_gate_commands`` (e.g. ``ruff check`` and ``ruff format --check``
are two separate gates, not one) and gave no way to distinguish "gate not
run" from "gate passed."

This block is pure prose consumed by the orchestrating LLM session itself —
there is no Python parser for it (see `tests/test_scope_conformance_gate_docs.py`
for the sibling doc-guard pattern this file follows). These tests pin the
per-gate reporting contract across the three `.claude/commands/*.md` sites so
the two-field collapse cannot silently creep back in.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS = _REPO_ROOT / ".claude" / "commands"


# NOTE: this is another local copy of the `_cmd(name)` helper that
# tests/test_scope_conformance_gate_docs.py, tests/test_auto_dev_model_pins.py,
# and tests/test_consolidated_park.py each already carry. Consolidating the
# duplicated helper into conftest.py is deliberately NOT attempted here — it
# is tracked separately as #1787.
def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text(encoding="utf-8")


def test_impl_completion_artifacts_reports_one_row_per_gate() -> None:
    """The impl.md block must no longer collapse gates into two bare fields,
    and must require ruff's two distinct commands be reported separately."""
    content = _cmd("auto-dev-impl.md")
    assert "one row per gate command" in content
    assert "ruff check" in content
    assert "ruff format --check" in content
    assert "**Mypy result (if Python touched):**" not in content
    assert "**Ruff result (if Python touched):**" not in content


def test_impl_completion_artifacts_defines_not_run_status() -> None:
    """A gate the agent did not run must be reportable as its own status,
    distinct from `pass`."""
    content = _cmd("auto-dev-impl.md")
    assert "not_run" in content
    assert "pass" in content


def test_impl_completion_artifacts_requires_every_configured_gate_reported() -> (
    None
):
    """Omitting a row for a configured gate must be a discipline failure, not
    a silently-accepted "done" claim — same `impl_failed` disposition as a
    contradicted artifact."""
    content = _cmd("auto-dev-impl.md")
    assert "missing a row for any gate" in content
    assert "same `impl_failed` disposition" in content


def test_auto_dev_and_impl_completion_artifacts_stay_in_sync() -> None:
    """auto-dev.md's narrative Mitigation-2 copy and auto-dev-impl.md's
    canonical block need not be byte-identical (they already aren't today),
    but the load-bearing per-gate substrings must be present in both so the
    two-field collapse cannot creep back into just one copy."""
    for name in ("auto-dev.md", "auto-dev-impl.md"):
        content = _cmd(name)
        assert "not_run" in content, f"{name} is missing the not_run status"
        assert "ruff check" in content, f"{name} is missing 'ruff check'"
        assert "ruff format --check" in content, (
            f"{name} is missing 'ruff format --check'"
        )


def test_review_fix_loop_references_per_gate_contract() -> None:
    """The fix-loop spawn section must no longer point at the collapsed
    'mypy/ruff results' field pair — it must reference the per-gate
    contract so a reader doesn't reintroduce the two-field collapse here."""
    content = _cmd("auto-dev-review.md")
    assert "mypy/ruff results" not in content
    assert "per-gate" in content
