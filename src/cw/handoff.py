"""Handoff document parsing for session resume."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

HANDOFF_GLOB = "session-*.md"


def find_latest_handoff(workspace_path: Path) -> Path | None:
    """Find the most recent session handoff in a workspace's .handoffs/ directory.

    Checks both workspace-level and ~/.claude/plans/ for handoff files.
    """
    candidates: list[Path] = []

    # Workspace-level handoffs
    handoffs_dir = workspace_path / ".handoffs"
    if handoffs_dir.is_dir():
        candidates.extend(handoffs_dir.glob(HANDOFF_GLOB))

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_handoffs_newer_than(workspace_path: Path, since_mtime: float) -> list[Path]:
    """Find handoff files newer than a given mtime.

    Used by background_session to detect newly generated handoffs.
    """
    handoffs_dir = workspace_path / ".handoffs"
    if not handoffs_dir.is_dir():
        return []

    # Cache stat results to avoid double stat() per file
    timed = [
        (p, p.stat().st_mtime)
        for p in handoffs_dir.glob(HANDOFF_GLOB)
    ]
    return [
        p for p, mtime in sorted(timed, key=lambda t: t[1], reverse=True)
        if mtime > since_mtime
    ]


def extract_resumption_prompt(handoff_path: Path) -> str | None:
    """Extract the resumption prompt from a handoff document.

    Looks for the ## Resumption Prompt section and extracts the content
    from the code block within it.
    """
    try:
        content = handoff_path.read_text()
    except OSError:
        return None

    # Find the Resumption Prompt section
    section_pattern = re.compile(
        r"^## Resumption Prompt\s*\n"
        r".*?"  # Optional text between heading and code block
        r"```\s*\n"
        r"(.*?)"
        r"```",
        re.MULTILINE | re.DOTALL,
    )

    match = section_pattern.search(content)
    if match:
        return match.group(1).strip()

    return None
