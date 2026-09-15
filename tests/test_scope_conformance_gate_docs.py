"""Doc-structure guards for the post-impl scope-conformance gate (#1779).

Pins the prose wiring that makes the Step 2.5 gate real: the impl command must
invoke the script, the collapse tables must distinguish the blocking drift exit
from the pre-existing non-blocking growth note, and the plan command must
require the ``## Files Modified`` heading the parser anchors on.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import _appendix, _bash_fences, _cmd
from tests.test_auto_dev_preflight_resolutions import _after

# Sentinel replacing the real guard-script invocation, so the executable fence
# tests observe *whether* a fence reached it rather than running the script.
_INVOKED = "INVOKED"

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


_GATE2_START = "2. **File set is within the plan's enumeration**"
_GATE2_END = "3. **Test command exit code is 0:**"
_RESOLVER_SUBSECTION = "### Guard-script path resolution and staleness marker (#2141)"

# `| `<script>.py` | <N> | `<doc>.md` ... |` — the version table's data rows.
_TABLE_ROW = re.compile(
    r"^\|\s*`(?P<script>[\w.]+\.py)`\s*\|\s*(?P<version>\d+)\s*\|\s*`(?P<doc>[\w.-]+\.md)`",
    re.MULTILINE,
)
# The per-site single declaration Cluster B mandates; fences are indented at
# some sites (gate 2 lives inside a numbered list), so leading space is allowed.
_MIN_VERSION_DECL = re.compile(r"^[ \t]*MIN_VERSION=(\d+)\b", re.MULTILINE)


def _table_minimums() -> dict[str, tuple[int, str]]:
    """Map each guard script to its ``(minimum version, call-site doc)`` row."""
    rows = {
        match["script"]: (int(match["version"]), match["doc"])
        for match in _TABLE_ROW.finditer(_resolver_table_section())
    }
    assert len(rows) == 4, f"expected 4 table rows, parsed {sorted(rows)}"
    return rows


def _gate2_section() -> str:
    """Step 2.5 gate 2, from its numbered bullet to gate 3's (#2141)."""
    content = _cmd("auto-dev-impl.md")
    start = content.index(_GATE2_START)
    end = content.index(_GATE2_END, start)
    return content[start:end]


def _resolver_table_section() -> str:
    """The shared resolver/marker subsection in auto-dev-impl.md (#2141)."""
    content = _cmd("auto-dev-impl.md")
    start = content.index(_RESOLVER_SUBSECTION)
    end = content.index("\n### ", start + len(_RESOLVER_SUBSECTION))
    return content[start:end]


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


def test_gate2_resolves_repo_local_then_global_script_path() -> None:
    """Gate 2 must probe the installed copy too, not just the repo copy (#2141).

    ``scripts/install-skills.sh`` has symlinked this script into
    ``~/.claude/scripts/`` since #2096; a client repo without a local
    ``.claude/scripts/`` previously made the gate silently no-op.
    """
    content = _cmd("auto-dev-impl.md")
    assert ".claude/scripts/check_plan_scope_conformance.py" in content
    assert '"$HOME/.claude/scripts/check_plan_scope_conformance.py"' in content


def test_gate2_absent_from_both_locations_skips_non_blocking() -> None:
    """Absent from both locations is explicitly non-blocking (#2141).

    Previously a missing file exited 2 and was swallowed by the appendix's
    generic "parse error" branch — non-blocking by accident, under the wrong
    label. This pins the honest label and the continue-to-gate-3 disposition.
    """
    section = _gate2_section()
    assert "check_plan_scope_conformance: script absent, skipped" in section
    assert "continue to gate 3" in section


def test_gate2_greps_cw_script_version_marker_and_headless_blocks_on_stale() -> None:
    """The marker-stale branch is a hard stop, textually distinct from the
    non-blocking absent-from-both-locations branch (#2141)."""
    section = _gate2_section()
    assert "cw-script-version" in section
    assert "HEADLESS BLOCK" in section

    absent_lines = [
        line
        for line in section.splitlines()
        if "check_plan_scope_conformance: script absent, skipped" in line
    ]
    stale_lines = [line for line in section.splitlines() if "HEADLESS BLOCK" in line]
    assert absent_lines
    assert stale_lines
    assert all("HEADLESS BLOCK" not in line for line in absent_lines)
    assert all("script absent, skipped" not in line for line in stale_lines)


