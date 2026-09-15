"""Doc-structure guards for the post-impl scope-conformance gate (#1779).

Pins the prose wiring that makes the Step 2.5 gate real: the impl command must
invoke the script, the collapse tables must distinguish the blocking drift exit
from the pre-existing non-blocking growth note, and the plan command must
require the ``## Files Modified`` heading the parser anchors on.

Since #2141 the file also *executes* the guard-script fences the docs tell a
worker to copy, because a text assertion cannot tell an ``exit 3`` that runs
from one that merely appears in the prose. Two runners, by anchor shape:
``_run_site_fence`` for the three ``$GUARD_ROOT`` sites, and
``_run_gate2_fence`` for Step 2.5 gate 2, which derives its own ``$TMPWT``,
``$FORK_POINT`` and ``$SESSION_WT`` and therefore needs real git worktrees and
a shell carrying none of the setup fence's state.

The marker fixtures both runners are driven with — ``GUARD_MARKER_CURRENT`` and
the ``GUARD_MARKER_BAD_CASES`` table — live in ``tests/conftest.py`` and are
shared with ``test_auto_dev_finalize_semantic_resolve.py``: a new stale-marker
case belongs in that table, not in a local list here (#2141 round 6).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest

from tests.conftest import (
    GUARD_FENCE_INVOKED,
    GUARD_MARKER_BAD_CASES,
    GUARD_MARKER_CURRENT,
    GUARD_MARKER_GOOD_CASES,
    GUARD_MARKER_STALE,
    _appendix,
    _bash_fences,
    _clean_git_env,
    _cmd,
    _placement,
    guard_candidate_path,
    run_guard_fence,
    substitute_fence_placeholders,
    write_guard_stub_bin,
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
_MARKER_RULE_START = "**Marker parsing is strict"
_MARKER_RULE_END = "**Existence is not enough.**"

# `| `<script>.py` | <N> | `<doc>.md` ... |` — the version table's data rows.
_TABLE_ROW = re.compile(
    r"^\|\s*`(?P<script>[\w.]+\.py)`\s*\|\s*(?P<version>\d+)\s*\|\s*`(?P<doc>[\w.-]+\.md)`",
    re.MULTILINE,
)
# The per-site single declaration Cluster B mandates; fences are indented at
# some sites (gate 2 lives inside a numbered list), so leading space is allowed.
_MIN_VERSION_DECL = re.compile(r"^[ \t]*MIN_VERSION=(\d+)\b", re.MULTILINE)

# The one marker-token regex the docs may state, in prose or in a fence.
_BOUNDED_MARKER_REGEX = "^[0-9]{1,6}$"

# How many lines of a script count as its header, and the header-anchored sed
# every marker fence must extract with (#2141 round 8).
_HEADER_LINES = "head -n 5"
_HEADER_ANCHORED_SED = "sed -nE 's/^#[[:space:]]*cw-script-version:"
# Any anchored digit regex, however bounded — used to prove there is only one.
_ANCHORED_DIGIT_REGEX = re.compile(r"\^\[0-9\][^\s]*\$")


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


def _marker_rule_prose() -> str:
    """The canonical marker-parsing rule, fence excluded (#2141 round 7).

    Deliberately narrower than ``_resolver_table_section``: that span also
    contains the template *fence*, so a substring assertion on it is satisfied
    by the fence alone and says nothing about whether the rule a worker reads
    states the same thing the snippet a worker copies does.
    """
    section = _resolver_table_section()
    start = section.index(_MARKER_RULE_START)
    end = section.index(_MARKER_RULE_END, start)
    return section[start:end]


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
    assert '"$SESSION_WT/.claude/scripts/check_plan_scope_conformance.py"' in section
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

    ``auto-dev-impl.md`` names the cw session worktree as the authoritative
    anchor, but gate 2 probed a bare relative ``.claude/scripts/...``. The prose
    must now describe the enforced anchor, and must say explicitly why the
    anchor is the cw session worktree rather than ``$TMPWT`` — the gate around
    it does run inside ``$TMPWT``, so silence there reads as a contradiction.
    """
    section = _gate2_section()
    assert "for candidate in .claude/scripts/" not in section
    assert '"$SESSION_WT/.claude/scripts/' in section
    assert "not `$TMPWT`" in section or "NOT $TMPWT" in section


def test_gate2_fence_derives_the_session_worktree_in_its_own_fence() -> None:
    """Gate 2 must be correct on its own, in a fresh shell (#2141 round 4).

    Nothing guarantees that Step 2.5's gate-setup fence and gate 2 share one
    Bash call — a headless worker may run each fenced block separately — so any
    anchor captured in an earlier fence is unset here, and any fallback to the
    ambient checkout resolves to ``$TMPWT`` (the detached gate worktree), which
    is the original bug. The fence therefore derives the worktree itself from
    the branch, and carries no ambient-cwd escape hatch at all.
    """
    fence = _gate2_fence()
    assert 'git -C "$TMPWT" worktree list' in fence
    # Round 8: derived in-fence, but no longer from the branch name alone — the
    # ticket-keyed probe comes first and the branch lookup is its fallback.
    assert "cw-context.json" in fence
    assert ".ticket_id" in fence
    assert "<ticket-id>" in fence
    assert fence.index("cw-context.json") < fence.index("branch refs/heads/"), (
        "the branch-keyed lookup must be the fallback, not the primary"
    )
    for forbidden in (
        "SESSION_ROOT",
        "GUARD_ROOT",
        "rev-parse",
        "$PWD",
        "CTX_WORKTREE",
    ):
        assert forbidden not in fence, (
            f"gate 2's fence must not reference {forbidden}: it either depends "
            f"on another fence's shell state or falls back to the ambient cwd"
        )


def test_gate2_fence_defines_tmpwt_and_fork_point_before_using_them() -> None:
    """Gate 2's fence must define its own setup variables (#2141 round 5).

    Round 4 replaced the fence from ``MIN_VERSION=`` down and left the
    pre-existing ``git -C "$TMPWT" diff --name-only "$FORK_POINT"`` line above
    it, so the block read ``$TMPWT`` before assigning it and read
    ``$FORK_POINT``, which it never assigned at all. The ordering is the whole
    self-containment claim, so it is pinned textually here rather than left to
    the executable test alone.
    """
    fence = _gate2_fence()
    for name in ("TMPWT", "FORK_POINT"):
        assignment = f"{name}="
        use = f'"${name}"'
        assert assignment in fence, (
            f"gate 2's fence never assigns ${name}: it depends on the setup "
            f"fence's shell state, which does not persist"
        )
        assert fence.index(assignment) < fence.index(use), (
            f"gate 2's fence reads ${name} before assigning it"
        )


def test_canonical_rule_bounds_the_marker_digit_count() -> None:
    """The rule must state the bound as the regex the fences use (#2141 round 7).

    An unbounded ``^[0-9]+$`` accepts a 20-digit marker, which overflows
    ``[ -lt ]``; the comparison errors, evaluates false, and the invocation
    runs anyway — the same fail-open shape as the round-2 ``1.5`` finding.
    Round 5 pinned only the English ("1-6 digit"), which no fence contains, so
    the prose rule and the fences were not actually pinned to one thing; the
    rule now spells the literal regex and this asserts on that.
    """
    rule = _marker_rule_prose()
    assert "1-6 digit" in rule, (
        "the canonical marker-parsing rule must state the 1-6 digit bound"
    )
    assert _BOUNDED_MARKER_REGEX in rule, (
        "the canonical rule must state the exact bounded regex, not only its "
        "English gloss — the fences are what a worker copies"
    )
    assert "^[0-9]+$" not in _resolver_table_section(), (
        "the canonical rule must not still advertise the unbounded regex"
    )


def _marker_fence_docs() -> list[str]:
    """Every doc carrying a ``cw-script-version`` fence (#2141 round 8)."""
    docs = {doc for _, doc in _table_minimums().values()}
    docs.add("auto-dev-impl.md")  # carries the canonical template fence too
    docs.add("auto-dev-impl-appendix.md")
    return sorted(docs)


def test_every_marker_fence_anchors_the_parse_to_the_script_header() -> None:
    """The marker must be a header comment line, not a mention anywhere (#2141 round 8).

    ``grep -m1 'cw-script-version:'`` matched the literal wherever it appeared —
    a docstring, a help string, a comment *about* the convention — so a script
    whose header carried no marker at all passed on an incidental later mention,
    which is precisely the stale copy the gate exists to reject. A behaviour
    change to the shared shape has to land at every copy at once, so this scans
    all of them rather than one exemplar.
    """
    fences = 0
    for doc in _marker_fence_docs():
        for fence in _bash_fences(_cmd(doc)):
            if "cw-script-version" not in fence:
                continue
            fences += 1
            assert "grep -m1" not in fence, (
                f"{doc}: `grep -m1` matches the marker anywhere in the file, so "
                f"an unmarked header passes on a later mention"
            )
            assert _HEADER_LINES in fence, (
                f"{doc}: the marker parse must be bounded to the script header"
            )
            assert _HEADER_ANCHORED_SED in fence, (
                f"{doc}: the marker must be extracted from a full `# "
                f"cw-script-version: N` comment line, anchored at both ends"
            )
    assert fences == 5, f"expected 5 marker fences across the docs, found {fences}"


def test_canonical_rule_anchors_the_marker_to_the_script_header() -> None:
    """The prose rule must state the header anchoring, not only the fence.

    Same shape as ``test_canonical_rule_bounds_the_marker_digit_count``: the
    fences are what a worker copies, but the rule is what a worker reasons from,
    and round 7 already showed the two drifting apart silently.
    """
    rule = _marker_rule_prose()
    assert "first 5 lines" in rule
    assert "# cw-script-version: N" in rule
    assert _HEADER_LINES in rule
    # Named as a prohibition, the way `grep -oE` already is — an unanchored
    # extraction is the round-8 finding, so the rule has to say not to.
    assert "Never `grep -m1" in rule, (
        "the canonical rule must forbid the unanchored extraction by name"
    )


def test_every_marker_regex_in_the_impl_doc_is_the_bounded_one() -> None:
    """One regex, at every occurrence in auto-dev-impl.md (#2141 round 7).

    ``test_no_site_fence_uses_the_fail_open_or_cwd_relative_shapes`` proves the
    bounded form is *present* in each marker fence; a looser second regex
    sitting alongside it — in another fence, or in the prose rule — is
    invisible to a presence check. This scans every anchored digit regex in the
    doc and requires them identical.
    """
    content = _cmd("auto-dev-impl.md")
    stated = set(_ANCHORED_DIGIT_REGEX.findall(content))
    assert stated == {_BOUNDED_MARKER_REGEX}, (
        f"auto-dev-impl.md states more than one marker regex: {sorted(stated)}"
    )
    marker_fences = [
        fence for fence in _bash_fences(content) if "cw-script-version" in fence
    ]
    assert marker_fences, "no cw-script-version fence in auto-dev-impl.md"
    for fence in marker_fences:
        assert _BOUNDED_MARKER_REGEX in fence


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


_GATE2_SCRIPT = "check_plan_scope_conformance.py"


def _gate2_fence() -> str:
    """Step 2.5 gate 2's own bash fence (#2141)."""
    return _site_fence(_GATE2_SCRIPT)


# The three sites whose resolver anchors to ``$GUARD_ROOT`` and is therefore
# exercisable by the shared ``run_guard_fence`` runner. Step 2.5 gate 2 derives
# its anchor from ``git worktree list`` instead, so it runs through
# ``_run_gate2_fence`` below — same matrix, real worktrees (#2141 round 4).
_GUARD_SCRIPTS = [
    "check_not_main_checkout.py",
    "check_impl_guard_staleness.py",
    "classify_merge_conflict.py",
]

# Where the resolved copy lives. ``global_only`` is the branch that motivated
# this ticket at all (a client repo with no local ``.claude/scripts/``), and it
# is also the branch an anchoring bug hides in: a cwd-relative repo-local probe
# silently misses and falls through to the global copy, so a repo-local-only
# fixture cannot tell a correct resolver from a broken one.
_LOCATIONS = ["repo_local", "global_only"]


@pytest.mark.parametrize("script", _GUARD_SCRIPTS)
@pytest.mark.parametrize("location", _LOCATIONS)
@pytest.mark.parametrize(("label", "script_body"), GUARD_MARKER_BAD_CASES)
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
@pytest.mark.parametrize(("label", "script_body"), GUARD_MARKER_GOOD_CASES)
def test_every_site_fence_reaches_the_script_on_a_current_marker(
    tmp_path: Path, script: str, location: str, label: str, script_body: str
) -> None:
    """Companion to the stale cases: the guard must not block the happy path.

    The ``global_only`` case doubles as the anchoring regression: the runner
    executes the fence from a nested subdirectory, so a resolver that probed a
    bare relative ``.claude/scripts/`` would reach the global copy here for the
    wrong reason, and would miss the repo-local copy in the sibling case below.

    Parametrized over marker *placement* as well since round 8: anchoring the
    parse to the header is what rejects ``marker_outside_header``, and a matrix
    with only a line-1 marker cannot tell a header-anchored parse from one
    pinned to a single hard-coded line number.
    """
    result = _run_site_fence(tmp_path, script, **_placement(location, script_body))
    assert result.returncode == 0, f"{script}/{location}/{label}: {result.stderr}"
    assert _INVOKED in result.stdout
    # Not just *that* an invocation happened: the stub echoes its argument
    # vector, so the resolved path is observable and must be the one candidate
    # this case planted. Without it a resolver hard-coded to `$HOME` passed the
    # repo-local case (#2141 round 6).
    assert guard_candidate_path(tmp_path, location, script) in result.stdout, (
        f"{script}/{location}/{label}: resolved a different copy: {result.stdout!r}"
    )
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
        repo_local=GUARD_MARKER_CURRENT,
        global_copy=GUARD_MARKER_STALE,
    )
    assert current_local.returncode == 0, f"{script}: {current_local.stderr}"
    assert _INVOKED in current_local.stdout
    # Precedence is only pinned by the path: "invoked, no STALE" is equally true
    # of a resolver that reached the *global* copy and never saw the stale
    # marker on it (#2141 round 6).
    assert (
        guard_candidate_path(tmp_path / "a", "repo_local", script)
        in current_local.stdout
    ), f"{script}: the global copy won over the repo-local one"
    assert "STALE:" not in current_local.stdout

    stale_local = _run_site_fence(
        tmp_path / "b",
        script,
        repo_local=GUARD_MARKER_STALE,
        global_copy=GUARD_MARKER_CURRENT,
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
    result = _run_site_fence(
        tmp_path, script, worktree_path_override=GUARD_MARKER_CURRENT
    )
    assert result.returncode == 0, f"{script}: {result.stderr}"
    assert _INVOKED in result.stdout, (
        f"{script}: context-provided worktree_path was not honored"
    )
    assert (
        guard_candidate_path(tmp_path, "worktree_override", script) in result.stdout
    ), f"{script}: resolved outside the context-provided worktree_path"
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


_GATE2_BRANCH = "dev/gate2-fixture"
_GATE2_LOCATIONS = ["session_worktree", "global_only"]

# The ticket id the gate-2 fence's `<ticket-id>` placeholder is filled with, and
# the value planted in the session worktree's `.claude/cw-context.json` for the
# cases that exercise the context-keyed lookup (#2141 round 8).
_GATE2_TICKET = "2141"

# A local branch name deliberately unlike `<branch-name>`, standing in for a
# session worktree checked out on something other than the remote feature
# branch — the shape the branch-keyed lookup alone cannot find.
_GATE2_LOCAL_BRANCH = "agent-9f1c2e"

# Committed on the branch one commit past ``main``, so the fence's own
# ``FORK_POINT`` derivation shows up as real content in
# ``/tmp/touched_files-$CW_SESSION`` (#2141 round 5).
_PROBE_FILE = "scope_probe.txt"


class _Gate2Placement(TypedDict):
    """Where gate 2's resolver should find its candidate, as ``**kwargs``."""

    session_copy: str | None
    global_copy: str | None


def _gate2_placement(location: str, body: str) -> _Gate2Placement:
    if location == "session_worktree":
        return {"session_copy": body, "global_copy": None}
    return {"session_copy": None, "global_copy": body}


def _gate2_candidate_path(tmp_path: Path, location: str) -> str:
    """The absolute script path gate 2 must resolve to for *location* (#2141).

    Gate 2's companion to ``conftest.guard_candidate_path``; separate because
    its session-worktree candidate is anchored by ``git worktree list`` rather
    than by the ``run_guard_fence`` fixture layout.
    """
    root = tmp_path / ("session-wt" if location == "session_worktree" else "home")
    return str(root.resolve() / ".claude" / "scripts" / _GATE2_SCRIPT)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=_clean_git_env(),
    )


