"""GitHub CLI helpers for cw."""

from __future__ import annotations

import json
import subprocess as _sp
from typing import Any

# Lookback window (days) for timed_out-merged detection.
TIMED_OUT_MERGED_LOOKBACK_DAYS = 7

_GH_PR_STATE_MERGED = "MERGED"


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
        issue_result = _sp.run(
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
        return None, False
    except (OSError, _sp.TimeoutExpired):
        return None, True

    if issue_result.returncode != 0:
        return None, True

    try:
        data: dict[str, Any] = json.loads(issue_result.stdout)
        pr_refs: list[dict[str, Any]] = data.get("closedByPullRequestsReferences") or []
    except (ValueError, AttributeError):
        return None, True

    for ref in pr_refs:
        pr_number = ref.get("number")
        if pr_number is None:
            continue
        try:
            pr_result = _sp.run(
                ["gh", "pr", "view", str(pr_number), "--json", "state"],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, _sp.TimeoutExpired):
            continue
        if pr_result.returncode != 0:
            continue
        try:
            pr_data: dict[str, Any] = json.loads(pr_result.stdout)
        except ValueError:
            continue
        if pr_data.get("state") == _GH_PR_STATE_MERGED:
            return True, True

    return False, True
