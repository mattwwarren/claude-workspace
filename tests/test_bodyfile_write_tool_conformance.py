"""Guard tests: Write-tool-authored gh body files, single source of truth (#1833).

Six call sites tell the agent to post/write a `gh` body via a file path (or,
for `ship-it.md`, inline a heredoc-composed body directly into the same Bash
call) but never say the file itself must be authored with the Write tool, not
Bash heredoc/`cat`/redirection. The substantive "author via Write tool, not
heredoc" instruction lives in exactly one place -- a new paragraph appended to
this repo's own CLAUDE.md, extending its existing "## Agent File Operations"
section. Each of the six sites carries only a one-line pointer to that
section plus its own site-specific mechanics -- no site restates the
argument for why Write tool beats heredoc.

Pure-markdown assertions, following the ``read_text()`` + literal-substring
convention of ``test_harden_ticket_positioning.py`` and
``test_auto_dev_preflight_resolutions.py``.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
SKILLS = ROOT / ".claude" / "skills"
COMMANDS = ROOT / ".claude" / "commands"

POINTER = "via the **Write tool** — see CLAUDE.md's **Agent File Operations** rule"

# The six convert-site files, per the plan's file list.
CONVERT_SITES = {
    "harden-ticket/SKILL.md": SKILLS / "harden-ticket" / "SKILL.md",
    "cw-followup/SKILL.md": SKILLS / "cw-followup" / "SKILL.md",
    "sprint-buildout/SKILL.md": SKILLS / "sprint-buildout" / "SKILL.md",
    "auto-dev-finalize.md": COMMANDS / "auto-dev-finalize.md",
    "ship-it.md": COMMANDS / "ship-it.md",
    "handoff.md": COMMANDS / "handoff.md",
}

# Substantive-argument phrases that must live ONLY in CLAUDE.md, never restated
# at a convert site.
RESTATED_ARGUMENT_PHRASES = (
    "never inline it into the same Bash call via heredoc",
    "mirrors `.cw/plan.md`'s disk-reference pattern",
    "do NOT use Bash `cat`/`>>`",
)


def _claude_md() -> str:
    return CLAUDE_MD.read_text()


def _skill(name: str) -> str:
    return (SKILLS / name).read_text()


def _cmd(name: str) -> str:
    return (COMMANDS / name).read_text()


def test_claude_md_agent_file_operations_gains_body_rule() -> None:
    """The new paragraph names all four gh subcommands and the mechanism rule."""
    content = _claude_md()
    section_start = content.index("## Agent File Operations")
    table_end = content.index("| Read file |", section_start)
    rule_window = content[table_end:]

    for token in (
        "gh pr create",
        "gh issue comment",
        "gh issue edit",
        "gh pr edit",
        "--body-file",
        "Write tool",
        "Never heredoc-inline",
        "$(cat",
    ):
        assert token in rule_window, f"missing {token!r} after the existing table"


def test_claude_md_existing_table_rows_unchanged() -> None:
    """The pre-existing 4-row table survives the addition verbatim."""
    content = _claude_md()
    assert (
        "| Copy file | `Read` source then `Write` destination | `Bash(cp ...)` |"
        in content
    )
    assert (
        "| Move file | `Read` then `Write` then `Bash(rm)` | `Bash(mv ...)` |"
        in content
    )
    assert "| Create file | `Write` | `Bash(echo >)` |" in content
    assert "| Read file | `Read` | `Bash(cat)` |" in content


def test_harden_ticket_body_file_uses_write_tool() -> None:
    """A one-line pointer precedes the existing 'Post it to the issue' line."""
    content = _skill("harden-ticket/SKILL.md")
    pointer_idx = content.index(POINTER)
    post_idx = content.index("Post it to the issue")
    assert pointer_idx < post_idx

    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in content


def test_sprint_buildout_rationale_uses_write_tool() -> None:
    """A pointer sits near the existing --body-file <scratch>/rationale.md line."""
    content = _skill("sprint-buildout/SKILL.md")
    assert "Author `<scratch>/rationale.md` " + POINTER in content
    pointer_idx = content.index(POINTER)
    body_file_idx = content.index("--body-file <scratch>/rationale.md")
    assert body_file_idx < pointer_idx

    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in content


def test_handoff_write_uses_write_tool() -> None:
    """The handoff.md write-location bullet gains the Write-tool pointer."""
    content = _cmd("handoff.md")
    assert POINTER in content
    write_idx = content.index("Write to `.handoffs/`")
    pointer_idx = content.index(POINTER)
    assert write_idx < pointer_idx

    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in content


def test_cw_followup_anti_pattern_removed() -> None:
    """The old redirect-then-append anti-pattern lines are gone."""
    content = _skill("cw-followup/SKILL.md")
    assert "> /tmp/body.md" not in content
    assert "cat /tmp/decisions.md >> /tmp/body.md" not in content


def test_cw_followup_replacement_uses_write_tool() -> None:
    """The replacement instructs the Write tool, preceding --body-file post."""
    content = _skill("cw-followup/SKILL.md")
    assert "Use the **Write tool**" in content
    assert "CLAUDE.md's **Agent File Operations** rule" in content
    write_idx = content.index("Use the **Write tool**")
    post_idx = content.index("gh issue edit", write_idx)
    assert "--body-file" in content[post_idx : post_idx + 200]

    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in content


def test_auto_dev_finalize_body_file_uses_write_tool() -> None:
    """The pointer precedes the gh pr edit --body-file line."""
    content = _cmd("auto-dev-finalize.md")
    pointer_idx = content.index(POINTER)
    body_file_idx = content.index("gh pr edit <pr-number> --body-file")
    assert pointer_idx < body_file_idx

    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in content


def test_auto_dev_finalize_stdin_form_removed() -> None:
    """The stdin `--body-file -` form is replaced with a scratch path."""
    content = _cmd("auto-dev-finalize.md")
    assert "--body-file -`:" not in content
    assert "--body-file .cw/pr-body.md" in content


def test_ship_it_body_uses_write_tool() -> None:
    """The pointer precedes the gh pr create bash block in Step 3."""
    content = _cmd("ship-it.md")
    step3_idx = content.index("## Step 3: Create the PR")
    pointer_idx = content.index(POINTER, step3_idx)
    gh_create_idx = content.index("gh pr create", pointer_idx)
    assert step3_idx < pointer_idx < gh_create_idx

    for phrase in RESTATED_ARGUMENT_PHRASES:
        assert phrase not in content


def test_ship_it_heredoc_anti_pattern_removed() -> None:
    """The cat <<EOF heredoc (any spacing/quoting variant) is gone, replaced
    by a --body-file scratch path."""
    content = _cmd("ship-it.md")
    assert re.search(r"cat\s*<<-?\s*['\"]?EOF", content) is None
    assert "--body-file .cw/pr-body.md" in content


def test_ship_it_allowed_tools_includes_write() -> None:
    """Frontmatter widens allowed-tools to include Write."""
    content = _cmd("ship-it.md")
    assert 'allowed-tools: ["Bash", "Read", "Write"]' in content
    assert 'allowed-tools: ["Bash", "Read"]' not in content


def test_no_site_restates_write_tool_rationale() -> None:
    """None of the six convert sites restate the CLAUDE.md rationale."""
    for name, path in CONVERT_SITES.items():
        content = path.read_text()
        for phrase in RESTATED_ARGUMENT_PHRASES:
            assert phrase not in content, f"{name} restates {phrase!r}"
