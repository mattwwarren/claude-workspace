"""Doc-structure guards for the post-impl scope-conformance gate (#1779).

Pins the prose wiring that makes the Step 2.5 gate real: the impl command must
invoke the script, the collapse tables must distinguish the blocking drift exit
from the pre-existing non-blocking growth note, and the plan command must
require the ``## Files Modified`` heading the parser anchors on.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    GUARD_FENCE_INVOKED,
    _appendix,
    _bash_fences,
    _cmd,
    run_guard_fence,
)
from tests.test_auto_dev_preflight_resolutions import _after

# Sentinel replacing the real guard-script invocation, so the executable fence
# tests observe *whether* a fence reached it rather than running the script.
_INVOKED = GUARD_FENCE_INVOKED

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
    section = _gate2_section()
    assert '"$GUARD_ROOT/.claude/scripts/check_plan_scope_conformance.py"' in section
    assert '"$HOME/.claude/scripts/check_plan_scope_conformance.py"' in section


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

    # The disposition itself, not merely the words "HEADLESS BLOCK": the stale
    # branch must name the blocker reason and the details template an agent
    # emits, or the prose stops short of telling a worker what to do.
    blocking = "\n".join(stale_lines)
    assert 'blocker.reason: "impl_failed"' in blocking
    assert "Step 2.5 gate 2: HEADLESS BLOCK" in blocking
    assert "check_plan_scope_conformance.py at <resolved-path>" in blocking
    assert "missing/stale cw-script-version marker (need >= 1)" in blocking
    assert "STOP" in blocking


def test_gate2_anchors_the_repo_local_candidate_absolutely() -> None:
    """Gate 2's probe must not depend on the cwd (#2141 review round 2).

    ``auto-dev-impl.md`` names ``worktree_path`` as the authoritative anchor,
    but gate 2 probed a bare relative ``.claude/scripts/...``. The prose must
    now describe the enforced anchor, and must say explicitly why the anchor is
    the cw session worktree rather than ``$TMPWT`` — the gate around it does run
    inside ``$TMPWT``, so silence there reads as a contradiction.
    """
    section = _gate2_section()
    assert "for candidate in .claude/scripts/" not in section
    assert "GUARD_ROOT=$(git rev-parse --show-toplevel" in section
    assert "cw-context.json" in section
    assert "worktree_path" in section
    assert "not `$TMPWT`" in section or "NOT $TMPWT" in section


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
        if f"/.claude/scripts/{script}" in fence and "for candidate in" in fence
    ]
    assert len(fences) == 1, (
        f"{doc}: expected one fence for {script}, got {len(fences)}"
    )
    return fences[0]


def _run_site_fence(
    tmp_path: Path,
    script: str,
    *,
    repo_local: str | None = None,
    global_copy: str | None = None,
    worktree_path_override: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a site's own fence via the shared runner (#2141)."""
    return run_guard_fence(
        tmp_path,
        _site_fence(script),
        script,
        repo_local=repo_local,
        global_copy=global_copy,
        worktree_path_override=worktree_path_override,
    )


_GUARD_SCRIPTS = [
    "check_not_main_checkout.py",
    "check_plan_scope_conformance.py",
    "check_impl_guard_staleness.py",
    "classify_merge_conflict.py",
]

# The one marker value that may reach the invocation: a clean integer at the
# table minimum.
_CURRENT_MARKER = "# cw-script-version: 1\n"

# Every marker state that must NOT reach it. Anything that is not
# ``^[0-9]+$`` is stale by construction — a `grep -oE '[0-9]+'` extraction
# turned `1.5` into two lines, which made `[ ... -lt ... ]` error out and the
# condition evaluate false, so the script ran anyway (#2141 review round 2).
_STALE_MARKERS: list[tuple[str, str]] = [
    ("below_minimum", "# cw-script-version: 0\n"),
    ("no_marker", "import sys\n"),
    ("malformed_float", "# cw-script-version: 1.5\n"),
    ("malformed_alpha", "# cw-script-version: abc\n"),
    ("malformed_negative", "# cw-script-version: -1\n"),
    ("malformed_suffix", "# cw-script-version: 2x\n"),
    ("malformed_empty", "# cw-script-version:\n"),
]

