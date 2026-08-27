"""Shared optional-file read primitive for the prompt-context submodules.

A dependency-free leaf module rather than a member of any one concern:
``_agent_spec``, ``_sensitive_files``, ``_repo_config`` and ``core`` all read
optional repo files this way, and folding the helper into one of them would
make the other three depend on that one's concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _load_optional_text(path: Path) -> str | None:
    """Return *path*'s text, or ``None`` if absent/unreadable/not UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
