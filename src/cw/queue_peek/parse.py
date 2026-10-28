"""Transcript and sentinel parsing for ``cw queue peek``.

Walks a claude-jsonl transcript (or an opencode log) for first/last activity
timestamps, the last ``AUTO_DEV_RESULT`` sentinel, the PR number and the
usage-limit signal. Reads only the file it is given.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from cw._transcript import subagent_transcript_paths
from cw._util import _iter_tool_result_text
from cw.auto_dev_result import (
    AutoDevResult,
    extract_block,
    is_documented_example,
    parse_stdout,
)
from cw.exceptions import USAGE_LIMIT_RE
from cw.opencode_runner import parse_last_sentinel
from cw.pr_hydrate import _PR_URL_RE

if TYPE_CHECKING:
    from pathlib import Path


def _newest_subagent_ts(project_dir: Path, claude_session_id: str | None) -> str | None:
    """Return the newest content-based last-activity ts across one parent
    session's subagent transcripts, or None (#2028).

    Reuses ``parse_transcript`` per child so the result inherits its
    away_summary immunity (only ``entry_type == "assistant"`` counts) — no
    separate parser needed. ISO-8601 timestamp strings sort correctly
    lexicographically, matching the comparison idiom already used elsewhere
    in this module.
    """
    if claude_session_id is None:
        return None
    newest: str | None = None
    for child in subagent_transcript_paths(project_dir, claude_session_id):
        child_ts = parse_transcript(child).get("last_asst_ts")
        if child_ts is not None and (newest is None or child_ts > newest):
            newest = child_ts
    return newest


def _scan_assistant_content(contents: list[Any]) -> tuple[AutoDevResult | None, bool]:
    """Scan one assistant message's content blocks.

    Returns ``(sentinel, usage_limit_detected)`` — the latest non-example
    ``AutoDevResult`` found in the blocks (or None), and whether any text
    block matched :data:`USAGE_LIMIT_RE`. Usage-limit detection is
    independent of sentinel framing — it scans every text block regardless
    of whether it is wrapped in a ``<<<AUTO_DEV_RESULT`` marker.
    """
    sentinel: AutoDevResult | None = None
    usage_limit_detected = False
    for block in contents:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        if not isinstance(text, str):
            continue
        if USAGE_LIMIT_RE.search(text):
            usage_limit_detected = True
        if extract_block(text) is None:
            continue
        result = parse_stdout(text)
        if not isinstance(result, AutoDevResult):
            continue
        if not is_documented_example(result):
            sentinel = result
    return sentinel, usage_limit_detected


def _tool_result_pr_number(record: dict[str, Any]) -> int | None:
    """Return the last GitHub PR number named in one user record's tool_results.

    Fallback evidence for a session wedged before it emitted its finalize
    sentinel: ``gh pr create`` already printed the PR URL into a
    ``tool_result`` block (#2251). Only ``tool_result`` blocks count -- a PR
    URL in assistant prose is not evidence this session opened it.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    contents = message.get("content")
    if not isinstance(contents, list):
        return None
    pr_number: int | None = None
    for block in contents:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        for text in _iter_tool_result_text(block):
            for match in _PR_URL_RE.finditer(text):
                pr_number = int(match.group(2))
    return pr_number


def parse_transcript(path: Path) -> dict[str, Any]:
    """Walk the jsonl, return first/last activity timestamps + last sentinel status.

    Uses ``auto_dev_result.extract_block`` and ``parse_stdout`` for sentinel
    parsing so the same framing and coercion rules apply here as in the rest of
    the pipeline.
    """
    first_user_ts: str | None = None
    last_asst_ts: str | None = None
    last_sentinel: AutoDevResult | None = None
    tool_output_pr_number: int | None = None
    usage_limit_detected = False

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry_type = d.get("type")
                ts = d.get("timestamp")
                if entry_type == "user":
                    if not first_user_ts and ts:
                        first_user_ts = ts
                    tool_output_pr_number = (
                        _tool_result_pr_number(d) or tool_output_pr_number
                    )
                if entry_type == "assistant" and ts:
                    last_asst_ts = ts
                    msg = d.get("message", {})
                    contents = msg.get("content", [])
                    if not isinstance(contents, list):
                        continue
                    sentinel, hit_limit = _scan_assistant_content(contents)
                    usage_limit_detected = usage_limit_detected or hit_limit
                    if sentinel is not None:
                        last_sentinel = sentinel
    except OSError:
        pass

    return {
        "first_user_ts": first_user_ts,
        "last_asst_ts": last_asst_ts,
        "last_sentinel_status": last_sentinel.status if last_sentinel else None,
        "last_sentinel_stage": last_sentinel.stage_reached if last_sentinel else None,
        "last_pr_number": (
            last_sentinel.pr.number
            if last_sentinel and last_sentinel.pr
            else tool_output_pr_number
        ),
        "usage_limit_detected": usage_limit_detected,
    }


def parse_opencode_transcript(path: Path, ticket_id: str) -> dict[str, Any]:
    """Parse an opencode JSONL log for queue_peek (#1671 R3).

    opencode's ``--format json`` output has a different shape than claude-jsonl
    (event types: ``step_start``/``step_finish``/``text``; no user/assistant
    distinction; no timestamps in events). This reader shares
    ``parse_last_sentinel`` with the harvest path so peek and harvest always
    agree on which sentinel a log carries (#2490): the last one for *ticket_id*,
    a block quoted for another ticket is ignored.

    Since opencode events carry no timestamps, the file's mtime serves as the
    last-activity proxy (``last_asst_ts``). ``first_user_ts`` is None —
    ``format_row`` falls through to ``claim_started_at`` for age computation.
    """
    last_sentinel: AutoDevResult | None = None
    try:
        log_content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "first_user_ts": None,
            "last_asst_ts": None,
            "last_sentinel_status": None,
            "last_sentinel_stage": None,
            "last_pr_number": None,
            "usage_limit_detected": False,
        }

    if log_content:
        result = parse_last_sentinel(log_content, ticket_id=ticket_id)
        if isinstance(result, AutoDevResult):
            last_sentinel = result

    last_asst_ts: str | None = None
    try:
        mtime = path.stat().st_mtime
        last_asst_ts = dt.datetime.fromtimestamp(mtime, tz=dt.UTC).isoformat()
    except OSError:
        pass

    return {
        "first_user_ts": None,
        "last_asst_ts": last_asst_ts,
        "last_sentinel_status": last_sentinel.status if last_sentinel else None,
        "last_sentinel_stage": last_sentinel.stage_reached if last_sentinel else None,
        "last_pr_number": (
            last_sentinel.pr.number if last_sentinel and last_sentinel.pr else None
        ),
        "usage_limit_detected": False,
    }