# Where the resolved copy lives. ``global_only`` is the branch that motivated
# this ticket at all (a client repo with no local ``.claude/scripts/``), and it
# is also the branch an anchoring bug hides in: a cwd-relative repo-local probe
# silently misses and falls through to the global copy, so a repo-local-only
# fixture cannot tell a correct resolver from a broken one.
_LOCATIONS = ["repo_local", "global_only"]


def _placement(location: str, body: str) -> dict[str, str | None]:
    if location == "repo_local":
        return {"repo_local": body, "global_copy": None}
    return {"repo_local": None, "global_copy": body}


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
@pytest.mark.parametrize("location", _LOCATIONS)
@pytest.mark.parametrize(("label", "script_body"), _STALE_MARKERS)
def test_every_site_fence_hard_stops_without_invoking(
    tmp_path: Path, script: str, location: str, label: str, script_body: str
) -> None:
    """Executable proof, per site, that a stale hit cannot reach the script.

    Step 2.5 gate 2 is the highest-consequence site — a fall-through there
    ships scope the approved file set never covered — and a text assertion
    cannot distinguish an ``exit 3`` that runs from one that merely appears in
    the prose. Parametrized over both candidate locations so a marker check
    that only guards the repo-local branch is red here (#2141).
    """
    result = _run_site_fence(tmp_path, script, **_placement(location, script_body))
    assert result.returncode != 0, (
        f"{script}/{location}/{label}: fence fell through with exit 0"
    )
    assert _INVOKED not in result.stdout, (
        f"{script}/{location}/{label}: script was reached anyway"
    )
    assert "STALE:" in result.stdout


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
@pytest.mark.parametrize("location", _LOCATIONS)
def test_every_site_fence_reaches_the_script_on_a_current_marker(
    tmp_path: Path, script: str, location: str
) -> None:
    """Companion to the stale cases: the guard must not block the happy path.

    The ``global_only`` case doubles as the anchoring regression: the runner
    executes the fence from a nested subdirectory, so a resolver that probed a
    bare relative ``.claude/scripts/`` would reach the global copy here for the
    wrong reason, and would miss the repo-local copy in the sibling case below.
    """
    result = _run_site_fence(tmp_path, script, **_placement(location, _CURRENT_MARKER))
    assert result.returncode == 0, f"{script}/{location}: {result.stderr}"
    assert _INVOKED in result.stdout
    assert "STALE:" not in result.stdout


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
def test_every_site_fence_prefers_the_repo_local_copy(
    tmp_path: Path, script: str
) -> None:
    """Repo-local wins when both exist — and is not exempt from the marker.

    Two directions, because only the pair pins precedence: a current repo-local
    copy must win over a stale global one (no STALE), and a stale repo-local
    copy must NOT be rescued by a current global one (STALE, no invocation).
    """
    current_local = _run_site_fence(
        tmp_path / "a",
        script,
        repo_local=_CURRENT_MARKER,
        global_copy="# cw-script-version: 0\n",
    )
    assert current_local.returncode == 0, f"{script}: {current_local.stderr}"
    assert _INVOKED in current_local.stdout
    assert "STALE:" not in current_local.stdout

    stale_local = _run_site_fence(
        tmp_path / "b",
        script,
        repo_local="# cw-script-version: 0\n",
        global_copy=_CURRENT_MARKER,
    )
    assert stale_local.returncode != 0, f"{script}: stale repo-local copy was rescued"
    assert _INVOKED not in stale_local.stdout
    assert "STALE:" in stale_local.stdout


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
def test_every_site_fence_honors_cw_context_worktree_path(
    tmp_path: Path, script: str
) -> None:
    """A context-provided `worktree_path` must win over `git rev-parse` (#2141).

    Every other case above only exercises the `git rev-parse --show-toplevel`
    fallback (repo root or `$HOME`) — none plants a `.claude/cw-context.json`
    pointing somewhere else, so the resolver's actual override branch had no
    executable coverage. Here neither the repo nor `$HOME` carries a copy;
    only the `worktree_path`-anchored directory does, so a resolver that
    ignored the override would report the script absent instead of invoking it.
    """
    result = _run_site_fence(tmp_path, script, worktree_path_override=_CURRENT_MARKER)
    assert result.returncode == 0, f"{script}: {result.stderr}"
    assert _INVOKED in result.stdout, (
        f"{script}: context-provided worktree_path was not honored"
    )
    assert "STALE:" not in result.stdout


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
def test_every_site_fence_skips_when_absent_from_both_locations(
    tmp_path: Path, script: str
) -> None:
    """Absent from both keeps each site's own non-blocking skip disposition.

    The marker gate applies only to a candidate that was *found*: absence is a
    separate branch with separate (per-site, prose-level) message text, and the
    fence must neither invoke anything nor hard-stop the shell.
    """
    result = _run_site_fence(tmp_path, script)
    assert result.returncode == 0, f"{script}: {result.stderr}"
    assert _INVOKED not in result.stdout
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
    assert '"$GUARD_ROOT/.claude/scripts/<script>.py"' in template
    assert '[[ ! "$FOUND_VERSION" =~ ^[0-9]+$ ]]' in template


