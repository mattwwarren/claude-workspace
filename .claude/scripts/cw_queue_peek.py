#!/usr/bin/env python3
"""cw queue peek — in-flight inspection of RUNNING dev-queue sessions.

For each RUNNING task in the dev-queue (one client or all), look up:

- session age (now minus first user message in the worker's transcript)
- idle gap (now minus last assistant message)
- whether a final AUTO_DEV_RESULT sentinel has been emitted, and its status
- whether a PR was opened, and if so its current state (open / merged / closed)
- attempt counter

Then compute a recommendation per row using a peek/stop ladder so the
operator (or orchestrator) can decide whether to wait, peek again later, or
stop the session via `cw spawn close <session_id>`.

Output: a table on stdout + a JSON blob on `--json`.

Reports only. Never stops sessions itself — the operator runs
`cw spawn close <id>` after reviewing the report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

DEV_QUEUE = Path.home() / ".local/share/cw/dev_queue.json"
CLAUDE_PROJECTS = Path.home() / ".claude/projects"
NOW = dt.datetime.now(dt.UTC)

# Thresholds — absolute (tier-agnostic). The contract enforces a 60-min hard
# ceiling via HEADLESS_TIMEOUT_SECONDS, so the ladder is calibrated against
# that ceiling rather than per-tier (worker may not have emitted scope yet).
WAIT_AGE_MIN = 30  # below this, almost always healthy
PEEK_AGE_MIN = 45  # above this, peek even if active
STOP_AGE_MIN = 55  # approaching timeout — stop or hand off
IDLE_PEEK_MIN = 7  # idle this long with no PR → check for stall
IDLE_STALL_MIN = 15  # idle this long → likely stuck
IDLE_POST_PR_MIN = 5  # idle this long after PR shipped → stuck in stage5


def load_running_tasks(client: str | None) -> list[dict[str, Any]]:
    if not DEV_QUEUE.exists():
        return []
    data = json.loads(DEV_QUEUE.read_text())
    tasks = data.get("tasks", [])
    out = []
    for t in tasks:
        if t.get("status") != "running":
            continue
        if client and t.get("client") != client:
            continue
        out.append(t)
    return out


def find_transcript_for_ticket(ticket_id: str) -> Path | None:
    """Locate the main /auto-dev transcript jsonl for a ticket.

    A worker's project directory can contain multiple jsonls — the main
    /auto-dev session plus one jsonl per fork-session subagent. The main
    session has the earliest first user message (it spawned the others)
    and its first user message references "/auto-dev <ticket>".
    """
    if not CLAUDE_PROJECTS.exists():
        return None
    candidates: list[tuple[Path, str]] = []  # (path, first_user_ts)
    for proj in CLAUDE_PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        if f"auto-dev-{ticket_id}" not in proj.name:
            continue
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
            # Prefer jsonls whose first user message references /auto-dev
            score = 0 if f"/auto-dev {ticket_id}" in first_user_text else 1
            candidates.append((jsonl, f"{score}-{first_user_ts}"))
    if not candidates:
        return None
    # Sort: prefer /auto-dev-prefixed (score 0), then earliest first_user_ts
    candidates.sort(key=lambda c: c[1])
    return candidates[0][0]


def parse_transcript(path: Path) -> dict[str, Any]:
    """Walk the jsonl, return first/last activity timestamps + last sentinel status."""
    first_user_ts: str | None = None
    last_asst_ts: str | None = None
    last_sentinel_status: str | None = None
    last_sentinel_stage: str | None = None
    last_pr_number: int | None = None
    with path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            ts = d.get("timestamp")
            if t == "user" and not first_user_ts and ts:
                first_user_ts = ts
            if t == "assistant" and ts:
                last_asst_ts = ts
                # Look for sentinel in the assistant message text
                msg = d.get("message", {})
                contents = msg.get("content", [])
                if isinstance(contents, list):
                    for c in contents:
                        if not isinstance(c, dict):
                            continue
                        text = c.get("text", "") if c.get("type") == "text" else ""
                        # Look for sentinel in two forms:
                        # 1. AUTO_DEV_RESULT<<<...>>> contract markers
                        # 2. Code-fenced JSON with schema_version + status
                        bodies: list[str] = [
                            m.group(1)
                            for m in re.finditer(
                                r"AUTO_DEV_RESULT\s*\n(.*?)AUTO_DEV_RESULT",
                                text,
                                re.DOTALL,
                            )
                        ]
                        bodies.extend(
                            m.group(1)
                            for m in re.finditer(
                                r'```(?:json)?\s*(\{[^`]*?"schema_version"[^`]*?\})\s*```',
                                text,
                                re.DOTALL,
                            )
                        )
                        for body in bodies:
                            status_m = re.search(r'"status":\s*"([^"]+)"', body)
                            stage_m = re.search(r'"stage_reached":\s*"([^"]+)"', body)
                            pr_m = re.search(
                                r'"pr_number":\s*(\d+)', body
                            ) or re.search(r'"number":\s*(\d+)', body)
                            if status_m:
                                last_sentinel_status = status_m.group(1)
                            if stage_m:
                                last_sentinel_stage = stage_m.group(1)
                            if pr_m:
                                last_pr_number = int(pr_m.group(1))
    return {
        "first_user_ts": first_user_ts,
        "last_asst_ts": last_asst_ts,
        "last_sentinel_status": last_sentinel_status,
        "last_sentinel_stage": last_sentinel_stage,
        "last_pr_number": last_pr_number,
    }


def gh_pr_state(pr_number: int) -> str:
    """Returns OPEN | MERGED | CLOSED | UNKNOWN."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return "UNKNOWN"
        data = json.loads(result.stdout)
        return data.get("state", "UNKNOWN")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return "UNKNOWN"


