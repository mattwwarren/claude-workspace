"""Resolve a cw guard script and check its ``cw-script-version`` marker.

Stage-neutral: the codex IMPL scope-conformance gate and the codex FINALIZE
guard scripts (``check_must_fix_override.py``, ``check_merge_gate_overlap.py``)
resolve their script the same way the command docs do, repo-local
``.claude/scripts/<name>`` first and then ``~/.claude/scripts/<name>``, and
apply the same strict marker rule: a full ``# cw-script-version: N`` comment
line within the first five lines, with ``N`` a 1-6 digit integer at or above
the caller's minimum (#3062).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_MARKER_HEAD_LINES: Final = 5
_MARKER_LINE: Final = re.compile(r"#\s*cw-script-version:\s*(\S*)\s*")
_MARKER_TOKEN: Final = re.compile(r"[0-9]{1,6}")


def find_guard_script(worktree: Path, name: str) -> Path | None:
    """The script *name*: repo-local first, then ``~/.claude/scripts``."""
    for root in (worktree, Path.home()):
        candidate = root / ".claude" / "scripts" / name
        if candidate.is_file():
            return candidate
    return None


def script_marker_ok(script: Path, minimum: int) -> bool:
    """Whether *script* carries a ``cw-script-version`` marker of at least *minimum*."""
    try:
        head = script.read_text(encoding="utf-8").splitlines()[:_MARKER_HEAD_LINES]
    except (OSError, UnicodeDecodeError):
        head = []
    token = ""
    for line in head:
        match = _MARKER_LINE.fullmatch(line)
        if match is not None:
            token = match.group(1)
            break
    return bool(_MARKER_TOKEN.fullmatch(token)) and int(token) >= minimum
