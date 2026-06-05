"""GitHub CLI helpers for cw."""

from __future__ import annotations

import json
import subprocess as _sp
from typing import Any

_GH_PR_STATE_MERGED = "MERGED"


def _fetch_issue_pr_refs(ticket_id: str, timeout: int) -> list[dict[str, Any]] | None:
    """Return the list of PR refs linked to ticket_id, or None on any error."""
    try:
        result = _sp.run(
            [
                "gh",
                "issue",
                "view",
                ticket_id,
                "--json",
                "closedByPullRequestsReferences",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise
    except (OSError, _sp.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        data: dict[str, Any] = json.loads(result.stdout)
        return data.get("closedByPullRequestsReferences") or []
    except (ValueError, AttributeError):
        return None


def _fetch_pr_state(pr_number: int, timeout: int) -> str | None:
    """Return the state string for the given PR number, or None on any error."""
    try:
        result = _sp.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, _sp.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        pr_data: dict[str, Any] = json.loads(result.stdout)
        return str(pr_data.get("state", ""))
    except ValueError:
        return None


def pr_is_merged_for_ticket(
    ticket_id: str, *, timeout: int = 10
) -> tuple[bool | None, bool]:
    """Return (merged, gh_available).

    merged:
      True   — at least one PR linked to ticket_id is MERGED
      False  — linked PRs found but none are MERGED
      None   — transient error (timeout, non-zero exit, JSON parse failure)

    gh_available:
      False  — gh binary not found (FileNotFoundError / OSError on exec)
      True   — binary present (even if the call failed transiently)
    """
    try:
        refs = _fetch_issue_pr_refs(ticket_id, timeout)
    except FileNotFoundError:
        return None, False

    if refs is None:
        return None, True

    for ref in refs:
        pr_number = ref.get("number")
        if pr_number is None:
            continue
        state = _fetch_pr_state(int(pr_number), timeout)
        if state == _GH_PR_STATE_MERGED:
            return True, True

    return False, True
