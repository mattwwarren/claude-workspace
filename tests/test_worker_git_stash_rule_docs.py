"""Doc-structure guards for #1418: dispatched workers must never run a bare
``git stash`` or ``git stash pop``.

The git stash stack is one reflog shared by every worktree of the repository
and every concurrent session, so a pop applies another session's work and an
unrecovered stash silently loses the worker's own. The rule is carried on four
Claude-facing surfaces (the Worker Execution Discipline section of
``auto-dev.md``, the impl prompt list, the review fix-loop prompt, and
``CLAUDE.md``); the opencode/codex preamble and the codex fix prompt carry it as
code (see ``tests/test_opencode_runner.py`` and
``tests/test_codex_fix_loop_commit.py``). The markdown is hard-wrapped, so every
assertion runs on ``_norm``-collapsed text.
"""

from __future__ import annotations

from pathlib import Path

from tests._shared.workflow_text import _cmd, _norm, _section_between

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


def _discipline_section() -> str:
    return _norm(
        _section_between(
            _cmd("auto-dev.md"),
            "## Worker Execution Discipline",
            "## Comment provenance rule",
        )
    )


def test_auto_dev_discipline_has_rule_4_after_rule_3() -> None:
    section = _discipline_section()

    rule_3 = section.index("3. Never background a pipeline-dependent Bash call")
    rule_4 = section.index("4. Never run a bare `git stash`")
    assert rule_3 < rule_4


def test_auto_dev_discipline_rule_4_carries_the_safe_recipe() -> None:
    section = _discipline_section()

    for literal in (
        "`git stash pop`",
        "`git stash clear`",
        "temporary WIP commit",
        'git stash push -u -m "<unique-tag>"',
        "git stash list --format='%H %gs'",
        "git stash apply <sha>",
        "(never `pop`)",
        "cw guard-git-stash",
    ):
        assert literal in section, literal


def test_impl_prompt_list_carries_the_stash_bullet() -> None:
    content = _norm(_cmd("auto-dev-impl.md"))

    bullet = content.index("- Instruction never to run a bare `git stash`")
    read_write = content.index(
        "- Instruction to use Read/Write tools for file operations, not Bash cp/mv/cat"
    )
    next_bullet = content.index("- **Pre-mutation guard (hard, not prose)")
    assert read_write < bullet < next_bullet
    window = content[bullet:next_bullet]
    assert "temporary WIP commit" in window
    assert "git stash apply <sha>" in window
    assert "never `pop`" in window


def test_review_fix_loop_prompt_carries_the_stash_instruction() -> None:
    content = _norm(_cmd("auto-dev-review.md"))

    anchor = content.index("The fix-loop agent's prompt must ALSO instruct:")
    end = content.index("3b. **Orchestrator fix gate**")
    window = content[anchor:end]
    assert "Never run a bare `git stash`" in window
    assert "temporary WIP commit" in window


def test_claude_md_documents_the_shared_stash_rule() -> None:
    content = _norm(_CLAUDE_MD.read_text(encoding="utf-8"))

    assert "### Git stash is shared across worktrees (#1418)" in content
    assert "cw guard-git-stash" in content
    assert "git stash apply <sha>" in content
    assert "prompt-only" in content