def test_resolver_table_lists_all_four_scripts_with_minimum_version() -> None:
    """One shared table in auto-dev-impl.md covers all four guard scripts."""
    section = _resolver_table_section()
    for script in (
        "check_not_main_checkout.py",
        "check_plan_scope_conformance.py",
        "check_impl_guard_staleness.py",
        "classify_merge_conflict.py",
    ):
        assert script in section
    assert "cw-script-version" in section


def test_every_call_site_declares_min_version_matching_the_table() -> None:
    """The version table is the single source; no site repeats the literal (#2141).

    Every call site previously hard-coded its minimum twice — once in the
    ``-lt`` comparison and once in the ``need >= N`` message — so a table bump
    could land without the sites, or vice versa, with nothing failing. Each
    site now declares ``MIN_VERSION=<N>`` once and uses the variable in both
    places; this test is the CI coupling that makes a one-sided bump red.
    """
    for script, (version, doc) in _table_minimums().items():
        fences = [fence for fence in _bash_fences(_cmd(doc)) if f"/{script}" in fence]
        assert fences, f"no bash fence invoking {script} in {doc}"
        for fence in fences:
            assert _MIN_VERSION_DECL.findall(fence) == [str(version)], (
                f"{doc}: fence for {script} must declare MIN_VERSION={version} "
                f"exactly once, to match the version table"
            )
            assert '-lt "$MIN_VERSION"' in fence, (
                f"{doc}: fence for {script} must compare against $MIN_VERSION, "
                f"not a repeated literal"
            )
            assert "need >= $MIN_VERSION" in fence, (
                f"{doc}: fence for {script} must interpolate $MIN_VERSION into "
                f"the STALE message, not repeat the literal"
            )
            assert f"-lt {version}" not in fence
            assert f"need >= {version}" not in fence


def test_headless_block_prose_quotes_the_table_minimum() -> None:
    """The ``blocker.details`` templates carry the table's number too (#2141).

    Each site's HEADLESS BLOCK bullet spells the minimum into the sentinel
    message an agent emits. That literal cannot be a shell variable — it is
    prose, not a fence — so it is pinned to the table here instead: a bump that
    updates the table and the fences but leaves the bullets behind is red.
    """
    for script, (version, doc) in _table_minimums().items():
        bullets = [
            line
            for line in _cmd(doc).splitlines()
            if "HEADLESS BLOCK" in line and script in line
        ]
        assert bullets, f"no HEADLESS BLOCK bullet naming {script} in {doc}"
        for bullet in bullets:
            assert f"need >= {version}" in bullet, (
                f"{doc}: the {script} HEADLESS BLOCK bullet must quote the "
                f"table minimum ({version})"
            )


def test_every_call_site_hard_stops_inside_the_bash_fence() -> None:
    """A stale marker must make the invocation unreachable *in the shell* (#2141).

    The prose bullet after each fence directs ``EXIT blocked``/STOP, which is
    the documented idiom for an agent-level exit that cannot live in shell. But
    a worker skimming only the fence saw a bare ``echo`` and could fall
    through — the highest-consequence case being Step 2.5 gate 2, where a
    skipped stop bypasses the approved file-set gate entirely.
    """
    docs = {doc for _, doc in _table_minimums().values()}
    docs.add("auto-dev-impl.md")  # carries the canonical template fence too
    for doc in sorted(docs):
        for fence in _bash_fences(_cmd(doc)):
            if "cw-script-version" not in fence:
                continue
            assert "exit 3" in fence, (
                f"{doc}: a stale-marker fence must hard-stop with `exit 3`, "
                f"not merely echo STALE"
            )
            assert "HARD STOP" in fence, (
                f"{doc}: the stale branch must carry a HARD STOP comment "
                f"naming the blocker disposition"
            )
            stale_index = fence.index("STALE:")
            assert fence.index("exit 3", stale_index) > stale_index, (
                f"{doc}: `exit 3` must follow the STALE echo"
            )


def _site_fence(script: str) -> str:
    """The single bash fence that probes and invokes *script* (#2141)."""
    doc = _table_minimums()[script][1]
    fences = [
        fence
        for fence in _bash_fences(_cmd(doc))
        if f"for candidate in .claude/scripts/{script}" in fence
    ]
    assert len(fences) == 1, (
        f"{doc}: expected one fence for {script}, got {len(fences)}"
    )
    return fences[0]