def _gate2_session(tmp_path: Path) -> str:
    """A ``$CW_SESSION`` unique to *tmp_path* (#2141 round 4).

    Gate 2's fence hard-codes ``/tmp/gate-wt-$CW_SESSION``, so the session id
    is what keeps two concurrently-running tests (or a stale run's leftovers)
    from sharing one detached worktree. Derived from the full ``tmp_path``
    rather than its basename, which repeats across parametrized cases.
    """
    return "t" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]


def _write_failing_git_diff_stub(bin_dir: Path) -> None:
    """Plant a ``git`` that fails only on ``diff``, ahead of the real one (#2141).

    Gate 2's touched-file extraction is the one command in the fence whose
    failure was invisible: piped straight into ``sort``, its status was
    discarded. Every other subcommand the fence runs (``merge-base``,
    ``worktree list``) must still behave, so the stub delegates.
    """
    real_git = shutil.which("git")
    assert real_git, "git must be on PATH to shadow it"
    stub = bin_dir / "git"
    stub.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "diff" ]; then\n'
        '    echo "fatal: stubbed git diff failure" >&2\n'
        "    exit 128\n"
        "  fi\n"
        "done\n"
        f'exec {real_git} "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _add_gate2_session_worktree(
    repo: Path,
    session_wt: Path,
    *,
    session_branch: str,
    session_copy: str | None,
    context_ticket: str | None,
    plan: bool,
) -> None:
    """Provision the session worktree gate 2 must resolve to (#2141 round 8).

    Split out of ``_run_gate2_fence`` when round 8's two extra knobs pushed it
    past the statement ceiling; the four fixture shapes it plants (branch name,
    context file, script copy, plan file) are one concern.
    """
    if session_branch == _GATE2_BRANCH:
        _git(repo, "worktree", "add", str(session_wt), _GATE2_BRANCH)
    else:
        _git(
            repo,
            "worktree",
            "add",
            "-b",
            session_branch,
            str(session_wt),
            _GATE2_BRANCH,
        )
    if context_ticket is not None:
        context_dir = session_wt / ".claude"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "cw-context.json").write_text(
            json.dumps({"ticket_id": context_ticket}), encoding="utf-8"
        )
    if session_copy is not None:
        scripts = session_wt / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / _GATE2_SCRIPT).write_text(session_copy, encoding="utf-8")
    if plan:
        cw_dir = session_wt / ".cw"
        cw_dir.mkdir(parents=True, exist_ok=True)
        (cw_dir / "plan.md").write_text("## Files Modified\n", encoding="utf-8")


