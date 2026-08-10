"""Doc-structure guards for the post-impl scope-conformance gate (#1779).

Pins the prose wiring that makes the Step 2.5 gate real: the impl command must
invoke the script, the collapse tables must distinguish the blocking drift exit
from the pre-existing non-blocking growth note, and the plan command must
require the ``## Files Modified`` heading the parser anchors on.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS = _REPO_ROOT / ".claude" / "commands"


# NOTE: this is another local copy of the `_cmd(name)` helper that
# tests/test_auto_dev_model_pins.py and tests/test_consolidated_park.py each
# already carry. Consolidating the duplicated helper into conftest.py is
# deliberately NOT attempted here — it is tracked separately as #1787.
def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text(encoding="utf-8")


def _doc(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


def test_impl_step2_5_gate2_invokes_scope_conformance_script() -> None:
    """Step 2.5 gate 2 must call the mechanical gate, not eyeball the file set."""
    content = _cmd("auto-dev-impl.md")
    assert ".claude/scripts/check_plan_scope_conformance.py" in content
    assert "--touched-files" in content


def test_impl_step2_5_gate2_blocks_with_plan_scope_drift_reason() -> None:
    """Exit 1 from the gate script must map to the new blocker reason."""
    content = _cmd("auto-dev-impl.md")
    assert 'blocker.reason: "plan_scope_drift"' in content
    assert '"stage2_impl"' in content


def test_impl_step2_5_below_threshold_still_uses_impl_scope_growth_friction() -> None:
    """Regression guard: the non-blocking within-allowance path must survive."""
    content = _cmd("auto-dev-impl.md")
    assert "impl_scope_growth" in content


def test_impl_step2_5_populates_lines_actual_on_the_drift_exit() -> None:
    """stage_reached=stage2_impl requires a non-null scope.lines_actual."""
    content = _cmd("auto-dev-impl.md")
    assert "scope.lines_actual" in content


def test_gate_collapse_table_distinguishes_drift_from_growth() -> None:
    """auto-dev.md's Gate-Collapse Table needs both rows, not one merged row."""
    content = _cmd("auto-dev.md")
    assert "S2.5 files outside plan, within threshold" in content
    assert "S2.5 files outside plan, threshold exceeded" in content
    assert '"impl_scope_growth: <files>"' in content
    assert 'blocker.reason: "plan_scope_drift"' in content


def test_blocker_reason_table_documents_plan_scope_drift() -> None:
    """The blocker.reason Values table must carry a plan_scope_drift row."""
    content = _cmd("auto-dev.md")
    assert "| `plan_scope_drift` |" in content


def test_headless_contract_mirrors_plan_scope_drift() -> None:
    """docs/headless-contract.md is kept in lockstep with auto-dev.md."""
    content = _doc("docs/headless-contract.md")
    assert "S2.5 files outside plan, threshold exceeded" in content
    assert "| `plan_scope_drift` |" in content
    assert "#1779" in content


def test_plan_step1b_requires_files_modified_heading() -> None:
    """The gate parser has no anchor unless Step 1b mandates the heading."""
    content = _cmd("auto-dev-plan.md")
    assert "## Files Modified" in content
    assert "one bullet per file" in content


def test_scope_exceeded_and_plan_scope_drift_are_distinguishable() -> None:
    """Acceptance criterion 4: the two scope signals must not read alike."""
    content = _cmd("auto-dev.md")
    assert "before impl started" in content
    assert "after impl, before review" in content


def test_review_md_cross_references_scope_conformance_gate() -> None:
    """Step 3b's plan_deviation rule must point at the earlier mechanical gate."""
    content = _cmd("auto-dev-review.md")
    assert "check_plan_scope_conformance" in content
    assert "plan_scope_drift" in content
