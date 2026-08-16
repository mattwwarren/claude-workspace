"""Guard tests: session-handoff.md compact-repr pass (#1833, spirit of #839).

`.claude/agents/session-handoff.md`'s rendered handoff templates get a
compact-repr pass -- cutting narrative duplication (a "Handoff Scenarios"
section that restates "Handoff Methodology," and an "Integration Points"
table that restates the same situation->command mapping as
`.claude/commands/handoff.md`'s own "When to Use" table).

Pure-markdown assertions, following the ``read_text()`` + literal-substring
convention of ``test_harden_ticket_positioning.py``.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
AGENTS = ROOT / ".claude" / "agents"
COMMANDS = ROOT / ".claude" / "commands"


def _agent() -> str:
    return (AGENTS / "session-handoff.md").read_text()


def _handoff_cmd() -> str:
    return (COMMANDS / "handoff.md").read_text()


def _section(content: str, heading: str, next_heading_prefix: str = "## ") -> str:
    """Slice from `heading` to the next heading at the same or higher level."""
    start = content.index(heading)
    rest_start = start + len(heading)
    end = len(content)
    idx = content.find(f"\n{next_heading_prefix}", rest_start)
    if idx != -1:
        end = idx
    return content[start:end]


def test_compact_repr_rule_present() -> None:
    """A Compact-Repr Rule heading exists, citing #839, pointer not re-narration."""
    content = _agent()
    assert "## Compact-Repr Rule" in content
    section = _section(content, "## Compact-Repr Rule")
    assert "#839" in section
    assert "pointer" in section
    assert "not a re-narration" in section


def test_handoff_scenarios_no_longer_duplicates_actions() -> None:
    """The per-scenario 'Actions:' numbered-list duplication is gone."""
    content = _agent()
    section = _section(content, "## Handoff Scenarios")
    assert "**Actions:**" not in section


def test_integration_points_table_deduped() -> None:
    """The situation->command table is removed from session-handoff.md,
    while handoff.md still carries its own unchanged row."""
    agent_content = _agent()
    assert "| Work complete | `/session-done` |" not in agent_content

    cmd_content = _handoff_cmd()
    assert "| Work complete | Use `/session-done` instead |" in cmd_content


def test_output_format_templates_still_carry_required_fields() -> None:
    """The compaction pass cut duplication, not content -- required fields survive."""
    content = _agent()
    for field in (
        "## Completed",
        "## In Progress",
        "## Blocked",
        "## Resume Prompt",
        "### Critical Files",
    ):
        assert field in content


def test_tools_frontmatter_unchanged() -> None:
    """Frontmatter tools list is untouched -- content-compaction only."""
    content = _agent()
    assert "tools: [Read, Write, Grep, Glob]" in content
