"""Writers for skill, command and context files under a test's fake
``~/.claude`` tree, plus the skill-listing record builder. Moved from
``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping


def write_skill(claude_dir: Path, name: str, description: str | None) -> Path:
    """Write ``<claude_dir>/skills/<name>/SKILL.md`` with YAML frontmatter.

    ``description=None`` omits the ``description`` key entirely.
    """
    lines = ["---", f"name: {name}"]
    if description is not None:
        dumped = yaml.safe_dump(description, default_flow_style=True, width=10**9)
        lines.append(f"description: {dumped.splitlines()[0]}")
    lines += ["---", "body", ""]
    path = claude_dir / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_command(claude_dir: Path, name: str, description: str | None) -> Path:
    """Write ``<claude_dir>/commands/<name>.md``.

    ``description=None`` writes a body with no frontmatter block at all.
    """
    if description is None:
        text = "body\n"
    else:
        dumped = yaml.safe_dump(description, default_flow_style=True, width=10**9)
        text = f"---\ndescription: {dumped.splitlines()[0]}\n---\nbody\n"
    path = claude_dir / "commands" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_context_files(
    home: Path,
    *,
    claude_md: str = "",
    local_md: str = "",
    rules: Mapping[str, str] | None = None,
) -> None:
    """Write the user-global context files under ``<home>/.claude``.

    A file is written only when its argument is non-empty, so a missing file
    can be distinguished from an empty one.
    """
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    if claude_md:
        (claude_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    if local_md:
        (claude_dir / "CLAUDE.local.md").write_text(local_md, encoding="utf-8")
    for rel, text in (rules or {}).items():
        rule_path = claude_dir / "rules" / f"{rel}.md"
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(text, encoding="utf-8")


def skill_listing_record(
    content: str, skill_count: int, *, is_initial: bool = True
) -> dict[str, Any]:
    """Build the transcript record carrying a ``skill_listing`` attachment.

    ``dict[str, Any]`` is the harness's JSONL wire format being faked, the
    same type ``tests._queue_peek_helpers.make_transcript`` takes.
    """
    return {
        "type": "attachment",
        "attachment": {
            "type": "skill_listing",
            "content": content,
            "skillCount": skill_count,
            "isInitial": is_initial,
            "names": [],
            "digests": [],
        },
    }
