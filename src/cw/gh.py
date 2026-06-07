"""GitHub CLI helpers for cw."""

from __future__ import annotations

import json
import subprocess as _sp
from typing import Any

_GH_PR_STATE_MERGED = "MERGED"
_PR_EXISTS_TIMEOUT = 10

# Lookback window for timed_out-merged detection, shared by doctor.py and reconcile.py.
# Lives here (co-located with pr_is_merged_for_ticket) to avoid a circular import:
# doctor.py imports from reconcile.py, so reconcile.py cannot import from doctor.py.
TIMED_OUT_MERGED_LOOKBACK_DAYS = 7


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
    except FileNotFoundError:
        raise
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
      False  — gh binary not found (FileNotFoundError)
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
        try:
            state = _fetch_pr_state(int(pr_number), timeout)
        except FileNotFoundError:
            return None, False
        if state == _GH_PR_STATE_MERGED:
            return True, True

    return False, True


def pr_exists_for_branch(
    branch: str, *, timeout: int = _PR_EXISTS_TIMEOUT
) -> tuple[bool | None, bool]:
    """Return (open_pr_exists, gh_available).

    open_pr_exists:
      True   — an open PR exists for this branch
      False  — no open PR
      None   — transient error (timeout, non-zero exit, JSON parse failure)

    gh_available:
      False  — gh binary not found (FileNotFoundError)
      True   — binary present (even if the call failed transiently)
    """
    try:
        result = _sp.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, False
    except (OSError, _sp.TimeoutExpired):
        return None, True

    if result.returncode != 0:
        return None, True

    try:
        data: list[dict[str, object]] = json.loads(result.stdout)
        return len(data) > 0, True
    except (ValueError, AttributeError):
        return None, True
