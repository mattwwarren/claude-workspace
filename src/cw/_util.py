"""Shared utility helpers for cw internal modules.

Keep this module free of imports from other ``cw.*`` modules to avoid
circular dependencies — imported by :mod:`cw.cli` and :mod:`cw.reconcile`,
so those modules load without circular dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def _tail_lines(content: str, n: int) -> str:
    """Return the last *n* lines of *content*, preserving no trailing newline."""
    all_lines = content.splitlines()
    if len(all_lines) > n:
        return "\n".join(all_lines[-n:])
    return content.rstrip("\n")


def claude_project_dir(path: str | Path) -> Path:
    """Return the Claude Code project directory for *path*.

    Claude Code encodes a project's working directory into a flat directory
    name under ``~/.claude/projects/`` by replacing every ``/`` **and** every
    ``.`` with ``-``.  For example ``/home/u/.cw/wt/abc`` becomes
    ``-home-u--cw-wt-abc`` (double dash for the dot-prefixed ``.cw``
    segment).

    Using only ``.replace("/", "-")`` — which was the original single-replace
    — produces ``-home-u-.cw-wt-abc``, a path that does not exist on disk,
    causing all transcript-based liveness checks to return ``False`` and the
    idle watchdog to falsely reap active sessions whose worktrees live under a
    dotted directory such as ``~/.cw/``.  See GitHub issue #463.
    """
    encoded = str(path).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _iter_assistant_text_blocks(transcript_path: Path) -> Iterator[str]:
    """Yield each assistant text block from a Claude transcript JSONL file.

    The transcript stores one event per line; ``assistant`` events carry
    ``message.content`` blocks whose ``text`` fields hold the model output,
    JSON-escaped (real newlines restored by ``json.loads``). Blocks are
    yielded in file order. A missing file, an I/O error, or a malformed
    line/record yields nothing rather than raising — callers treat an empty
    iteration as "no output available".
    """
    if not transcript_path.is_file():
        return
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = block.get("text")
                    if isinstance(text, str):
                        yield text
    except OSError:
        return
