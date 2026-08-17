"""Guard tests: semantic auto-resolve for post-push merge conflicts (#1850).

Pure-markdown assertions over ``.claude/commands/auto-dev-finalize.md`` Step
4c.5 and ``.claude/commands/prep-pr.md`` Step 1, plus one schema assertion.
Follows this repo's established convention (see
``tests/test_auto_dev_finalize_early_push.py`` and its four siblings) of a
small **private per-file** ``_cmd()``-style helper that reads the prose and
asserts substrings/regions, rather than a shared ``conftest.py`` fixture.

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

from pathlib import Path

from cw.auto_dev_result.schema import FINALIZE_REGRESS_BLOCKER_REASONS

ROOT = Path(__file__).parent.parent
COMMANDS = ROOT / ".claude" / "commands"

_SECTION_HEADING = "Semantic auto-resolve attempt (operator direction, #1850)"
_TEMPLATE_HEADING = "**Sentinel template — `merge_conflict_post_push` blocker:**"


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def _finalize() -> str:
    return _cmd("auto-dev-finalize.md")


def _semantic_resolve_section() -> str:
    content = _finalize()
    start = content.index(_SECTION_HEADING)
    end = content.index(_TEMPLATE_HEADING, start)
    return content[start:end]


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
    content = _finalize()
    template = content[content.index(_TEMPLATE_HEADING) :]
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
    assert "prep_pr_state.py detect-gates" in section


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
    """The new script ships only to this repo's .claude/scripts/ — never to
    the global ~/.claude/scripts/ — so it must be invoked repo-relative via
    `uv run python`, matching check_impl_guard_staleness.py's own convention.
    """
    section = _semantic_resolve_section()
    assert "uv run python .claude/scripts/classify_merge_conflict.py" in section
    assert "~/.claude/scripts/classify_merge_conflict.py" not in section


def test_gate_failure_park_is_terminal_no_retry() -> None:
    """Exactly one resolver invocation and one gate-detection invocation in
    the whole file, plus an explicit instruction covering the gate step
    itself — not just the resolver — never being retried."""
    section = _semantic_resolve_section()
    assert _finalize().count("classify_merge_conflict.py resolve") == 1
    assert section.count("prep_pr_state.py detect-gates") == 1
    assert "do NOT re-run the gate" in section
