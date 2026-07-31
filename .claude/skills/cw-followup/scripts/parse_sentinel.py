#!/usr/bin/env python3
"""Locate and parse the AUTO_DEV_RESULT sentinel for a cw-dispatched session.

Resolves a session reference (short cw id, ticket id, claude session UUID, or
direct transcript path) to a JSONL transcript, walks each text block (assistant
text AND tool_result stdout) to find the LAST sentinel pair, and parses it via
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
import subprocess
import sys
from pathlib import Path
from typing import Any


def _bootstrap_sys_path() -> None:
    """Add repo src/ to sys.path so cw imports work under bare python3.

    # Why: this cannot be extracted to a shared module — it must run BEFORE any cw
    # import, so there is no shared cw path yet to import it from. Each standalone
    # script that imports cw carries its own copy. Do not deduplicate.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            src = str(parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            return
    msg = (
        f"Could not locate pyproject.toml walking up from {__file__} — bootstrap failed"
    )
    raise RuntimeError(msg)


_bootstrap_sys_path()

from cw._util import _iter_sentinel_text_blocks, claude_project_dir
from cw.auto_dev_result import (
    AutoDevResult,
    BlockedResult,
    _is_placeholder_sentinel_text,
    extract_block,
    is_documented_example,
    parse_stdout,
)


def _run_cw_json(*cw_args: str) -> Any:
    """Run a cw command and return parsed JSON output, or None on failure."""
    try:
        result = subprocess.run(
            ["cw", *cw_args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def _resolve_by_session_id(session_id: str) -> dict[str, Any] | None:
    """Prefix-match a session by short id via cw session show."""
    data = _run_cw_json("session", "show", session_id, "--json")
    return data if isinstance(data, dict) else None


def _resolve_by_ticket_id(ticket_id: str) -> dict[str, Any] | None:
    """Most recent session whose name references the ticket (via cw session list)."""
    needle = str(ticket_id).lstrip("#")
    candidates: list[dict[str, Any]] = []
    # Query non-terminal, completed, and timed_out sessions to cover all cases.
    for extra in ([], ["--status", "completed"], ["--status", "timed_out"]):
        args = ["session", "list", "--ticket", needle, "--json", *extra]
        data = _run_cw_json(*args)
        if isinstance(data, list):
            candidates.extend(s for s in data if isinstance(s, dict))
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.get("started_at") or "")


def _transcript_path_for_session(session: dict[str, Any]) -> Path | None:
    claude_session_id = session.get("claude_session_id")
    cwd = session.get("worktree_path")
    if not claude_session_id or not cwd:
        return None
    # cw._util.claude_project_dir owns the path-encoding rule (#463 fixed a
    # single-replace bug there once) — do not reimplement it here.
    return claude_project_dir(Path(cwd)) / f"{claude_session_id}.jsonl"


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

    blocks_scanned = 0
    sentinel_text: str | None = None
    last_result: AutoDevResult | BlockedResult | None = None
    for text in _iter_sentinel_text_blocks(transcript_path):
        blocks_scanned += 1
        block = extract_block(text)
        if block is not None:
            if _is_placeholder_sentinel_text(block):
                continue
            candidate = parse_stdout(text)
            if isinstance(candidate, AutoDevResult) and is_documented_example(
                candidate
            ):
                continue
            sentinel_text = text
            last_result = candidate

    if last_result is None:
        result: AutoDevResult | BlockedResult = parse_stdout(
            "",
        )  # produces a BlockedResult(no_result_emitted)
    else:
        result = last_result

    output: dict[str, Any] = {
        "transcript_path": str(transcript_path),
        "blocks_scanned": blocks_scanned,
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
