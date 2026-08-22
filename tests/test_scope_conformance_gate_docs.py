"""Doc-structure guards for the post-impl scope-conformance gate (#1779).

Pins the prose wiring that makes the Step 2.5 gate real: the impl command must
invoke the script, the collapse tables must distinguish the blocking drift exit
from the pre-existing non-blocking growth note, and the plan command must
require the ``## Files Modified`` heading the parser anchors on.
"""

from pathlib import Path

from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after

_REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS = _REPO_ROOT / ".claude" / "agents"


# NOTE: mirrors the `_agent(name)` / `AGENTS` pair already duplicated locally
# in tests/test_plan_format_only_findings.py and
# tests/test_auto_dev_preflight_resolutions.py — no shared module for `_agent`,
# per this repo's established convention.
def _agent(name: str) -> str:
    return (AGENTS / name).read_text(encoding="utf-8")


def _doc(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


def test_impl_step2_5_gate2_invokes_scope_conformance_script() -> None:
    """Step 2.5 gate 2 must call the mechanical gate, not eyeball the file set."""
    content = _cmd("auto-dev-impl.md")
    assert ".claude/scripts/check_plan_scope_conformance.py" in content
    assert "--touched-files" in content


def test_impl_step2_5_gate2_blocks_with_plan_scope_drift_reason() -> None:
    """Exit 1 from the gate script must map to the new blocker reason.

    #1879 relocated gate 2's non-exit-0 dispositions to
    ``auto-dev-impl-appendix.md`` — drift is the exceptional outcome, so the
    branch is rare-path. The core doc keeps the script invocation and the
    common-path exit-0 verdict; the literals below are asserted at their new
    home rather than dropped.
    """
    content = _appendix("impl")
    assert 'blocker.reason: "plan_scope_drift"' in content
    assert '"stage2_impl"' in content


def test_impl_step2_5_below_threshold_still_uses_impl_scope_growth_friction() -> None:
    """Regression guard: the non-blocking within-allowance path must survive."""
    assert "impl_scope_growth" in _appendix("impl")


def test_impl_step2_5_populates_lines_actual_on_the_drift_exit() -> None:
    """stage_reached=stage2_impl requires a non-null scope.lines_actual."""
    assert "scope.lines_actual" in _appendix("impl")


def test_impl_core_doc_keeps_gate2_common_path_and_appendix_trigger() -> None:
    """Exit 0 stays on the common path; only the exceptional branches moved."""
    content = _cmd("auto-dev-impl.md")
    assert "exit 0 with an empty `extra_files`" in content
    assert "scope-conformance disposition by exit code (#1779)" in content


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


def test_headless_contract_backfills_five_stage1_blocker_reasons() -> None:
    """docs/headless-contract.md must carry all five Stage-1 plan-gate
    blocker.reason values from auto-dev.md's Gate-Collapse Table and
    blocker.reason Values table: plan_unreviewable, plan_unsound,
    ambiguity_scan_unconverged, deferred_stub_unresolved, and (since #1897
    merged) scope_tier_stale (#1951)."""
    content = _doc("docs/headless-contract.md")
    gate_start = content.index("## 2. Gate-Collapse Table")
    gate_end = content.index("## 3. Structured Output")
    gate_window = content[gate_start:gate_end]

    blocker_start = content.index(
        '### 4.2 `blocker.reason` (when `status = "blocked"`)'
    )
    blocker_end = content.index("### 4.3 `next_actions` Vocabulary")
    blocker_window = content[blocker_start:blocker_end]

    reasons = [
        "plan_unreviewable",
        "plan_unsound",
        "ambiguity_scan_unconverged",
        "deferred_stub_unresolved",
        "scope_tier_stale",
    ]
    for reason in reasons:
        assert f'blocker.reason: "{reason}"' in gate_window, (
            f"missing gate-collapse row for {reason}"
        )
        assert f"| `{reason}` |" in blocker_window, (
            f"missing blocker.reason table row for {reason}"
        )


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


def test_impl_step2_5_gate2_validates_json_verdict_before_trusting_exit_1() -> None:
    """Exit 1 alone (e.g. a transient `uv run` failure) must not be trusted as
    genuine drift — the prose must require a JSON-verdict check with a
    `triggered` key before building `plan_scope_drift` blocker.details (#1779
    fix cycle 1). Re-pointed at the appendix by #1879 along with the rest of
    gate 2's rare-path disposition."""
    content = _appendix("impl")
    assert "valid JSON verdict" in content
    assert '"triggered" key' in content or "a `triggered` key" in content
    assert "tooling failure, not drift" in content


def test_gate_collapse_tables_mirror_the_tooling_failure_row() -> None:
    """The exit-1-without-a-valid-verdict row must stay in lockstep across
    auto-dev.md and docs/headless-contract.md, the same way the sibling
    plan_scope_drift row already is (#1779 fix cycle 1)."""
    for content in (_cmd("auto-dev.md"), _doc("docs/headless-contract.md")):
        assert "without" in content
        assert "valid JSON verdict" in content
        assert "tooling failure, not drift" in content


def test_plan_step1b_files_modified_is_complete_inventory() -> None:
    """Step 1b's file-enumeration bullet must clarify the heading is a
    complete inventory (test files + mechanical companions), not just the
    source-file subset — otherwise Phase 1 tests and `__init__.py`
    re-exports land as unmeasured `extra_files` at the Step 2.5 gate (#1881)."""
    content = _cmd("auto-dev-plan.md")
    window = _after(content, "one bullet per file", span=1200)
    assert "not a source-only subset" in window
    assert "__init__" in window
    assert "invisible to the gate" in window


def test_plan_reviewer_check2_requires_files_modified_reconciliation() -> None:
    """Check 2's file-list verification must tie back to the single
    ``## Files Modified`` heading the scope-conformance gate parses (#1881)."""
    content = _agent("plan-reviewer.md")
    start = content.index("### Check 2 — File Enumeration")
    end = content.index("### Check 3")
    window = content[start:end]
    assert "## Files Modified" in window
    assert "#1881" in window


def test_plan_reviewer_check2_flags_missing_files_modified_entry() -> None:
    """A file named only in Phase 1/Phase 2 prose but absent from
    ``## Files Modified`` must be a Reject (MUST_FIX) — it is invisible to
    the mechanical scope-conformance gate (#1881)."""
    content = _agent("plan-reviewer.md")
    start = content.index("### Check 2 — File Enumeration")
    end = content.index("### Check 3")
    window = content[start:end]
    assert "missing from `## Files Modified`" in window
    assert "#1881" in window


def test_plan_spec_marker_not_bumped() -> None:
    """Regression guard: this ticket is prose-only and must not bump the
    plan-spec/plan-soundness marker versions (#1881)."""
    content = _cmd("auto-dev-plan.md")
    assert "plan-spec-reviewed" in content
    assert "v2" in content
    assert "v3" not in content
