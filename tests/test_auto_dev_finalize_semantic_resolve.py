"""Guard tests: semantic auto-resolve for post-push merge conflicts (#1850).

Pure-markdown assertions over ``.claude/commands/auto-dev-finalize.md`` Step
4c.5 and ``.claude/commands/prep-pr.md`` Step 1, plus one schema assertion.
Follows this repo's established convention (see
``tests/test_auto_dev_finalize_early_push.py`` and its four siblings) of
reading the prose and asserting substrings/regions. The reader itself is the
shared ``_cmd()`` helper imported from ``tests.conftest`` (#1787) — it used to
be a private per-file copy here.

What is pinned here:

1. Step 4c.5 gains a narrowly-scoped semantic auto-resolve attempt that runs
   *before* the park, delegating the actual decision to the deterministic
   ``classify_merge_conflict.py`` gate script.
2. The existing ``merge_conflict_post_push`` sentinel template is byte-shape
   unchanged — the new step only ever appends a clause to ``blocker.details``.
3. The attempt is terminal: exactly one resolver invocation, no retry after a
   gate failure, and no concurrency/mutex machinery smuggled in alongside.
4. ``prep-pr.md`` Step 1's *pre-push* refusal to auto-resolve is untouched —
   this ticket governs the post-push site only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cw.auto_dev_result.schema import FINALIZE_REGRESS_BLOCKER_REASONS
from tests.conftest import (
    GUARD_FENCE_INVOKED,
    _appendix,
    _bash_fences,
    _cmd,
    _placement,
    run_guard_fence,
)

_SECTION_HEADING = "Semantic auto-resolve attempt (operator direction, #1850)"
_TEMPLATE_HEADING = "**Sentinel template — `merge_conflict_post_push` blocker:**"

_SCRIPT = "classify_merge_conflict.py"

# Sentinel standing in for the real resolver invocation, so the executable
# fence test can observe *whether* the fence reached it without running it.
_INVOKED = GUARD_FENCE_INVOKED

# The one marker value that may reach the invocation (the version table's
# minimum for this script).
_CURRENT_MARKER = "# cw-script-version: 1\n"


def _finalize() -> str:
    return _cmd("auto-dev-finalize.md")


def _semantic_resolve_section() -> str:
    content = _finalize()
    start = content.index(_SECTION_HEADING)
    end = content.index(_TEMPLATE_HEADING, start)
    return content[start:end]


def _stale_guard_fence() -> str:
    """The Step 4c.5 resolver fence — the canonical stale-guard shape (#2141)."""
    # Anchor on the resolver probe, not a bare script mention: the sibling
    # commit fence names the script too, in its `Auto-Resolved-By` trailer.
    fences = [
        fence
        for fence in _bash_fences(_semantic_resolve_section())
        if f"/.claude/scripts/{_SCRIPT}" in fence and "for candidate in" in fence
    ]
    assert len(fences) == 1, f"expected exactly one resolver fence, got {len(fences)}"
    return fences[0]


def _run_stale_guard(
    tmp_path: Path,
    *,
    repo_local: str | None = None,
    global_copy: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute the doc's own fence against fixture copies (#2141).

    Thin wrapper over the shared ``run_guard_fence`` runner, which plants the
    fixture at either candidate location and runs the fence from a nested
    subdirectory so an anchoring regression is visible.
    """
    return run_guard_fence(
        tmp_path,
        _stale_guard_fence(),
        _SCRIPT,
        repo_local=repo_local,
        global_copy=global_copy,
    )


@pytest.mark.parametrize("location", ["repo_local", "global_only"])
def test_stale_guard_fence_reaches_the_resolver_on_a_current_marker(
    tmp_path: Path, location: str
) -> None:
    """A marker at the table minimum is the happy path: the fence invokes.

    ``global_only`` is the branch this ticket exists for — a client repo with
    no local ``.claude/scripts/`` relying on the ``install-skills.sh`` symlink
    (#2096) — and it never ran before this round's parametrization.
    """
    result = _run_stale_guard(tmp_path, **_placement(location, _CURRENT_MARKER))
    assert result.returncode == 0, f"{location}: {result.stderr}"
    assert _INVOKED in result.stdout
    assert "STALE:" not in result.stdout


@pytest.mark.parametrize("location", ["repo_local", "global_only"])
@pytest.mark.parametrize(
    ("label", "script_body"),
    [
        ("below_minimum", "# cw-script-version: 0\n"),
        ("no_marker", "import sys\n"),
        ("malformed_float", "# cw-script-version: 1.5\n"),
        ("malformed_alpha", "# cw-script-version: abc\n"),
        ("malformed_empty", "# cw-script-version:\n"),
        # An unbounded `^[0-9]+$` accepts this, and `[ -lt ]` then errors with
        # "integer expression expected", evaluates false, and runs the resolver
        # anyway — the round-2 fail-open one width up (#2141 round 5).
        ("malformed_oversized", "# cw-script-version: 99999999999999999999\n"),
    ],
)
def test_stale_guard_fence_hard_stops_without_invoking(
    tmp_path: Path, location: str, label: str, script_body: str
) -> None:
    """Executable proof that a stale hit cannot reach the resolver (#2141).

    The sibling text assertions pin that the prose *says* HEADLESS BLOCK; this
    one runs the fence the worker actually copies and proves the shell itself
    stops. A fence that only echoes ``STALE:`` and falls through would exit 0
    here — which is exactly the review finding this closes. The malformed
    cases are the round-2 finding: a non-integer marker made the numeric
    comparison error out, and the guard failed open.
    """
    result = _run_stale_guard(tmp_path, **_placement(location, script_body))
    assert result.returncode != 0, f"{location}/{label}: fence fell through with exit 0"
    assert _INVOKED not in result.stdout, f"{location}/{label}: resolver was reached"
    assert "STALE:" in result.stdout


def test_stale_guard_fence_skips_when_absent_from_both_locations(
    tmp_path: Path,
) -> None:
    """Absence is a separate branch: no invocation, and no shell hard stop.

    "Not invoked" alone is not the contract (#2141 round 5). Unlike this
    pipeline's other guard sites, absence here is *not* "continue
    non-blocking": the required fallback is the documented
    ``merge_conflict_post_push`` escalation, so this pins both halves — the
    fence leaves ``$RESOLVE_OUTPUT`` unset (nothing downstream can mistake a
    nonexistent resolver for a refusal verdict), and the section's absent
    bullet still routes to that sentinel via ``git merge --abort``.
    """
    result = _run_stale_guard(tmp_path)
    assert result.returncode == 0, result.stderr
    assert _INVOKED not in result.stdout
    assert "STALE:" not in result.stdout
    # The runner echoes `${VERDICT-}${RESOLVE_OUTPUT-}${SCOPE_CONFORMANCE_OUTPUT-}`
    # after the fence, so a populated capture variable would show up here.
    assert result.stdout.strip() == "", (
        f"the absent branch must capture no resolver output: {result.stdout!r}"
    )

    bullets = [
        line
        for line in _semantic_resolve_section().splitlines()
        if "Absent from both locations" in line
    ]
    assert len(bullets) == 1, f"expected one absent-disposition bullet, got {bullets}"
    absent = bullets[0]
    assert "classify_merge_conflict: script absent, skipped" in absent
    assert "git merge --abort" in absent
    assert "merge_conflict_post_push" in absent, (
        "the absent branch must name the escalation sentinel it falls through to"
    )


def test_semantic_resolve_section_inserted_before_blocker_template() -> None:
    content = _finalize()
    rebase_idx = content.index("**Single auto-rebase attempt (no loops):**")
    section_idx = content.index(_SECTION_HEADING)
    template_idx = content.index(_TEMPLATE_HEADING)
    assert rebase_idx < section_idx < template_idx


def test_rebase_fallthrough_comment_retargeted_to_semantic_resolve() -> None:
    content = _finalize()
    assert (
        "# If rebase fails with conflicts here → abort and emit blocker" not in content
    )
    assert (
        "# If rebase fails with conflicts here → abort and attempt semantic"
        " auto-resolve (see below)" in content
    )


def test_blocker_template_json_shape_unchanged() -> None:
    """#1879 relocated the template's JSON body to
    ``auto-dev-finalize-appendix.md`` — it is reached only when both the
    auto-rebase and the semantic auto-resolve failed or were refused, so it is
    rare-path. The ``_TEMPLATE_HEADING`` marker stays in the core doc (it is
    also the end anchor of the indivisible #1850 semantic-resolve section,
    which did not move), and the JSON shape is asserted at its new home.
    """
    template = _appendix("finalize")
    assert _TEMPLATE_HEADING in _finalize()
    for field in (
        '"stage": "stage5_post_create"',
        '"reason": "merge_conflict_post_push"',
        '"exception_type": null',
        '"message": "PR is open but conflicts with main; auto-rebase failed"',
        '"retry_eligible": true',
        '"retry_delay_seconds": null',
        '"next_actions": ["manual_intervention"]',
    ):
        assert field in template
    assert (
        '"details": "PR #<N> opened with conflicts after sibling merges to'
        " origin/main between /prep-pr's sync-with-main and PR open. One"
        ' auto-rebase attempted and failed; conflicted files: <list>"' in template
    )


def test_refuse_path_falls_through_to_existing_blocker_unchanged() -> None:
    section = _semantic_resolve_section()
    assert "git merge --abort" in section
    assert "semantic auto-resolve attempted — refused" in section
    assert "merge_conflict_post_push" in section
    assert "no new `blocker.reason`" in section


def test_gate_failure_reverts_and_parks() -> None:
    section = _semantic_resolve_section()
    assert "PRE_MERGE_SHA" in section
    assert "git reset --hard $PRE_MERGE_SHA" in section
    assert "reverted" in section
    assert '"$PREP_PR_STATE" detect-gates' in section


def test_gate_run_is_foreground_no_fix_loop() -> None:
    section = _semantic_resolve_section()
    assert "no autofix" in section
    assert "no fix loop" in section
    assert "no backgrounding" in section


def test_semantic_resolve_push_is_plain_not_forced() -> None:
    section = _semantic_resolve_section()
    assert "git push origin HEAD:refs/heads/<branch-name>" in section
    assert "--force-with-lease" not in section
    assert "Step 4c.5 semantic-resolve push" in section


def test_success_path_records_friction_highlight() -> None:
    section = _semantic_resolve_section()
    assert "semantic_merge_conflict_auto_resolved" in section
    assert "friction_highlights" in section


def test_no_finalize_regress_blocker_reasons_change() -> None:
    assert frozenset({"agent_block"}) == FINALIZE_REGRESS_BLOCKER_REASONS


def test_no_mutex_or_concurrency_language_introduced() -> None:
    section = _semantic_resolve_section().lower()
    for banned in ("mutex", "finalize-slot", "serialize"):
        assert banned not in section


def test_prep_pr_step1_merge_conflict_refusal_untouched() -> None:
    content = _cmd("prep-pr.md")
    assert "Do NOT attempt an autonomous conflict resolution" in content


def test_classify_merge_conflict_script_referenced_repo_relative() -> None:
    """The script resolves repo-local-then-global-then-marker-verified (#2141).

    ``install-skills.sh`` has symlinked this script into ``~/.claude/scripts/``
    since #2096, so the old repo-relative-only invocation silently no-opped in
    a client repo with no local ``.claude/scripts/``. The repo copy still wins
    resolution order; the global copy is a real fallback candidate, and either
    one must carry a current ``cw-script-version`` marker before it is run.
    """
    section = _semantic_resolve_section()
    assert ".claude/scripts/classify_merge_conflict.py" in section
    assert '"$HOME/.claude/scripts/classify_merge_conflict.py"' in section
    assert 'uv run python "$RESOLVED" resolve' in section


def test_gate_failure_park_is_terminal_no_retry() -> None:
    """Exactly one resolver invocation and one gate-detection invocation in
    the whole file, plus an explicit instruction covering the gate step
    itself — not just the resolver — never being retried.

    #2141 moved the invocation onto the resolved path (``"$RESOLVED"``), so the
    counted literal follows it; the "exactly one invocation" intent is
    unchanged.
    """
    section = _semantic_resolve_section()
    assert _finalize().count('uv run python "$RESOLVED" resolve') == 1
    assert section.count('"$PREP_PR_STATE" detect-gates') == 1
    assert "do NOT re-run the gate" in section


def test_semantic_resolve_resolves_repo_local_then_global_script_path() -> None:
    """Both candidates present, repo-local first (#2141)."""
    section = _semantic_resolve_section()
    repo_idx = section.index(".claude/scripts/classify_merge_conflict.py")
    global_idx = section.index('"$HOME/.claude/scripts/classify_merge_conflict.py"')
    assert repo_idx < global_idx


def test_semantic_resolve_absent_from_both_locations_skips_with_labeled_friction() -> (
    None
):
    """Absent still escalates to human review — honestly labelled (#2141).

    Unlike the other three guard sites, absence here is NOT "continue the
    pipeline": a conflict the resolver never ran on was always going to reach
    ``merge_conflict_post_push``. What changes is the label — today a missing
    file exits 2 and is misattributed as a "refused" classification.
    """
    section = _semantic_resolve_section()
    assert "classify_merge_conflict: script absent, skipped" in section
    assert "merge_conflict_post_push" in section


def test_semantic_resolve_greps_version_marker_and_headless_blocks_on_stale() -> None:
    """A stale resolver is a tooling-integrity failure, not a merge outcome.

    It must NOT fold into ``merge_conflict_post_push`` alongside genuine
    refusals: a copy of the script that predates the current contract cannot
    be trusted to have classified the conflict at all (#2141).
    """
    section = _semantic_resolve_section()
    assert "cw-script-version" in section
    assert "HEADLESS BLOCK" in section
    stale_lines = [line for line in section.splitlines() if "HEADLESS BLOCK" in line]
    assert stale_lines
    assert any('blocker.reason: "agent_block"' in line for line in stale_lines)
    assert all("merge_conflict_post_push" not in line for line in stale_lines)


def test_resolver_table_referenced_not_duplicated() -> None:
    """Step 4c.5 cross-references auto-dev-impl.md's table, never re-embeds it."""
    section = _semantic_resolve_section()
    assert "Guard-script path resolution and staleness marker" in section
    for other in (
        "check_not_main_checkout.py",
        "check_plan_scope_conformance.py",
        "check_impl_guard_staleness.py",
    ):
        assert other not in section


def test_grouping_comment_no_longer_claims_repo_only_install() -> None:
    """Both clauses of the #1850 comment went stale when #2096 landed (#2141).

    "never to the global ~/.claude/scripts/" became false, and the "Same
    convention as ..." framing inverted — it is now the same convention, which
    is precisely why the claim of difference has to go.
    """
    content = _finalize()
    assert "never to the global" not in content
    assert (
        "Same convention as `check_impl_guard_staleness.py`"
        "/`check_plan_scope_conformance.py`" not in content
    )
