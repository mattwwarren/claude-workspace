#!/usr/bin/env python3
"""Locate and parse the AUTO_DEV_RESULT sentinel for a cw-dispatched session.

Resolves a session reference (short cw id, ticket id, claude session UUID, or
direct transcript path) to a JSONL transcript, walks each ``assistant`` text
block to find the LAST sentinel pair, and parses it via
``cw.auto_dev_result.parse_stdout`` (the production parser — never reimplement).

Output: a single JSON line on stdout describing the resolved transcript and the
parsed result. Exit code is 0 when the transcript was located and parsed (even
when the parse yielded a ``BlockedResult``); 1 when the session reference could
not be resolved or the transcript file was missing.

Run via ``uv run`` from the cw repo so ``cw.auto_dev_result`` imports cleanly:

    uv run --project "$(git rev-parse --show-toplevel)" \\
        python .claude/skills/cw-followup/scripts/parse_sentinel.py \\
        --session-id <short-id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cw.auto_dev_result import AutoDevResult, BlockedResult, extract_block, parse_stdout

_SESSIONS_PATH = Path.home() / ".local" / "share" / "cw" / "sessions.json"


def _load_sessions() -> dict[str, dict[str, Any]]:
    if not _SESSIONS_PATH.is_file():
        return {}
    with _SESSIONS_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
    if isinstance(sessions, dict):
        return sessions
    if isinstance(sessions, list):
        result: dict[str, dict[str, Any]] = {}
        for entry in sessions:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                result[entry_id] = entry
        return result
    return {}


def _resolve_by_session_id(session_id: str) -> dict[str, Any] | None:
    sessions = _load_sessions()
    if session_id in sessions:
        return sessions[session_id]
    # Allow prefix match for the short ID convention
    matches = [s for sid, s in sessions.items() if sid.startswith(session_id)]
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_by_ticket_id(ticket_id: str) -> dict[str, Any] | None:
    """Most recent session whose worktree_path or branch references the ticket."""
    sessions = _load_sessions()
    needle = str(ticket_id).lstrip("#")
    candidates = []
    for session in sessions.values():
        worktree = session.get("worktree_path") or ""
        branch = session.get("branch") or ""
        # auto-dev/<n>, auto-dev-<n>, dev/issue-<n>-... patterns
        if (
            f"auto-dev-{needle}" in worktree
            or f"auto-dev/{needle}" in branch
            or f"auto-dev/#{needle}" in branch
            or f"issue-{needle}" in branch
        ):
            candidates.append(session)
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.get("started_at") or "")


def _transcript_path_for_session(session: dict[str, Any]) -> Path | None:
    claude_session_id = session.get("claude_session_id")
    cwd = session.get("worktree_path")
    if not claude_session_id or not cwd:
        return None
    encoded = str(cwd).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{claude_session_id}.jsonl"


def _iter_assistant_text_blocks(transcript_path: Path) -> list[str]:
    """Yield each assistant message text block in order. JSONL-aware.

    The transcript is one JSON record per line. Assistant events carry their
    text under ``message.content[*].text`` and the text is itself JSON-escaped
    (real newlines become ``\\n``), which is why scanning the raw file with the
    sentinel regex misses real runs — see GitHub issue #176 / PR #179.
    """
    blocks: list[str] = []
    if not transcript_path.is_file():
        return blocks
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
                    blocks.append(text)
    return blocks


def _find_last_sentinel_text(blocks: list[str]) -> str | None:
    """Walk in reverse — most runs emit the sentinel at the final assistant turn."""
    for text in reversed(blocks):
        if extract_block(text) is not None:
            return text
    return None


def _result_to_dict(result: AutoDevResult | BlockedResult) -> dict[str, Any]:
    parsed: Any = json.loads(result.model_dump_json())
    if isinstance(parsed, dict):
        return parsed
    return {}


def _raw_payload(sentinel_text: str | None) -> dict[str, Any] | None:
    """Return the raw JSON inside the sentinel block (unvalidated).

    The parser's ``Status`` Literal is closed, so producer-emitted statuses
    like ``premises_pending_verification`` route through ``BlockedResult``
    with ``reason=status_unknown``. The skill still needs the original fields
    (friction reports, ambiguity arrays) to drive the followup action, so we
    expose the raw payload alongside the validated parse.
    """
    if sentinel_text is None:
        return None
    inner = extract_block(sentinel_text)
    if inner is None:
        return None
    try:
        payload = json.loads(inner)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a cw session and parse its AUTO_DEV_RESULT sentinel.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--session-id", help="Short cw session id (prefix match allowed)."
    )
    source.add_argument(
        "--ticket-id",
        help=("GitHub ticket number; resolves to the most recent matching session."),
    )
    source.add_argument(
        "--transcript-path",
        help="Direct path to a Claude JSONL transcript file.",
    )
    args = parser.parse_args()

    session: dict[str, Any] | None = None
    transcript_path: Path | None = None

    if args.transcript_path:
        transcript_path = Path(args.transcript_path)
    elif args.session_id:
        session = _resolve_by_session_id(args.session_id)
    elif args.ticket_id:
        session = _resolve_by_ticket_id(args.ticket_id)

    if session is not None and transcript_path is None:
        transcript_path = _transcript_path_for_session(session)

    if transcript_path is None:
        sys.stderr.write(
            "could not resolve a transcript path from the given session reference\n",
        )
        return 1
    if not transcript_path.is_file():
        sys.stderr.write(f"transcript file not found: {transcript_path}\n")
        return 1

    blocks = _iter_assistant_text_blocks(transcript_path)
    sentinel_text = _find_last_sentinel_text(blocks)

    if sentinel_text is None:
        result: AutoDevResult | BlockedResult = parse_stdout(
            "",
        )  # produces a BlockedResult(no_result_emitted)
    else:
        result = parse_stdout(sentinel_text)

    output: dict[str, Any] = {
        "transcript_path": str(transcript_path),
        "assistant_blocks_scanned": len(blocks),
        "sentinel_found": sentinel_text is not None,
        "session": {
            "id": session.get("id") if session else None,
            "name": session.get("name") if session else None,
            "status": session.get("status") if session else None,
            "worktree_path": session.get("worktree_path") if session else None,
            "branch": session.get("branch") if session else None,
        }
        if session
        else None,
        "result": _result_to_dict(result),
        "result_kind": "AutoDevResult"
        if isinstance(result, AutoDevResult)
        else "BlockedResult",
        "raw_payload": _raw_payload(sentinel_text),
    }
    sys.stdout.write(json.dumps(output, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
