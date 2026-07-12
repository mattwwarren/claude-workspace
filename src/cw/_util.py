"""Shared utility helpers for cw internal modules.

Keep this module free of imports from other ``cw.*`` modules to avoid
circular dependencies — imported by :mod:`cw.cli` and :mod:`cw.reconcile`,
so those modules load without circular dependencies.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


MCP_EXTRA_MSG = (
    "channel server requires [mcp] extra; "
    "run 'uv pip install cw[mcp]' or 'uv sync --extra mcp'. "
    "If you installed with 'uv tool install', reinstall with the extra: "
    'uv tool install "claude-workspace[mcp] @ '
    'git+https://github.com/mattwwarren/claude-workspace.git" '
    '(or --from ".[mcp]" from a local clone).'
)


def _tail_lines(content: str, n: int) -> str:
    """Return the last *n* lines of *content*, preserving no trailing newline."""
    all_lines = content.splitlines()
    if len(all_lines) > n:
        return "\n".join(all_lines[-n:])
    return content.rstrip("\n")


_WORKTREE_DISPLAY_MAX = 40


def _shorten_worktree(path_value: object, home: str) -> str:
    """Display a worktree path relative to ``$HOME`` and capped in length. Pure."""
    if path_value is None:
        return "—"
    as_str = str(path_value)
    if home and as_str.startswith(home):
        as_str = "~" + as_str[len(home) :]
    if len(as_str) > _WORKTREE_DISPLAY_MAX:
        keep = _WORKTREE_DISPLAY_MAX - 1
        as_str = "…" + as_str[-keep:]
    return as_str


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


def _iter_tool_result_text(block: dict[str, object]) -> Iterator[str]:
    """Yield the text of a single ``tool_result`` content block.

    Anthropic transcripts encode a tool result's ``content`` either as a plain
    string or as a list of ``{"type": "text", "text": ...}`` sub-blocks. Yields
    nothing for any other shape.
    """
    content = block.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for sub in content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                text = sub.get("text")
                if isinstance(text, str):
                    yield text


def _iter_sentinel_text_blocks(transcript_path: Path) -> Iterator[str]:
    """Yield every text block that may carry an AUTO_DEV_RESULT sentinel.

    Superset of :func:`_iter_assistant_text_blocks`: yields assistant ``text``
    blocks AND ``tool_result`` block content (a worker's Bash stdout). A worker
    may emit the sentinel via ``cat <<EOF`` rather than as plain assistant text,
    landing the frame in a tool_result block — scanning only assistant text
    misses it and the stage stalls (GitHub #731). User prose ``text`` blocks and
    the ``tool_use`` command echo are deliberately NOT yielded, to avoid
    surfacing the prompt's schema example or a duplicate of the same frame.

    A missing file, an I/O error, or a malformed line/record yields nothing
    rather than raising. Blocks are yielded in file order.
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
                if not isinstance(record, dict):
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                is_assistant = record.get("type") == "assistant"
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if is_assistant and block_type == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            yield text
                    elif block_type == "tool_result":
                        yield from _iter_tool_result_text(block)
    except OSError:
        return


def _last_content_entry_timestamp(transcript_path: Path) -> datetime | None:
    """Return the timestamp of the last content-bearing transcript record.

    Claude Code appends several non-conversational record types over a
    session's lifetime — ``ai-title``, ``agent-name``, ``last-prompt``,
    ``mode``, ``permission-mode``, ``bridge-session``, ``queue-operation``,
    ``file-history-snapshot``, ``attachment``, ``system`` — none of which
    carry a ``"message"`` field. These bump the transcript file's ``mtime``
    without representing genuine model/user activity, causing mtime-based
    liveness checks to false-positive on a stalled session (GitHub #1076).

    A content-bearing record is a ``user`` or ``assistant`` record whose
    ``message`` field is a ``dict`` — the same guard already used inline by
    ``_awaiting_subagent`` (:mod:`cw.reconcile._shared`); any block type
    (text, tool_use, tool_result) within such a message counts as activity.

    Performs a full forward linear scan of the file (mirrors
    ``_awaiting_subagent``'s existing full-scan cost profile — no tail-read,
    no offset cache) so that the LAST content-bearing record with a
    parseable ``"timestamp"`` wins. A content-bearing record with a missing
    or malformed timestamp is skipped, not scan-aborting.

    Returns ``None`` when the file is missing, an ``OSError`` occurs while
    reading, or no content-bearing record anywhere in the file has a
    parseable timestamp — callers must fall back to mtime-based liveness in
    that case.
    """
    if not transcript_path.is_file():
        return None
    last_ts: datetime | None = None
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") not in {"user", "assistant"}:
                    continue
                if not isinstance(record.get("message"), dict):
                    continue
                ts = record.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    last_ts = datetime.fromisoformat(ts)
                except ValueError:
                    continue
    except OSError:
        return None
    return last_ts
