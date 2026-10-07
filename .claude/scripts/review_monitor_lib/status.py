"""Status reporting subcommands."""

from __future__ import annotations

import json

from review_monitor_lib.models import MonitorState
from review_monitor_lib.state import cmd_list_repos, load_state


def cmd_status(repo: str, as_json: bool = False) -> None:
    """Print the current monitor state.

    If *as_json* is True, print the full state as JSON.
    Otherwise print a human-readable table.
    """
    state = load_state(repo)
    if as_json:
        print(json.dumps(state.to_dict(), indent=2))
        return

    if not state.monitored:
        print("No PRs currently monitored.")
    else:
        print(f"{'PR':<20} {'ROLE':<10} {'STATUS':<12} {'THREADS':<10} {'REVIEW'}")
        print("-" * 70)
        for key, pr in sorted(state.monitored.items()):
            total = len(pr.thread_status)
            addressed = sum(1 for ts in pr.thread_status.values() if ts.is_addressed)
            threads_col = f"{addressed}/{total}" if total else "n/a"
            review_col = "re-review" if pr.awaiting_rereview else ""
            print(
                f"{key:<20} {pr.role:<10} {pr.status:<12}"
                f" {threads_col:<10} {review_col}"
            )

    print(f"\n{len(state.completed)} completed PR(s) in history.")


def cmd_status_all() -> MonitorState:
    """Load and merge state from all repo files in the central directory."""
    repos = cmd_list_repos()
    combined = MonitorState(monitored={}, completed={})
    for repo in repos:
        state = load_state(repo)
        combined.monitored.update(state.monitored)
        combined.completed.update(state.completed)
    return combined
