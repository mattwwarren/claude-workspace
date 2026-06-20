"""In-flight inspection of RUNNING dev-queue sessions (``cw queue peek``).

For each RUNNING task in the dev-queue (one client or all), look up:

- session age (first user message in the worker's transcript)
- idle gap (last assistant message)
- last AUTO_DEV_RESULT sentinel status and stage
- PR number and state (via ``gh pr view``)
- attempt counter

Then compute a WAIT / PEEK / STOP recommendation per row so the operator can
decide whether to keep a session alive or close it via ``cw spawn close``.

Reports only — never stops sessions itself.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from cw.auto_dev_result import (
    AutoDevResult,
    extract_block,
    is_documented_example,
    parse_stdout,
)
from cw.dev_queue import list_tickets
from cw.gh import _fetch_pr_state
from cw.models import QueueItemStatus, TicketTask

if TYPE_CHECKING:
    from collections.abc import Iterable

# Patched by tests to redirect path lookups without touching the filesystem.
CLAUDE_PROJECTS: Path = Path.home() / ".claude/projects"
CW_STATE: Path = Path.home() / ".local/share/cw/sessions.json"

# Thresholds — absolute (tier-agnostic). The contract enforces a 60-min hard
# ceiling via HEADLESS_TIMEOUT_SECONDS; the ladder is calibrated against that.
WAIT_AGE_MIN: int = 30  # below this, almost always healthy
PEEK_AGE_MIN: int = 45  # above this, peek even if active
STOP_AGE_MIN: int = 55  # approaching timeout — stop or hand off
IDLE_PEEK_MIN: int = 7  # idle this long with no PR → check for stall
IDLE_STALL_MIN: int = 15  # idle this long → likely stuck
IDLE_POST_PR_MIN: int = 5  # idle this long after PR shipped → stuck in stage5
STOP_ATTEMPTS_MIN: int = 3  # at or above this attempt count → systemic failure


def load_running_tasks(client: str | None) -> list[TicketTask]:
    """Return RUNNING TicketTask entries, optionally filtered by client."""
    return [t for t in list_tickets(client) if t.status == QueueItemStatus.RUNNING]


def load_claude_session_id(session_id: str | None) -> str | None:
    """Map a cw session id (8-char hex) to the full claude_session_id UUID.

    Reads CW_STATE directly so tests can monkeypatch the path without
    wiring through the full cw.config stack.
    """
    if not session_id or not CW_STATE.exists():
        return None
    try:
        data = json.loads(CW_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for sess in data.get("sessions", []):
        if sess.get("id") == session_id:
            raw = sess.get("claude_session_id")
            return str(raw) if raw is not None else None
    return None


def _matching_project_dirs(ticket_id: str) -> list[Path]:
    """Return project dirs whose name contains ``auto-dev-{ticket_id}``."""
    if not CLAUDE_PROJECTS.exists():
        return []
    return [
        p
        for p in CLAUDE_PROJECTS.iterdir()
        if p.is_dir() and f"auto-dev-{ticket_id}" in p.name
    ]


def _find_transcript_heuristic(ticket_id: str) -> Path | None:
    """Heuristic fallback: score by /auto-dev prefix, pick the most recent run."""
    candidates: list[tuple[Path, int, str]] = []  # (path, score, first_user_ts)
    for proj in _matching_project_dirs(ticket_id):
        for jsonl in proj.glob("*.jsonl"):
            first_user_ts = ""
            first_user_text = ""
            with jsonl.open() as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "user":
                        continue
                    first_user_ts = d.get("timestamp", "")
                    msg = d.get("message", {})
                    contents = msg.get("content", "")
                    if isinstance(contents, str):
                        first_user_text = contents
                    elif isinstance(contents, list):
                        for c in contents:
                            if isinstance(c, dict) and c.get("type") == "text":
                                first_user_text += c.get("text", "")
                    break
            score = 0 if f"/auto-dev {ticket_id}" in first_user_text else 1
            candidates.append((jsonl, score, first_user_ts))
    if not candidates:
        return None
    # Two-pass stable sort: latest timestamp first, then score ascending.
    # Result: score=0 (main session) beats score=1 (subagents); among score=0
    # candidates across multiple runs, the most recent run's transcript wins.
    candidates.sort(key=lambda c: c[2], reverse=True)  # latest timestamp first
    candidates.sort(key=lambda c: c[1])  # score 0 before 1 (stable)
    return candidates[0][0]


def find_transcript_for_ticket(
    ticket_id: str, session_id: str | None = None
) -> Path | None:
    """Locate the main /auto-dev transcript jsonl for a ticket.

    If session_id is provided, resolves it to a claude_session_id via
    CW_STATE and returns the matching jsonl directly (exact match).
    Falls back to a heuristic that prefers the most recent main-session
    transcript when no exact match is found.
    """
    claude_id = load_claude_session_id(session_id)
    if claude_id:
        for proj in _matching_project_dirs(ticket_id):
            candidate = proj / f"{claude_id}.jsonl"
            if candidate.exists():
                return candidate
    return _find_transcript_heuristic(ticket_id)


def parse_transcript(path: Path) -> dict[str, Any]:
    """Walk the jsonl, return first/last activity timestamps + last sentinel status.

    Uses ``auto_dev_result.extract_block`` and ``parse_stdout`` for sentinel
    parsing so the same framing and coercion rules apply here as in the rest of
    the pipeline.
    """
    first_user_ts: str | None = None
    last_asst_ts: str | None = None
    last_sentinel: AutoDevResult | None = None

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry_type = d.get("type")
                ts = d.get("timestamp")
                if entry_type == "user" and not first_user_ts and ts:
                    first_user_ts = ts
                if entry_type == "assistant" and ts:
                    last_asst_ts = ts
                    msg = d.get("message", {})
                    contents = msg.get("content", [])
                    if not isinstance(contents, list):
                        continue
                    for block in contents:
                        if not isinstance(block, dict) or block.get("type") != "text":
                            continue
                        text = block.get("text", "")
                        if not isinstance(text, str) or extract_block(text) is None:
                            continue
                        result = parse_stdout(text)
                        if not isinstance(result, AutoDevResult):
                            continue
                        if not is_documented_example(result):
                            last_sentinel = result
    except OSError:
        pass

    return {
        "first_user_ts": first_user_ts,
        "last_asst_ts": last_asst_ts,
        "last_sentinel_status": last_sentinel.status if last_sentinel else None,
        "last_sentinel_stage": last_sentinel.stage_reached if last_sentinel else None,
        "last_pr_number": (
            last_sentinel.pr.number if last_sentinel and last_sentinel.pr else None
        ),
    }


def gh_pr_state(pr_number: int) -> str:
    """Return OPEN | MERGED | CLOSED | UNKNOWN for the given PR number."""
    try:
        return _fetch_pr_state(pr_number, timeout=10) or "UNKNOWN"
    except FileNotFoundError:
        return "UNKNOWN"


def minutes_since(iso_ts: str | None, now: dt.datetime) -> float | None:
    """Return minutes elapsed since *iso_ts*, or None if unparseable."""
    if not iso_ts:
        return None
    try:
        ts = dt.datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return (now - ts).total_seconds() / 60.0


def _stage5_rec(idle_min: float | None) -> tuple[str, str]:
    """Return recommendation for a shipped+PR-open (stage-5 wait) session."""
    if idle_min is not None and idle_min > IDLE_STALL_MIN:
        reason = f"shipped + PR open but idle {idle_min:.0f}min — CI may be hung"
        return ("PEEK", reason)
    return ("WAIT", "shipped + PR open — auto-merge CI in progress")


def _stall_check(
    age_min: float,
    idle_min: float | None,
    pr_state: str | None,
) -> tuple[str, str]:
    """Return recommendation based on stall/age thresholds.

    Handles the generic age/idle ladder when no PR-merge or stage-5 special
    case applies.
    """
    if idle_min is not None and idle_min > IDLE_STALL_MIN and not pr_state:
        return (
            "STOP-OR-PEEK",
            f"idle {idle_min:.0f}min, no PR — likely stuck; manual peek before stop",
        )
    if idle_min is not None and idle_min > IDLE_PEEK_MIN and not pr_state:
        return ("PEEK", f"idle {idle_min:.0f}min, no PR — check for tool denial")
    if age_min > PEEK_AGE_MIN:
        return ("PEEK", f"age {age_min:.0f}min mature — check stage")
    if age_min > WAIT_AGE_MIN:
        return ("WAIT", f"age {age_min:.0f}min in normal range")
    return ("WAIT", f"age {age_min:.0f}min — early/healthy")


def _score_session(
    age_min: float,
    idle_min: float | None,
    pr_state: str | None,
    sentinel_status: str | None,
    attempts: int,
) -> tuple[str, str]:
    """Return recommendation when age_min is known."""
    if pr_state == "MERGED" and idle_min is not None and idle_min > IDLE_POST_PR_MIN:
        return (
            "STOP",
            f"PR merged + worker idle {idle_min:.0f}min — stuck in post-create wait",
        )
    if sentinel_status == "shipped" and pr_state == "OPEN":
        return _stage5_rec(idle_min)
    if age_min > STOP_AGE_MIN:
        reason = f"age {age_min:.0f}min approaches 60-min timeout — stop or hand off"
        return ("STOP", reason)
    if attempts >= STOP_ATTEMPTS_MIN:
        return ("STOP", f"attempt {attempts} — systemic, not transient")
    return _stall_check(age_min, idle_min, pr_state)


def recommend(
    age_min: float | None,
    idle_min: float | None,
    pr_state: str | None,
    sentinel_status: str | None,
    attempts: int,
) -> tuple[str, str]:
    """Return (recommendation, reasoning) from the peek-stop ladder."""
    if age_min is None:
        return ("PEEK", "no transcript timestamps — verify session is alive")
    return _score_session(age_min, idle_min, pr_state, sentinel_status, attempts)


def format_row(t: TicketTask, info: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Build a report dict for one RUNNING task."""
    age = minutes_since(info.get("first_user_ts"), now)
    idle = minutes_since(info.get("last_asst_ts"), now)
    pr_state = None
    if info.get("last_pr_number"):
        pr_state = gh_pr_state(info["last_pr_number"])
    rec, reason = recommend(
        age, idle, pr_state, info.get("last_sentinel_status"), t.attempts
    )
    return {
        "ticket": t.ticket_id,
        "session": (t.session_id or "-")[:12],
        "client": t.client,
        "attempts": t.attempts,
        "age_min": round(age, 1) if age is not None else None,
        "idle_min": round(idle, 1) if idle is not None else None,
        "stage": info.get("last_sentinel_stage"),
        "status": info.get("last_sentinel_status"),
        "pr": info.get("last_pr_number"),
        "pr_state": pr_state,
        "recommend": rec,
        "reason": reason,
    }