def test_no_site_fence_uses_the_fail_open_or_cwd_relative_shapes() -> None:
    """Both round-2 findings, pinned as absences across every fence (#2141).

    ``grep -oE '[0-9]+'`` scans for digit runs, so a ``1.5`` marker yields two
    lines and the ``-lt`` comparison errors out — the condition evaluates false
    and the script runs. A bare relative ``.claude/scripts/`` probe misses the
    repo-local copy whenever the cwd is not the worktree root. Neither shape may
    survive at any site, including the canonical template.
    """
    docs = {doc for _, doc in _table_minimums().values()}
    docs.add("auto-dev-impl.md")
    docs.add("auto-dev-impl-appendix.md")
    for doc in sorted(docs):
        for fence in _bash_fences(_cmd(doc)):
            if "cw-script-version" not in fence:
                continue
            assert "grep -oE" not in fence, (
                f"{doc}: a digit-run scan accepts `1.5` and then fails open"
            )
            assert '[ -z "$FOUND_VERSION" ]' not in fence, (
                f"{doc}: an emptiness test alone lets a malformed marker through"
            )
            assert '[[ ! "$FOUND_VERSION" =~ ^[0-9]+$ ]]' in fence, (
                f"{doc}: the marker must be rejected unless it is a clean integer"
            )
            assert "for candidate in .claude/scripts/" not in fence, (
                f"{doc}: the repo-local candidate must be absolutely anchored"
            )
            assert '"$GUARD_ROOT/.claude/scripts/' in fence, (
                f"{doc}: the repo-local candidate must anchor to $GUARD_ROOT"
            )
            assert "GUARD_ROOT=$(git rev-parse --show-toplevel" in fence, (
                f"{doc}: $GUARD_ROOT must be computed inside the fence, once"
            )
            assert fence.count("GUARD_ROOT=$(git rev-parse") == 1, (
                f"{doc}: $GUARD_ROOT is computed once per fence, not per candidate"
            )


def test_plan_spec_marker_not_bumped() -> None:
    """Regression guard: this ticket is prose-only and must not bump the
    plan-spec/plan-soundness marker versions (#1881)."""
    content = _cmd("auto-dev-plan.md")
    assert "plan-spec-reviewed" in content
    assert "v2" in content
    assert "v3" not in content
