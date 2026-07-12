"""GitHub CLI helpers for cw."""

from __future__ import annotations

import json
import subprocess as _sp
from typing import Any

_GH_PR_STATE_MERGED = "MERGED"
_PR_EXISTS_TIMEOUT = 10
_PLAN_MARKER = "<!-- plan-spec-reviewed"
_FETCH_COMMENTS_TIMEOUT = 30
_POST_COMMENT_TIMEOUT_SECONDS = 30
_PR_VIEW_TIMEOUT = 15
# Field list hydrated per PR by the serve-tick pass (GitHub #929). Order is
# asserted by tests/test_gh.py — keep it in sync with the decision-table spec.
_PR_VIEW_FIELDS = (
    "state,mergeable,mergeStateStatus,statusCheckRollup,"
    "reviewDecision,isDraft,reviewRequests"
)

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


def fetch_pr_view(
    pr_ref: str, *, timeout: int = _PR_VIEW_TIMEOUT
) -> dict[str, Any] | None:
    """Return the parsed ``gh pr view --json`` response for *pr_ref*, or None.

    *pr_ref* may be a full PR URL (``gh`` infers owner/repo) or a PR number.
    Fetches the fixed ``_PR_VIEW_FIELDS`` set consumed by ``cw.pr_hydrate``.

    Returns None on ANY failure — non-zero exit, timeout, malformed JSON, or a
    missing ``gh`` binary — so the best-effort hydration pass never raises.
    """
    try:
        result = _sp.run(
            ["gh", "pr", "view", pr_ref, "--json", _PR_VIEW_FIELDS],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, _sp.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except (ValueError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _fetch_branch_merged_pr(branch: str, timeout: int) -> tuple[bool | None, bool]:
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
    ticket_id: str, *, branch: str | None = None, timeout: int = 10
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

    merged:
      True   -- at least one PR linked to ticket_id is MERGED
      False  -- linked PRs found but none are MERGED
      None   -- transient error (timeout, non-zero exit, JSON parse failure)

    gh_available:
      False  -- gh binary not found (FileNotFoundError)
      True   -- binary present (even if the call failed transiently)
    """
    try:
        refs = _fetch_issue_pr_refs(ticket_id, timeout)
    except FileNotFoundError:
        return None, False

    if refs is None:
        if branch is None:
            return None, True
        return _fetch_branch_merged_pr(branch, timeout)

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


def _fetch_branch_exists_on_origin(
    branch: str, timeout: int
) -> tuple[bool | None, bool]:
    """Return (exists, gh_available) for *branch* via ``gh api`` refs endpoint.

    Returns:
      (True, True)   — branch present on origin
      (False, True)  — branch absent on origin (404)
      (None, True)   — transient error (unexpected non-zero, JSON parse failure)
      (None, False)  — gh binary not found (FileNotFoundError)
    """
    try:
        result = _sp.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/git/refs/heads/{branch}",
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

    if result.returncode == 0:
        return True, True
    combined = result.stdout + result.stderr
    if "HTTP 404" in combined or '"Not Found"' in combined:
        return False, True
    return None, True


def branch_exists_on_origin(
    branch: str, *, timeout: int = 10
) -> tuple[bool | None, bool]:
    """Check whether *branch* still exists on origin.

    Uses ``gh api repos/{owner}/{repo}/git/refs/heads/{branch}`` (owner/repo
    inferred from the current directory's git remote).

    Returns (exists, gh_available):
      (True, True)   — branch present on origin
      (False, True)  — branch absent on origin
      (None, True)   — transient error; treat as "cannot determine"
      (None, False)  — gh binary not found

    Fail-open: callers must treat (None, *) as "cannot determine".
    """
    return _fetch_branch_exists_on_origin(branch, timeout)


def _current_gh_login(*, timeout: int) -> str | None:
    """Return the login of the currently-authenticated ``gh`` identity.

    This is the identity ``auto-dev-plan`` posts plan-review comments as
    (cw is a single-user tool with no separate bot account, so "the
    operator's own gh login" doubles as the trusted commenter). Returns
    None on any failure to resolve it — gh binary absent, non-zero exit,
    a timeout, or empty stdout after stripping. Callers MUST fail closed
    on None; do not treat it as "trust anyone."
    """
    try:
        result = _sp.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None
    except (OSError, _sp.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    login = result.stdout.strip()
    return login or None


def _fetch_issue_comments(
    ticket_id: str, timeout: int
) -> list[dict[str, Any]] | None:
    """Return the issue's comments list, or None on any fetch/parse error."""
    try:
        result = _sp.run(
            ["gh", "issue", "view", ticket_id, "--json", "comments"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None
    except (OSError, _sp.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        data: dict[str, Any] = json.loads(result.stdout)
        comments: list[dict[str, Any]] = data.get("comments") or []
    except (ValueError, AttributeError):
        return None
    return comments


def fetch_approved_plan_comment(
    ticket_id: str, *, timeout: int = _FETCH_COMMENTS_TIMEOUT
) -> str | None:
    """Return the body of the latest approved plan comment on a GitHub issue.

    Scans issue comments in reverse order (newest first) for the first one
    containing the ``<!-- plan-spec-reviewed`` marker written by auto-dev-plan
    AND authored by the currently-authenticated ``gh`` identity. A
    marker-bearing comment from any other commenter is skipped (not
    scan-terminating) — it does not count as "no reviewed plan," it simply
    isn't authoritative, so scanning continues for an older trusted match.

    Returns None when:
    - gh binary is absent or returns an error
    - the response cannot be parsed
    - no comment carries the plan marker (Stage 1 not yet complete)
    - no marker-bearing comment is authored by the currently-authenticated
      ``gh`` identity, or that identity cannot be resolved (fail-closed)
    """
    comments = _fetch_issue_comments(ticket_id, timeout)
    if comments is None:
        return None

    if not any(
        isinstance(c.get("body", ""), str) and _PLAN_MARKER in c["body"]
        for c in comments
    ):
        return None

    trusted_login = _current_gh_login(timeout=timeout)
    if trusted_login is None:
        return None

    for comment in reversed(comments):
        body = comment.get("body", "")
        if not (isinstance(body, str) and _PLAN_MARKER in body):
            continue
        author = comment.get("author")
        if isinstance(author, dict) and author.get("login") == trusted_login:
            return body
    return None


def post_issue_comment(
    ticket_id: str, body: str, *, timeout: int = _POST_COMMENT_TIMEOUT_SECONDS
) -> _sp.CompletedProcess[bytes] | None:
    """Post *body* as a GitHub issue comment via ``gh issue comment``.

    Returns the completed subprocess (inspect .returncode / .stderr) or None
    if the subprocess could not run / timed out. Policy-free: callers decide
    whether to log or swallow — this neither logs nor raises on gh failure.
    """
    try:
        return _sp.run(
            ["gh", "issue", "comment", ticket_id, "--body", body],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None


def add_pr_reviewer(
    pr_ref: str, reviewer: str, *, timeout: int = _POST_COMMENT_TIMEOUT_SECONDS
) -> _sp.CompletedProcess[bytes] | None:
    """Request *reviewer* on *pr_ref* via ``gh pr edit --add-reviewer``.

    *pr_ref* may be a full PR URL (``gh`` infers owner/repo) or a PR number;
    *reviewer* is a GitHub login or an ``org/team`` slug. Mirrors
    ``post_issue_comment``: returns the completed subprocess (inspect
    ``.returncode`` / ``.stderr``) or None if the subprocess could not run /
    timed out. Policy-free: callers decide whether to log or swallow — this
    neither logs nor raises on gh failure.
    """
    try:
        return _sp.run(
            ["gh", "pr", "edit", pr_ref, "--add-reviewer", reviewer],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None


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