def build_peek_rows(client: str | None, now: dt.datetime) -> list[dict[str, Any]]:
    """Enumerate RUNNING tasks and build one report row per task."""
    rows = []
    for t in load_running_tasks(client):
        transcript = find_transcript_for_ticket(str(t.ticket_id), t.session_id)
        info = parse_transcript(transcript) if transcript else {}
        rows.append(format_row(t, info, now))
    return rows


def print_table(rows: Iterable[dict[str, Any]]) -> None:
    """Print rows as a formatted text table with a suggested-stops footer."""
    rows = list(rows)
    if not rows:
        click.echo("No RUNNING tasks found.")
        return
    cols = [
        ("ticket", 7),
        ("session", 12),
        ("att", 3),
        ("age_m", 7),
        ("idle_m", 7),
        ("stage", 18),
        ("status", 22),
        ("pr", 6),
        ("pr_state", 9),
        ("recommend", 14),
    ]
    header = "  ".join(f"{name:<{w}}" for name, w in cols)
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        cells = []
        for name, w in cols:
            key = name.replace("_m", "_min").replace("att", "attempts")
            val = (
                r.get(key, "-")
                if name in {"age_m", "idle_m", "att"}
                else r.get(name, "-") or "-"
            )
            cells.append(f"{val!s:<{w}}")
        click.echo("  ".join(cells))
        if r.get("recommend") != "WAIT":
            click.echo(f"    └─ {r.get('reason')}")
    actionable = [r for r in rows if r["recommend"].startswith("STOP")]
    if actionable:
        click.echo()
        click.echo("Suggested stops:")
        for r in actionable:
            click.echo(
                f"  cw spawn close {r['session']}  # #{r['ticket']} — {r['reason']}"
            )