def _run_gate2_fence(
    tmp_path: Path,
    *,
    session_copy: str | None = None,
    global_copy: str | None = None,
    plan: bool = True,
    session_worktree: bool = True,
    gate_worktree: bool = True,
    break_git_diff: bool = False,
    session_branch: str = _GATE2_BRANCH,
    context_ticket: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute gate 2's own fence against real worktrees (#2141 round 4/5).

    Deliberately not the shared ``run_guard_fence``: gate 2's resolver reads
    ``git -C "$TMPWT" worktree list``, so only a fixture with an actual detached
    gate worktree *and* an actual session worktree on the branch can tell a
    correct resolver from one that happens to land on the right directory.

    The fence runs as its own ``bash -c`` invocation with **cwd = the detached
    ``$TMPWT``** — the shell a headless worker would give it if it executed each
    fenced block separately — and with ``TMPWT`` and ``FORK_POINT`` **unset**
    (#2141 round 5). Those two are the setup fence's variables, and the gate-2
    block must derive both itself; a fixture that exported them could not tell a
    self-contained fence from one silently reading another fence's shell state.
    ``SESSION_ROOT`` and ``GUARD_ROOT`` are still exported, pointing at the
    detached checkout: they are round 4's poison decoys, and a fence that
    consults either — or that falls back to the ambient cwd — resolves to the
    gate worktree and fails these tests instead of silently skipping the gate.

    *session_branch* is the local branch the session worktree is checked out on,
    defaulting to ``<branch-name>`` itself; any other value leaves no worktree on
    ``refs/heads/<branch-name>``, so only the ``cw-context.json`` lookup can
    resolve it. *context_ticket*, when given, plants that ticket id in the
    session worktree's ``.claude/cw-context.json``. Both default to the
    round-4/5 shape, so every pre-existing case still exercises the branch-keyed
    fallback rather than the new primary path (#2141 round 8).

    The repo carries a real ``origin`` remote (itself), so ``origin/main`` and
    ``origin/<branch>`` exist for the fence's own ``merge-base``, and the branch
    carries one commit past ``main`` touching ``_PROBE_FILE`` — which is what
    makes an in-fence ``FORK_POINT`` observable in the touched-files scratch
    output echoed after the fence.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "cw test")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    _git(repo, "checkout", "-b", _GATE2_BRANCH)
    (repo / _PROBE_FILE).write_text("delivered\n", encoding="utf-8")
    _git(repo, "add", _PROBE_FILE)
    _git(repo, "commit", "-m", "probe")
    _git(repo, "checkout", "main")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin")

    if session_worktree:
        _add_gate2_session_worktree(
            repo,
            tmp_path / "session-wt",
            session_branch=session_branch,
            session_copy=session_copy,
            context_ticket=context_ticket,
            plan=plan,
        )

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    if global_copy is not None:
        global_scripts = home / ".claude" / "scripts"
        global_scripts.mkdir(parents=True, exist_ok=True)
        (global_scripts / _GATE2_SCRIPT).write_text(global_copy, encoding="utf-8")

    session = _gate2_session(tmp_path)
    tmpwt = Path(f"/tmp/gate-wt-{session}")
    if gate_worktree:
        _git(repo, "worktree", "add", "--detach", str(tmpwt), _GATE2_BRANCH)

    bin_dir = write_guard_stub_bin(tmp_path)
    if break_git_diff:
        _write_failing_git_diff_stub(bin_dir)
    body = (
        substitute_fence_placeholders(
            _gate2_fence(),
            {"branch-name": _GATE2_BRANCH, "ticket-id": _GATE2_TICKET},
        )
        + '\necho "${SCOPE_CONFORMANCE_OUTPUT-}"'
        + f'\ncat "/tmp/touched_files-{session}" 2>/dev/null\n'
    )
    try:
        return subprocess.run(
            ["bash", "-c", body],
            # A missing gate worktree cannot also be the cwd; the repo root
            # stands in, and is itself a decoy the fence must not resolve to.
            cwd=tmpwt if gate_worktree else repo,
            env={
                "HOME": str(home),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CW_SESSION": session,
                "SESSION_ROOT": str(tmpwt),
                "GUARD_ROOT": str(tmpwt),
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(tmpwt)],
            capture_output=True,
            check=False,
            env=_clean_git_env(),
        )
        shutil.rmtree(tmpwt, ignore_errors=True)
        Path(f"/tmp/touched_files-{session}").unlink(missing_ok=True)


def test_gate2_fence_resolves_the_session_worktree_from_the_gate_worktree(
    tmp_path: Path,
) -> None:
    """The round-4 contract, executed: cwd is the detached gate worktree, no
    anchor is inherited, and the fence still finds the session worktree's script
    and passes it the session worktree's absolute ``.cw/plan.md`` (#2141)."""
    result = _run_gate2_fence(tmp_path, session_copy=GUARD_MARKER_CURRENT)
    assert result.returncode == 0, result.stderr
    assert _INVOKED in result.stdout
    session_wt = (tmp_path / "session-wt").resolve()
    assert str(session_wt / ".claude" / "scripts" / _GATE2_SCRIPT) in result.stdout
    assert f"--plan {session_wt / '.cw' / 'plan.md'}" in result.stdout


def test_gate2_fence_derives_tmpwt_and_fork_point_itself(tmp_path: Path) -> None:
    """The round-5 contract, executed (#2141).

    The fence runs in a shell where ``TMPWT`` and ``FORK_POINT`` are unset —
    only ``CW_SESSION`` and ``HOME`` carry over — because shell state does not
    persist between fenced ``Bash`` calls. Round 4 left both of them read on the
    fence's first line and defined (``TMPWT``) or never defined (``FORK_POINT``)
    below it, so the "self-contained" block still depended on the setup fence.

    ``_PROBE_FILE`` in the touched-files scratch output is the proof that
    ``FORK_POINT`` was computed here: it is the one file the branch adds past
    ``origin/main``, so an empty (or errored) diff means the merge-base never
    ran.
    """
    result = _run_gate2_fence(tmp_path, session_copy=GUARD_MARKER_CURRENT)
    assert result.returncode == 0, result.stderr
    assert _INVOKED in result.stdout
    session_wt = (tmp_path / "session-wt").resolve()
    assert str(session_wt / ".claude" / "scripts" / _GATE2_SCRIPT) in result.stdout
    assert f"--plan {session_wt / '.cw' / 'plan.md'}" in result.stdout
    assert _PROBE_FILE in result.stdout, (
        "the touched-files diff is empty: FORK_POINT was not derived in-fence"
    )


def test_gate2_fence_hard_stops_when_the_gate_worktree_is_missing(
    tmp_path: Path,
) -> None:
    """No ``$TMPWT`` on disk → exit 3, never a silent skip (#2141 round 5).

    Every ``git -C "$TMPWT"`` below would fail one at a time and leave the gate
    looking like it ran, so the fence checks the directory up front.
    """
    result = _run_gate2_fence(
        tmp_path, session_copy=GUARD_MARKER_CURRENT, gate_worktree=False
    )
    assert result.returncode == 3, result.stdout
    assert _INVOKED not in result.stdout
    assert "gate worktree" in result.stdout
    assert "missing" in result.stdout


def test_gate2_fence_hard_stops_when_the_touched_file_diff_fails(
    tmp_path: Path,
) -> None:
    """A failed ``git diff`` must not hand the gate an empty file set (#2141 round 7).

    The extraction piped ``git diff --name-only`` straight into ``sort``.
    Without ``pipefail`` a pipeline's status is its *last* command's, so a
    failing diff still left ``sort`` at 0 and wrote an empty touched-files
    list — whereupon the scope-conformance script saw zero delivered files and
    the approved file-set gate passed vacuously. Same class as the merge-base
    failure this fence already hard-stops on.
    """
    result = _run_gate2_fence(
        tmp_path, session_copy=GUARD_MARKER_CURRENT, break_git_diff=True
    )
    assert result.returncode == 3, result.stdout
    assert _INVOKED not in result.stdout, (
        "the scope-conformance gate ran against an empty file set"
    )
    assert "git diff --name-only failed" in result.stdout


def test_gate2_fence_hard_stops_when_the_session_worktree_is_absent(
    tmp_path: Path,
) -> None:
    """No worktree on the branch → exit 3, not a fall-through to the gate
    worktree or to ``$HOME`` (#2141 round 4)."""
    result = _run_gate2_fence(
        tmp_path, session_worktree=False, global_copy=GUARD_MARKER_CURRENT
    )
    assert result.returncode == 3, result.stdout
    assert _INVOKED not in result.stdout
    assert "cannot locate cw session worktree" in result.stdout


def test_gate2_fence_hard_stops_when_the_plan_file_is_missing(
    tmp_path: Path,
) -> None:
    """A resolved current script with no ``.cw/plan.md`` must not be invoked
    with a path that does not exist (#2141 round 4)."""
    result = _run_gate2_fence(tmp_path, session_copy=GUARD_MARKER_CURRENT, plan=False)
    assert result.returncode == 3, result.stdout
    assert _INVOKED not in result.stdout
    assert "plan.md not found" in result.stdout


@pytest.mark.parametrize("location", _GATE2_LOCATIONS)
@pytest.mark.parametrize(("label", "script_body"), GUARD_MARKER_BAD_CASES)
def test_gate2_fence_hard_stops_without_invoking(
    tmp_path: Path, location: str, label: str, script_body: str
) -> None:
    """The round-1..3 marker gate survives the round-4 anchor rewrite (#2141)."""
    result = _run_gate2_fence(tmp_path, **_gate2_placement(location, script_body))
    assert result.returncode != 0, f"{location}/{label}: fence fell through"
    assert _INVOKED not in result.stdout, f"{location}/{label}: script was reached"
    assert "STALE:" in result.stdout


@pytest.mark.parametrize("location", _GATE2_LOCATIONS)
@pytest.mark.parametrize(("label", "script_body"), GUARD_MARKER_GOOD_CASES)
def test_gate2_fence_reaches_the_script_on_a_current_marker(
    tmp_path: Path, location: str, label: str, script_body: str
) -> None:
    """Companion to the stale cases: the guard must not block the happy path."""
    result = _run_gate2_fence(tmp_path, **_gate2_placement(location, script_body))
    assert result.returncode == 0, result.stderr
    assert _INVOKED in result.stdout
    assert _gate2_candidate_path(tmp_path, location) in result.stdout, (
        f"{location}/{label}: resolved a different copy: {result.stdout!r}"
    )
    assert "STALE:" not in result.stdout


def test_gate2_fence_prefers_the_session_worktree_copy(tmp_path: Path) -> None:
    """Session-worktree copy wins over the global one — and is not exempt from
    the marker gate (#2141)."""
    current_local = _run_gate2_fence(
        tmp_path / "a",
        session_copy=GUARD_MARKER_CURRENT,
        global_copy=GUARD_MARKER_STALE,
    )
    assert current_local.returncode == 0, current_local.stderr
    assert _INVOKED in current_local.stdout
    assert (
        _gate2_candidate_path(tmp_path / "a", "session_worktree")
        in current_local.stdout
    ), "the global copy won over the session-worktree one"
    assert "STALE:" not in current_local.stdout

    stale_local = _run_gate2_fence(
        tmp_path / "b",
        session_copy=GUARD_MARKER_STALE,
        global_copy=GUARD_MARKER_CURRENT,
    )
    assert stale_local.returncode != 0, "a stale session-worktree copy was rescued"
    assert _INVOKED not in stale_local.stdout
    assert "STALE:" in stale_local.stdout


def test_gate2_fence_skips_when_absent_from_both_locations(tmp_path: Path) -> None:
    """Absent from both keeps gate 2's non-blocking skip disposition (#2141)."""
    result = _run_gate2_fence(tmp_path)
    assert result.returncode == 0, result.stderr
    assert _INVOKED not in result.stdout
    assert "STALE:" not in result.stdout


def test_gate2_fence_resolves_a_session_worktree_on_another_branch(
    tmp_path: Path,
) -> None:
    """The branch name is not the only handle on the session worktree (#2141 round 8).

    ``isolation: "worktree"`` provisions the session on an auto-generated
    ``agent-<hash>`` branch, and ``auto-dev-impl.md`` says so in its own branch-
    discipline bullet — yet gate 2 looked the worktree up solely by
    ``branch refs/heads/<branch-name>``. Nothing is checked out on that ref here,
    so a branch-only lookup finds nothing and the gate falsely hard-stops on a
    correct implementation. ``.claude/cw-context.json``'s ``ticket_id`` is the
    handle that survives the rename.
    """
    result = _run_gate2_fence(
        tmp_path,
        session_copy=GUARD_MARKER_CURRENT,
        session_branch=_GATE2_LOCAL_BRANCH,
        context_ticket=_GATE2_TICKET,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _INVOKED in result.stdout
    session_wt = (tmp_path / "session-wt").resolve()
    assert str(session_wt / ".claude" / "scripts" / _GATE2_SCRIPT) in result.stdout
    assert f"--plan {session_wt / '.cw' / 'plan.md'}" in result.stdout


def test_gate2_fence_falls_back_to_the_branch_keyed_lookup(tmp_path: Path) -> None:
    """No ``cw-context.json`` → the pre-round-8 branch lookup still resolves it.

    The context file is gitignored, so a worktree provisioned outside cw (or one
    whose context file was cleaned up) carries none. Round 8 adds a primary
    lookup; it must not remove the one that worked.
    """
    result = _run_gate2_fence(tmp_path, session_copy=GUARD_MARKER_CURRENT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _INVOKED in result.stdout
    session_wt = (tmp_path / "session-wt").resolve()
    assert str(session_wt / ".claude" / "scripts" / _GATE2_SCRIPT) in result.stdout


def test_gate2_fence_hard_stops_when_neither_lookup_resolves(tmp_path: Path) -> None:
    """Both handles missing → exit 3, not a fall-through to ``$TMPWT``.

    The session worktree is on a foreign branch *and* carries no context file,
    so neither the ticket-keyed probe nor the branch-keyed fallback can name it.
    Adding a second lookup must not soften the hard stop when both come up empty.
    """
    result = _run_gate2_fence(
        tmp_path,
        session_copy=GUARD_MARKER_CURRENT,
        session_branch=_GATE2_LOCAL_BRANCH,
    )
    assert result.returncode == 3, result.stdout
    assert _INVOKED not in result.stdout
    assert "cannot locate cw session worktree" in result.stdout


def test_gate2_fence_ignores_a_context_file_for_another_ticket(
    tmp_path: Path,
) -> None:
    """The ticket-keyed probe must match on the id, not merely on the file.

    A sibling session's worktree also carries a ``cw-context.json``; accepting
    the first one found would resolve this gate against another ticket's
    checkout — worse than not resolving at all, because it would then run the
    file-set gate against the wrong plan.
    """
    result = _run_gate2_fence(
        tmp_path,
        session_copy=GUARD_MARKER_CURRENT,
        session_branch=_GATE2_LOCAL_BRANCH,
        context_ticket="9999",
    )
    assert result.returncode == 3, result.stdout
    assert _INVOKED not in result.stdout
    assert "cannot locate cw session worktree" in result.stdout


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
    assert '[[ ! "$FOUND_VERSION" =~ ^[0-9]{1,6}$ ]]' in template


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
            assert '[[ ! "$FOUND_VERSION" =~ ^[0-9]{1,6}$ ]]' in fence, (
                f"{doc}: the marker must be rejected unless it is a clean "
                f"1-6 digit integer — an unbounded `+` accepts a value that "
                f"then overflows `[ -lt ]` and falls open"
            )
            assert "for candidate in .claude/scripts/" not in fence, (
                f"{doc}: the repo-local candidate must be absolutely anchored"
            )
            if '"$SESSION_WT/.claude/scripts/' in fence:
                # Step 2.5 gate 2's variant: the ambient cwd there may already
                # be the detached gate worktree, so it derives its anchor from
                # the branch rather than from `git rev-parse` (#2141 round 4).
                assert 'git -C "$TMPWT" worktree list' in fence, (
                    f"{doc}: a $SESSION_WT anchor must be derived in-fence from "
                    f"`git worktree list`, keyed on the branch"
                )
                assert "rev-parse" not in fence, (
                    f"{doc}: a $SESSION_WT anchor must carry no ambient-cwd fallback"
                )
                continue
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