def minutes_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        ts = dt.datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return (NOW - ts).total_seconds() / 60.0


def recommend(
    age_min: float | None,
    idle_min: float | None,
    pr_state: str | None,
    sentinel_status: str | None,
    attempts: int,
) -> tuple[str, str]:
    """Return (recommendation, reasoning)."""
    if age_min is None:
        return ("PEEK", "no transcript timestamps — verify session is alive")

    # Stuck post-PR-merge — the canonical "worker forgot to exit" pattern
    if pr_state == "MERGED" and idle_min is not None and idle_min > IDLE_POST_PR_MIN:
        return (
            "STOP",
            f"PR merged + worker idle {idle_min:.0f}min — stuck in post-create wait",
        )

    # Healthy stage-5 wait: shipped sentinel + PR open + auto-merge pending CI
    if sentinel_status == "shipped" and pr_state == "OPEN":
        if idle_min is not None and idle_min > IDLE_STALL_MIN:
            return (
                "PEEK",
                f"shipped + PR open but idle {idle_min:.0f}min — CI may be hung",
            )
        return ("WAIT", "shipped + PR open — auto-merge CI in progress")

    # Approaching hard ceiling
    if age_min > STOP_AGE_MIN:
        return (
            "STOP",
            f"age {age_min:.0f}min approaches 60-min timeout — stop or hand off",
        )

    # Retry loop
    if attempts >= 3:
        return ("STOP", f"attempt {attempts} — systemic, not transient")

    # Long stall without PR
    if idle_min is not None and idle_min > IDLE_STALL_MIN and not pr_state:
        return (
            "STOP-OR-PEEK",
            f"idle {idle_min:.0f}min, no PR — likely stuck; manual peek before stop",
        )

    # Moderate stall
    if idle_min is not None and idle_min > IDLE_PEEK_MIN and not pr_state:
        return ("PEEK", f"idle {idle_min:.0f}min, no PR — check for tool denial")

    # Mature but progressing
    if age_min > PEEK_AGE_MIN:
        return ("PEEK", f"age {age_min:.0f}min mature — check stage")

    if age_min > WAIT_AGE_MIN:
        return ("WAIT", f"age {age_min:.0f}min in normal range")

    return ("WAIT", f"age {age_min:.0f}min — early/healthy")


def format_row(t: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    age = minutes_since(info.get("first_user_ts"))
    idle = minutes_since(info.get("last_asst_ts"))
    pr_state = None
    if info.get("last_pr_number"):
        pr_state = gh_pr_state(info["last_pr_number"])
    rec, reason = recommend(
        age, idle, pr_state, info.get("last_sentinel_status"), t.get("attempts", 1)
    )
    return {
        "ticket": t.get("ticket_id"),
        "session": (t.get("session_id") or "-")[:12],
        "client": t.get("client"),
        "attempts": t.get("attempts", 1),
        "age_min": round(age, 1) if age is not None else None,
        "idle_min": round(idle, 1) if idle is not None else None,
        "stage": info.get("last_sentinel_stage"),
        "status": info.get("last_sentinel_status"),
        "pr": info.get("last_pr_number"),
        "pr_state": pr_state,
        "recommend": rec,
        "reason": reason,
    }


def print_table(rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        print("No RUNNING tasks found.")
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
    print(header)
    print("-" * len(header))
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
        print("  ".join(cells))
        if r.get("recommend") != "WAIT":
            print(f"    └─ {r.get('reason')}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--client", help="Filter to one client", default=None)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = p.parse_args()

    tasks = load_running_tasks(args.client)
    rows = []
    for t in tasks:
        ticket = t.get("ticket_id")
        transcript = find_transcript_for_ticket(str(ticket))
        info = parse_transcript(transcript) if transcript else {}
        rows.append(format_row(t, info))

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print_table(rows)
        actionable = [r for r in rows if r["recommend"].startswith("STOP")]
        if actionable:
            print()
            print("Suggested stops:")
            for r in actionable:
                print(
                    f"  cw spawn close {r['session']}  # #{r['ticket']} — {r['reason']}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
