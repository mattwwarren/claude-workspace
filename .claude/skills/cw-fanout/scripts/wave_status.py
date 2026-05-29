#!/usr/bin/env python3
"""Wave lifecycle rollup for /cw-fanout.

Reports the per-ticket dev-queue lifecycle state for a single dispatch wave
(the ticket set passed on the command line) plus a ``terminal`` rollup that
tells the monitor loop when every ticket has reached a terminal state and the
wave is done.

Distinct from ``cw_queue_peek.py``: that script inspects the *health* of
RUNNING worker sessions (age, idle gap, stuck-post-merge) to recommend
WAIT/PEEK/STOP. This one answers the orthogonal question — *what lifecycle
state is each ticket in, and is the whole batch finished?* — across all states
(pending, running, completed, blocked_on_user, ...), not just RUNNING. The two
compose: wave_status decides when to stop monitoring; cw_queue_peek decides
what to do with the sessions still in flight.

Lightweight by design: reads ``~/.local/share/cw/dev_queue.json`` directly (no
``uv`` / cw import) so it is cheap to call repeatedly during monitoring.

Output: a table on stdout, or a JSON object with ``--json``. Exit code is 0
when the wave is terminal (every ticket done), 1 when at least one ticket is
still in flight (pending or running).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEV_QUEUE = Path.home() / ".local/share/cw/dev_queue.json"

# Terminal dev-queue states: the dispatcher will not advance these on its own.
# blocked_on_user means a session paused for operator input (needs attention);
# failed/cancelled are dead ends; completed shipped or no_op'd.
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "blocked_on_user"})
# In-flight states the dispatcher is still driving.
IN_FLIGHT_STATES = frozenset({"pending", "running"})
# Synthetic state for a wave ticket no longer present in the queue at all —
# typically completed and then retired/removed. Treated as terminal.
_ABSENT = "absent"
_SESSION_SHORT_LEN = 12


def _load_tasks() -> list[dict[str, Any]]:
    if not DEV_QUEUE.exists():
        return []
    try:
        data = json.loads(DEV_QUEUE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    return [t for t in tasks if isinstance(t, dict)]


def _row_for_ticket(
    ticket: str, client: str | None, tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    for task in tasks:
        if str(task.get("ticket_id")) != ticket:
            continue
        if client and task.get("client") != client:
            continue
        session_id = task.get("session_id") or ""
        short = session_id[:_SESSION_SHORT_LEN] if isinstance(session_id, str) else ""
        return {
            "ticket_id": ticket,
            "state": task.get("status", "unknown"),
            "attempts": task.get("attempts", 0),
            "session_id": short,
            "client": task.get("client", client),
        }
    return {
        "ticket_id": ticket,
        "state": _ABSENT,
        "attempts": 0,
        "session_id": "",
        "client": client,
    }


def _is_terminal(state: str) -> bool:
    return state == _ABSENT or state in TERMINAL_STATES


def build_report(tickets: list[str], client: str | None) -> dict[str, Any]:
    tasks = _load_tasks()
    rows = [_row_for_ticket(t.lstrip("#"), client, tasks) for t in tickets]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    in_flight = [row["ticket_id"] for row in rows if not _is_terminal(row["state"])]
    needs_attention = [
        row["ticket_id"] for row in rows if row["state"] == "blocked_on_user"
    ]
    return {
        "client": client,
        "terminal": not in_flight,
        "counts": counts,
        "in_flight": in_flight,
        "needs_attention": needs_attention,
        "tickets": rows,
    }


def _print_table(report: dict[str, Any]) -> None:
    print(f"{'TICKET':<10} {'STATE':<16} {'ATT':>3}  SESSION")
    print("-" * 44)
    for row in report["tickets"]:
        print(
            f"{row['ticket_id']:<10} {row['state']:<16} "
            f"{row['attempts']:>3}  {row['session_id']}"
        )
    counts = ", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items()))
    print(f"\ncounts: {counts}")
    if report["needs_attention"]:
        flagged = ", ".join(report["needs_attention"])
        print(f"needs attention (blocked_on_user): {flagged}")
    print(f"wave terminal: {report['terminal']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wave lifecycle rollup for /cw-fanout."
    )
    parser.add_argument(
        "tickets", nargs="+", help="Ticket ids in the wave (e.g. 201 202 203)."
    )
    parser.add_argument(
        "--client", "-c", default=None, help="Filter to this cw client."
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit JSON instead of a table.",
    )
    args = parser.parse_args()

    report = build_report(args.tickets, args.client)
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        _print_table(report)
    return 0 if report["terminal"] else 1


if __name__ == "__main__":
    sys.exit(main())
