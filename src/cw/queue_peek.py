"""In-flight inspection of RUNNING dev-queue sessions (``cw queue peek``).

For each RUNNING task in the dev-queue (one client or all), look up:

- session age (primarily the session's claim time — ``Session.started_at``
  in CW_STATE — falling back to the first user message in the worker's
  transcript when claim data is unavailable)
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

from cw._transcript import locate_transcript
from cw._util import claude_project_dir
from cw.auto_dev_result import (
    AutoDevResult,
    extract_block,
    is_documented_example,
    parse_stdout,
)
from cw.dev_queue import list_tickets
from cw.exceptions import USAGE_LIMIT_RE
from cw.gh import _fetch_pr_state
from cw.models import QueueItemStatus, Stage, TicketTask

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

_STAGE_ORDER: tuple[Stage, ...] = (
    Stage.HARDEN,
    Stage.PLAN,
    Stage.IMPL,
    Stage.REVIEW,
    Stage.FINALIZE,
)


def _reached_deep_stage(high_water: Stage | None) -> bool:
    if high_water is None:
        # unknown = no signal; does NOT suppress STOP (per R2)
        return False
    return _STAGE_ORDER.index(high_water) >= _STAGE_ORDER.index(Stage.REVIEW)


RECOMMEND_BLIND = "PEEK-BLIND"
_SIGNAL_SOURCE_BLIND = "blind"
_SIGNAL_SOURCE_TRANSCRIPT = "transcript"
_EPOCH = dt.datetime.fromtimestamp(0, tz=dt.UTC)


def load_running_tasks(client: str | None) -> list[TicketTask]:
    """Return RUNNING TicketTask entries, optionally filtered by client."""
    return [t for t in list_tickets(client) if t.status == QueueItemStatus.RUNNING]


def _load_session_refs(session_id: str | None) -> dict[str, Any]:
    """Load session lookup fields from CW_STATE for a cw session id.

    Returns a dict with ``claude_session_id``, ``surface_ref``,
    ``started_at``, and ``worktree_path`` (all may be None), or an empty
    dict when session_id is absent or no match is found. Reads CW_STATE
    directly so tests can monkeypatch the path without wiring through the
    full cw.config stack.
    """
    if not session_id or not CW_STATE.exists():
        return {}
    try:
        data = json.loads(CW_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    for sess in data.get("sessions", []):
        if sess.get("id") == session_id:
            return {
                "claude_session_id": sess.get("claude_session_id"),
                "surface_ref": sess.get("surface_ref"),
                "started_at": sess.get("started_at"),
                "worktree_path": sess.get("worktree_path"),
            }
    return {}


def load_claude_session_id(session_id: str | None) -> str | None:
    """Map a cw session id (8-char hex) to the full claude_session_id UUID.

    Reads CW_STATE directly so tests can monkeypatch the path without
    wiring through the full cw.config stack.
    """
    raw = _load_session_refs(session_id).get("claude_session_id")
    return str(raw) if raw is not None else None


def _parse_started_at(started_at_iso: str | None) -> dt.datetime:
    """Parse a started_at ISO string, returning _EPOCH on missing/invalid input."""
    if started_at_iso is None:
        return _EPOCH
    try:
        ts = dt.datetime.fromisoformat(started_at_iso)
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)
    except ValueError:
        return _EPOCH


def _find_transcript_in_project_dir(
    project_dir: Path,
    claude_session_id: str | None,
    surface_ref: str | None,
    started_at_iso: str | None,
) -> Path | None:
    """Find a transcript inside a known Claude project dir.

    Resolution order:
    1. csid set → ``<project_dir>/<csid>.jsonl`` (exact match via locate_transcript).
    2. csid absent or file missing → surface_ref newest-only with mtime >
       started_at (reused-worktree stale-transcript guard, via locate_transcript).
    3. Degraded fallback (both ids None) → newest ``*.jsonl`` in project_dir
       (best-effort when backfill hasn't fired yet; may include subagent files).

    Returns None when project_dir does not exist or no jsonl is found.
    """
    if not project_dir.is_dir():
        return None
    started_at = _parse_started_at(started_at_iso)
    # Layer 1: csid exact (locate_transcript does not fall through to surface_ref)
    if claude_session_id is not None:
        result = locate_transcript(
            project_dir=project_dir,
            claude_session_id=claude_session_id,
            surface_ref=None,
            started_at=started_at,
        )
        if result is not None:
            return result
    # Layer 2: surface_ref newest-only with stale guard
    if surface_ref is not None:
        return locate_transcript(
            project_dir=project_dir,
            claude_session_id=None,
            surface_ref=surface_ref,
            started_at=started_at,
        )
    # Layer 3: degraded fallback (both ids absent — backfill hasn't fired yet)
    try:
        all_jsonl = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return all_jsonl[0] if all_jsonl else None
    except OSError:
        return None


def _matching_project_dirs(ticket_id: str) -> list[Path]:
    """Return project dirs whose name ends with ``-{ticket_id}``."""
    if not CLAUDE_PROJECTS.exists():
        return []
    return [
        p
        for p in CLAUDE_PROJECTS.iterdir()
        if p.is_dir() and p.name.endswith(f"-{ticket_id}")
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


def _compute_jsonl_idle_min(t: TicketTask, now: dt.datetime) -> float | None:
    """Return minutes since the newest *.jsonl in identifiable project dirs, or None.

    Best-effort liveness for blind rows (no resolvable transcript). Scans
    the project dir derived from worktree_path (from task or CW_STATE), or
    falls back to heuristic-matched dirs. Returns round(elapsed, 1) or None.
    """
    refs = _load_session_refs(t.session_id)
    effective_wt = t.worktree_path
    if effective_wt is None:
        raw_wt = refs.get("worktree_path")
        if raw_wt is not None:
            effective_wt = Path(str(raw_wt))

    project_dirs: list[Path] = []
    if effective_wt is not None:
        project_dirs.append(claude_project_dir(effective_wt))
    else:
        project_dirs.extend(_matching_project_dirs(str(t.ticket_id)))

    newest_mtime: float | None = None
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        try:
            for p in project_dir.glob("*.jsonl"):
                mtime = p.stat().st_mtime
                if newest_mtime is None or mtime > newest_mtime:
                    newest_mtime = mtime
        except OSError:
            continue

    if newest_mtime is None:
        return None
    elapsed_seconds = (
        now - dt.datetime.fromtimestamp(newest_mtime, tz=dt.UTC)
    ).total_seconds()
    return round(elapsed_seconds / 60.0, 1)


def find_transcript_for_ticket(
    ticket_id: str,
    session_id: str | None = None,
    worktree_path: Path | None = None,
) -> Path | None:
    """Locate the main /auto-dev transcript jsonl for a ticket.

    Uses ``claude_project_dir(worktree_path)`` to find the project dir when
    a worktree path is available — this resolves correctly for dispatch workers
    whose project dirs are named after the worktree path (e.g.
    ``-home-u--cw-wt-<hash>-dev-817``) rather than containing
    ``auto-dev-{ticket_id}`` in the name.

    The effective worktree path is resolved in priority order:
    1. Explicit ``worktree_path`` parameter (USER-origin sessions that stamp it).
    2. ``worktree_path`` from the Session in CW_STATE (DAEMON-origin sessions;
       dispatch writes worktree_path to the Session but not to the TicketTask).

    Within the project dir, resolution order is: (1) exact csid match, (2)
    surface_ref-prefix glob with mtime guard, (3) newest ``*.jsonl`` (degraded
    fallback when backfill hasn't fired yet for the session ids).

    Falls back to the legacy heuristic (name-based project dir search) when
    no worktree path is available or its project dir is not found on disk.
    """
    refs = _load_session_refs(session_id)

    # Prefer explicit arg; fall back to worktree_path from the Session in CW_STATE.
    # TicketTask.worktree_path is None for dispatch tasks (dispatch stamps
    # session_id but not worktree_path); the Session object carries the real path.
    effective_wt = worktree_path
    if effective_wt is None:
        raw_wt = refs.get("worktree_path")
        if raw_wt is not None:
            effective_wt = Path(str(raw_wt))

    if effective_wt is not None:
        project_dir = claude_project_dir(effective_wt)
        transcript = _find_transcript_in_project_dir(
            project_dir,
            refs.get("claude_session_id"),
            refs.get("surface_ref"),
            refs.get("started_at"),
        )
        if transcript is not None:
            return transcript

    # Legacy path: search matching project dirs by name, then heuristic.
    claude_id = refs.get("claude_session_id")
    if claude_id:
        for proj in _matching_project_dirs(ticket_id):
            candidate = proj / f"{claude_id}.jsonl"
            if candidate.exists():
                return candidate
    return _find_transcript_heuristic(ticket_id)


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


def parse_transcript(path: Path) -> dict[str, Any]:
    """Walk the jsonl, return first/last activity timestamps + last sentinel status.

    Uses ``auto_dev_result.extract_block`` and ``parse_stdout`` for sentinel
    parsing so the same framing and coercion rules apply here as in the rest of
    the pipeline.
    """
    first_user_ts: str | None = None
    last_asst_ts: str | None = None
    last_sentinel: AutoDevResult | None = None
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
                if entry_type == "user" and not first_user_ts and ts:
                    first_user_ts = ts
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
            last_sentinel.pr.number if last_sentinel and last_sentinel.pr else None
        ),
        "usage_limit_detected": usage_limit_detected,
    }


def gh_pr_state(pr_number: int) -> str:
    """Return OPEN | MERGED | CLOSED | UNKNOWN for the given PR number."""
    try:
        return _fetch_pr_state(pr_number, timeout=10) or "UNKNOWN"
    except FileNotFoundError:
        return "UNKNOWN"


def minutes_since(iso_ts: str | None, now: dt.datetime) -> float | None:
    """Return minutes elapsed since *iso_ts*, or None if unparseable.

    A naive (tz-less) *iso_ts* is coerced to UTC, mirroring
    ``_parse_started_at``'s coercion — CW_STATE's ``started_at`` can be a
    naive ISO string in practice.
    """
    if not iso_ts:
        return None
    try:
        ts = dt.datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
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
    stage_high_water: Stage | None = None,
    usage_limit_detected: bool = False,
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
    if attempts >= STOP_ATTEMPTS_MIN and not _reached_deep_stage(stage_high_water):
        if usage_limit_detected:
            return (
                "PEEK",
                f"attempt {attempts} but usage-limit outage detected in "
                "transcript — verify before stopping",
            )
        return ("STOP", f"attempt {attempts} — systemic, not transient")
    return _stall_check(age_min, idle_min, pr_state)


def recommend(
    age_min: float | None,
    idle_min: float | None,
    pr_state: str | None,
    sentinel_status: str | None,
    attempts: int,
    stage_high_water: Stage | None = None,
    usage_limit_detected: bool = False,
) -> tuple[str, str]:
    """Return (recommendation, reasoning) from the peek-stop ladder."""
    if age_min is None:
        return ("PEEK", "no transcript timestamps — verify session is alive")
    return _score_session(
        age_min=age_min,
        idle_min=idle_min,
        pr_state=pr_state,
        sentinel_status=sentinel_status,
        attempts=attempts,
        stage_high_water=stage_high_water,
        usage_limit_detected=usage_limit_detected,
    )


def format_row(t: TicketTask, info: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Build a report dict for one RUNNING task."""
    signal_source: str = info.get("signal_source", _SIGNAL_SOURCE_TRANSCRIPT)
    jsonl_idle_min: float | None = info.get("jsonl_idle_min")

    if signal_source == _SIGNAL_SOURCE_BLIND:
        if jsonl_idle_min is not None:
            reason = f"no resolvable transcript; newest jsonl {jsonl_idle_min:.0f}m ago"
        else:
            reason = "no resolvable transcript; none found"
        return {
            "ticket": t.ticket_id,
            "session": (t.session_id or "-")[:12],
            "client": t.client,
            "attempts": t.attempts,
            "age_min": None,
            "idle_min": None,
            "stage": None,
            "status": None,
            "pr": None,
            "pr_state": None,
            "recommend": RECOMMEND_BLIND,
            "reason": reason,
            "signal_source": signal_source,
            "jsonl_idle_min": jsonl_idle_min,
            "stage_high_water": t.stage_high_water,
            "pipeline_stage": t.stage,
        }

    claim_age = minutes_since(info.get("claim_started_at"), now)
    age = (
        claim_age
        if claim_age is not None
        else minutes_since(info.get("first_user_ts"), now)
    )
    idle = minutes_since(info.get("last_asst_ts"), now)
    if claim_age is not None and idle is not None and age is not None and idle > age:
        # The transcript's last-assistant timestamp predates the session's own
        # claim time — a logical contradiction proving it belongs to a stale,
        # reused-worktree transcript rather than this session.
        idle = None
    pr_state = None
    if info.get("last_pr_number"):
        pr_state = gh_pr_state(info["last_pr_number"])
    rec, reason = recommend(
        age_min=age,
        idle_min=idle,
        pr_state=pr_state,
        sentinel_status=info.get("last_sentinel_status"),
        attempts=t.attempts,
        stage_high_water=t.stage_high_water,
        usage_limit_detected=info.get("usage_limit_detected", False),
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
        "signal_source": signal_source,
        "jsonl_idle_min": None,
        "stage_high_water": t.stage_high_water,
        "pipeline_stage": t.stage,
    }


def build_peek_rows(client: str | None, now: dt.datetime) -> list[dict[str, Any]]:
    """Enumerate RUNNING tasks and build one report row per task."""
    rows = []
    for t in load_running_tasks(client):
        transcript = find_transcript_for_ticket(
            str(t.ticket_id), t.session_id, t.worktree_path
        )
        if transcript is not None:
            info: dict[str, Any] = parse_transcript(transcript)
            info["signal_source"] = _SIGNAL_SOURCE_TRANSCRIPT
            info["jsonl_idle_min"] = None
            info["claim_started_at"] = _load_session_refs(t.session_id).get(
                "started_at"
            )
        else:
            info = {
                "signal_source": _SIGNAL_SOURCE_BLIND,
                "jsonl_idle_min": _compute_jsonl_idle_min(t, now),
            }
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