def _run_site_fence(
    tmp_path: Path, script: str, script_body: str
) -> subprocess.CompletedProcess[str]:
    """Execute a site's own fence against a fixture guard script (#2141).

    Both invocation spellings in these docs (``uv run python "$RESOLVED"`` and
    the bare ``python "$RESOLVED"`` the stdlib-only pre-mutation guard uses) are
    swapped for an ``echo`` sentinel, and the per-site capture variables are
    echoed afterwards because three of the four fences assign the invocation
    into a command substitution rather than letting it print.
    """
    repo = tmp_path / "repo"
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / script).write_text(script_body, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    fence = (
        _site_fence(script)
        .replace("uv run python", f"echo {_INVOKED}")
        .replace('python "$RESOLVED"', f'echo {_INVOKED} "$RESOLVED"')
    )
    captures = '"${VERDICT-}${RESOLVE_OUTPUT-}${SCOPE_CONFORMANCE_OUTPUT-}"'
    body = f"{fence}\necho {captures}\n"
    # Scoped to this tmp_path so the gate-2 fence's hard-coded
    # `/tmp/touched_files-$CW_SESSION` scratch write cannot collide with a
    # concurrent run; the path is literal in the doc, so it is cleaned up here
    # rather than redirected.
    session = tmp_path.name
    try:
        return subprocess.run(
            ["bash", "-c", body],
            cwd=repo,
            env={
                "HOME": str(home),
                "PATH": os.environ.get("PATH", ""),
                "CW_SESSION": session,
                "TMPWT": str(repo),
                "FORK_POINT": "HEAD",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(f"/tmp/touched_files-{session}").unlink(missing_ok=True)


_GUARD_SCRIPTS = [
    "check_not_main_checkout.py",
    "check_plan_scope_conformance.py",
    "check_impl_guard_staleness.py",
    "classify_merge_conflict.py",
]


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
@pytest.mark.parametrize(
    ("label", "script_body"),
    [
        ("below_minimum", "# cw-script-version: 0\n"),
        ("no_marker", "import sys\n"),
    ],
)
def test_every_site_fence_hard_stops_without_invoking(
    tmp_path: Path, script: str, label: str, script_body: str
) -> None:
    """Executable proof, per site, that a stale hit cannot reach the script.

    The operator's adjudication made parametrizing over every fence optional;
    it is done here because Step 2.5 gate 2 is the highest-consequence site —
    a fall-through there ships scope the approved file set never covered — and
    a text assertion cannot distinguish an ``exit 3`` that runs from one that
    merely appears in the prose (#2141).
    """
    result = _run_site_fence(tmp_path, script, script_body)
    assert result.returncode != 0, f"{script}/{label}: fence fell through with exit 0"
    assert _INVOKED not in result.stdout, f"{script}/{label}: script was reached anyway"
    assert "STALE:" in result.stdout


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
def test_every_site_fence_reaches_the_script_on_a_current_marker(
    tmp_path: Path, script: str
) -> None:
    """Companion to the stale cases: the guard must not block the happy path."""
    result = _run_site_fence(tmp_path, script, "# cw-script-version: 1\n")
    assert result.returncode == 0, f"{script}: {result.stderr}"
    assert _INVOKED in result.stdout
    assert "STALE:" not in result.stdout


def test_canonical_template_shows_the_hard_stop_shape() -> None:
    """The template every site copies carries the guard and the placeholders."""
    section = _resolver_table_section()
    template = next(fence for fence in _bash_fences(section) if "<script>.py" in fence)
    assert "MIN_VERSION=<N>" in template
    assert '-lt "$MIN_VERSION"' in template
    assert "need >= $MIN_VERSION" in template
    assert "<blocker.reason>" in template
    assert "exit 3" in template


def test_plan_spec_marker_not_bumped() -> None:
    """Regression guard: this ticket is prose-only and must not bump the
    plan-spec/plan-soundness marker versions (#1881)."""
    content = _cmd("auto-dev-plan.md")
    assert "plan-spec-reviewed" in content
    assert "v2" in content
    assert "v3" not in content
