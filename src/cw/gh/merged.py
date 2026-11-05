"""Whether a ticket's PR has merged."""

from __future__ import annotations

import json
import subprocess as _sp
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cw.models import PrState
    from cw.models.tasks import TicketTask

from cw.gh.pr_reads import _GH_PR_STATE_MERGED, _fetch_pr_state

# Lookback window for timed_out-merged detection, shared by doctor.py and reconcile.py.
# Lives here (co-located with pr_is_merged_for_ticket) to avoid a circular import:
# doctor.py imports from reconcile.py, so reconcile.py cannot import from doctor.py.
TIMED_OUT_MERGED_LOOKBACK_DAYS = 7


def _fetch_issue_pr_refs(
    ticket_id: str, timeout: int, *, cwd: Path | None = None
) -> list[dict[str, Any]] | None:
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
            cwd=cwd,
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


def _fetch_branch_merged_pr(
    branch: str, timeout: int, *, cwd: Path | None = None
) -> tuple[bool | None, bool]:
    """Return (merged, gh_available) for the head branch via gh pr list.

    Checks whether any MERGED PR exists for *branch* using
    ``gh pr list --head <branch> --state merged``.

    Returns:
      (True, True)   -- at least one merged PR found
      (False, True)  -- no merged PRs found (empty list)
      (None, True)   -- transient error (timeout, non-zero exit, JSON parse failure)
      (None, False)  -- gh binary not found (FileNotFoundError)
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
                "merged",
                "--json",
                "number",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
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


def pr_is_merged_for_ticket(
    ticket_id: str,
    *,
    branch: str | None = None,
    timeout: int = 10,
    cwd: Path | None = None,
) -> tuple[bool | None, bool]:
    """Return (merged, gh_available).

    Decision table:

      issue-link finds a MERGED ref          -> (True,  True)  branch not consulted
      issue-link list, no MERGED ref         -> (False, True)  branch not consulted
      issue-link FileNotFoundError (gh gone) -> (None,  False) branch not consulted
      refs is None AND branch is None        -> (None,  True)  (today's behaviour)
      refs is None AND branch provided       -> branch path result directly:
          merged                             -> (True,  True)
          no merged PRs                      -> (False, True)
          transient error                    -> (None,  True)
          gh binary absent                   -> (None,  False)

    Args:
      ticket_id: GitHub issue number or Linear ticket id (e.g. "487" or "GEN-403").
      branch: Optional head branch to consult when issue-link is unsupported
              (e.g. ``"dev/" + ticket_id``).  Callers are responsible for
              building this value; gh.py does not import from cw.reconcile
              to avoid a circular import.
      timeout: Subprocess timeout in seconds (applies to each gh call).
      cwd: Directory to scope every ``gh`` subprocess call to (``gh``
           autodetects the target repo from its cwd's git remote). Defaults
           to None, which preserves today's ambient-CWD behaviour — the
           calling process's own directory decides which repo ``gh`` talks
           to. Callers that operate on multiple clients' repos MUST pass the
           client's repo directory here; leaving it None risks scoping a gh
           call to the wrong repo when the ambient CWD belongs to a
           different client (GitHub #1269).

    merged:
      True   -- at least one PR linked to ticket_id is MERGED
      False  -- linked PRs found but none are MERGED
      None   -- transient error (timeout, non-zero exit, JSON parse failure)

    gh_available:
      False  -- gh binary not found (FileNotFoundError)
      True   -- binary present (even if the call failed transiently)
    """
    try:
        refs = _fetch_issue_pr_refs(ticket_id, timeout, cwd=cwd)
    except FileNotFoundError:
        return None, False

    if refs is None:
        if branch is None:
            return None, True
        return _fetch_branch_merged_pr(branch, timeout, cwd=cwd)

    for ref in refs:
        pr_number = ref.get("number")
        if pr_number is None:
            continue
        try:
            state = _fetch_pr_state(int(pr_number), timeout, cwd=cwd)
        except FileNotFoundError:
            return None, False
        if state == _GH_PR_STATE_MERGED:
            return True, True

    return False, True


def pr_state_is_fresh(
    pr_state: PrState, max_age_seconds: int, *, now: datetime | None = None
) -> bool:
    """True if *pr_state* was hydrated less than *max_age_seconds* ago.

    Same strict-`<` arithmetic as cw.pr_hydrate._throttled, reused here
    so callers can gate a persisted pr_state read on the same staleness
    window as the hydration pass itself (GitHub #975).
    """
    now = now or datetime.now(UTC)
    return (now - pr_state.hydrated_at).total_seconds() < max_age_seconds


def pr_is_merged_from_state(
    pr_state: PrState | None, *, max_age_seconds: int, now: datetime | None = None
) -> bool | None:
    """Return the merged verdict from a fresh persisted PrState, else None.

    GitHub #975: lets a caller that already holds a task's/watched-PR's
    hydrated pr_state (from cw.pr_hydrate's serve-tick pass, GitHub #929)
    skip pr_is_merged_for_ticket's own gh subprocess call entirely when
    that state is fresh. None means "no confident answer available from
    persisted state" (absent or stale) -- caller must fall back to
    pr_is_merged_for_ticket.
    """
    if pr_state is None:
        return None
    if not pr_state_is_fresh(pr_state, max_age_seconds, now=now):
        return None
    return pr_state.state == _GH_PR_STATE_MERGED


def resolve_merged_via_pr_state(
    ticket_id: str,
    client: str,
    task_by_ticket: dict[tuple[str, str], TicketTask],
    max_age_seconds: int,
    gh_fallback: Callable[[], tuple[bool | None, bool]],
) -> tuple[bool | None, bool]:
    """Resolve a ticket's merged verdict from persisted pr_state, else gh_fallback.

    GitHub #975: the single shared seam for "check task_by_ticket's fresh
    pr_state first, fall back to a gh subprocess call when stale/absent" --
    used identically by reconcile/prepasses.py's merge-state pre-pass,
    reconcile/tasks.py's _filter_merged_candidates, and
    doctor/loop_health.py's _check_timed_out_merged.

    gh_fallback is a zero-arg callable -- each call site passes a
    functools.partial binding its own ticket_id/branch/cwd args and its
    own pr_is_merged_for_ticket-shaped reference (reconcile's _deps
    indirection or doctor/loop_health.py's direct cw.gh import) -- so
    this function stays agnostic of pr_is_merged_for_ticket's own
    signature and every call site keeps its existing monkeypatch seam
    intact.
    """
    task = task_by_ticket.get((client, ticket_id))
    from_state = pr_is_merged_from_state(
        task.pr_state if task is not None else None,
        max_age_seconds=max_age_seconds,
    )
    if from_state is not None:
        return from_state, True
    return gh_fallback()
