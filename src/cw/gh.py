"""GitHub CLI helpers for cw."""

from __future__ import annotations

import json
import re
import subprocess as _sp
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as _urlquote

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    # Why: branch carries ticket_id, and some trackers emit `repo#N` ids. An
    # unencoded '#' is read as a URL fragment, so `heads/dev/redact-api#1`
    # would query `heads/dev/redact-api` — a different ref, silently. Encode
    # everything except '/', which is a real separator in the refs path.
    ref_path = _urlquote(branch, safe="/")
    try:
        result = _sp.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/git/refs/heads/{ref_path}",
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


def current_gh_login(*, timeout: int) -> str | None:
    """Return the login of the currently-authenticated ``gh`` identity.

    This is the identity ``auto-dev-plan`` posts plan-review comments as
    (cw is a single-user tool with no separate bot account, so "the
    operator's own gh login" doubles as the trusted commenter). Returns
    None on any failure to resolve it — gh binary absent, non-zero exit,
    a timeout, or empty stdout after stripping. Callers MUST fail closed
    on None; do not treat it as "trust anyone."

    Also the identity source for :mod:`cw.operator_identity`'s
    process-lifetime GitHub-login cache (RFC 0011 S1).
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


def _fetch_issue_comments(ticket_id: str, timeout: int) -> list[dict[str, Any]] | None:
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


def _comment_has_marker(comment: dict[str, Any]) -> bool:
    """Return True if *comment*'s body contains the plan-review marker."""
    body = comment.get("body", "")
    return isinstance(body, str) and _PLAN_MARKER in body


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

    if not any(_comment_has_marker(c) for c in comments):
        return None

    trusted_login = current_gh_login(timeout=timeout)
    if trusted_login is None:
        return None

    for comment in reversed(comments):
        body = comment.get("body", "")
        if not (isinstance(body, str) and _comment_has_marker(comment)):
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


_CREATE_TIMEOUT = 30
_ISSUE_URL_NUMBER_RE = re.compile(r"/(\d+)\s*$")


@contextmanager
def _body_file(body: str) -> Iterator[Path]:
    """Write *body* to a temp file for --body-file.

    Why: issue/milestone bodies and titles carry em-dashes, ampersands, and
    backticks. Passing them on argv invites a quoting bug on every call; a
    --body-file never can.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", encoding="utf-8", delete=False
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def create_issue(
    title: str,
    body: str,
    *,
    labels: list[str],
    milestone: int,
    timeout: int = _CREATE_TIMEOUT,
) -> int | None:
    """Create an issue and attach it to *milestone*; return its number, or None.

    Two calls, deliberately. ``gh issue create --milestone`` resolves a milestone
    BY NAME (``-m, --milestone name``), not by id — handing it ``str(11)`` would
    look for a milestone *titled* "11" and fail. ``gh issue edit -m`` is name-only
    too. So the milestone is attached afterwards through the REST endpoint, which
    does take the numeric id. This keeps ``milestone: int`` in the signature (the
    id is what ``apply_plan`` holds, and it is unambiguous where a title is not).

    ``-F`` (not ``-f``) sends a *typed* field, so ``milestone`` arrives as a JSON
    number rather than the string "11", which is what the API expects.
    """
    cmd = ["gh", "issue", "create", "--title", title]
    for label in labels:
        cmd += ["--label", label]
    try:
        with _body_file(body) as path:
            cmd += ["--body-file", str(path)]
            result = _sp.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = _ISSUE_URL_NUMBER_RE.search(result.stdout.decode("utf-8", "replace"))
    if not match:
        return None
    number = int(match.group(1))
    return number if _attach_milestone(number, milestone, timeout) else None


def _attach_milestone(number: int, milestone: int, timeout: int) -> bool:
    """Attach *number* to *milestone* by numeric id; confirm it actually stuck.

    Why read the response back instead of trusting the exit code: GitHub's
    "Update an issue" reference states that "without push access to the
    repository, milestone changes are silently dropped" — a 200 with the
    milestone simply not applied. Exit-code-only would report success and leave
    the issue off the milestone, which is precisely the half-applied buildout
    apply_plan exists to prevent. So confirm the echoed milestone number.
    """
    try:
        result = _sp.run(
            [
                "gh", "api",
                f"repos/{{owner}}/{{repo}}/issues/{number}",
                "-X", "PATCH",
                "-F", f"milestone={milestone}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return False
    attached = payload.get("milestone") if isinstance(payload, dict) else None
    if not isinstance(attached, dict):
        return False
    return attached.get("number") == milestone


def update_issue_body(number: int, body: str, *, timeout: int = _CREATE_TIMEOUT) -> bool:
    """Replace an issue's body via ``gh issue edit --body-file``. True on success."""
    try:
        with _body_file(body) as path:
            result = _sp.run(
                ["gh", "issue", "edit", str(number), "--body-file", str(path)],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except (OSError, _sp.TimeoutExpired):
        return False
    return result.returncode == 0


def create_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> int | None:
    """Create a milestone via the REST API; return its number, or None on failure."""
    try:
        result = _sp.run(
            [
                "gh", "api", "repos/{owner}/{repo}/milestones",
                "-f", f"title={title}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    number = payload.get("number") if isinstance(payload, dict) else None
    return number if isinstance(number, int) else None


def find_milestone(title: str, *, timeout: int = _CREATE_TIMEOUT) -> tuple[int | None, bool]:
    """Return (number, ok) for an existing milestone titled *title*, open OR closed.

    ``?state=all`` is load-bearing, not decoration. GitHub's "List milestones"
    defaults ``state`` to ``open``, so without it a milestone that has been
    CLOSED (which is what happens to a sprint's milestone once the sprint ends)
    is invisible here — apply_plan would read that as "no such milestone",
    create a SECOND one with the same title, and orphan the issues filed under
    the first. That is precisely the duplicate-filing this function exists to
    prevent. It goes in the path as a query string: ``-f state=all`` would flip
    gh's auto-method from GET to POST.

    ``&per_page=100`` is equally load-bearing. ``gh api`` pagination is
    opt-in — without an explicit ``per_page``, GitHub returns only the first
    page (30 items) of milestones. A repo with more than 30 milestones would
    silently drop the older ones from this scan, reintroducing the exact
    duplicate-milestone bug ``?state=all`` was added to prevent.

    ``ok=False`` means the gh call itself failed (non-zero exit, OSError,
    timeout) — the caller cannot conclude the milestone is absent, only that it
    could not check. ``ok=True`` with ``number=None`` is a genuine miss: the
    call succeeded and no milestone with this title exists yet. Conflating
    these two is exactly what breaks idempotency on a re-run after a transient
    gh failure (see ``cw.sprint.apply_plan``).
    """
    try:
        result = _sp.run(
            [
                "gh", "api",
                "repos/{owner}/{repo}/milestones?state=all&per_page=100",
                "--jq", ".[] | {number, title}",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("title") == title and isinstance(entry.get("number"), int):
            number: int = entry["number"]
            return number, True
    return None, True


def milestone_issue_titles(
    milestone: int, *, timeout: int = _CREATE_TIMEOUT
) -> tuple[dict[str, int] | None, bool]:
    """Return ({issue title: number}, ok) for every issue on *milestone*.

    ``ok=False`` means the gh call itself failed (non-zero exit, OSError,
    timeout, unparseable JSON) — this is what makes ``cw sprint apply``
    idempotent: a caller must not read a failed lookup as "milestone has no
    issues yet" and re-file everything as duplicates. On success the dict may
    legitimately be empty (milestone exists but nothing has been filed under it
    yet); that is ``({}, True)``, not ``(None, True)``.

    Note: if two issues under *milestone* share the exact same title, this dict
    comprehension keeps only the last one seen. The idempotent re-entry check
    in ``apply_plan`` assumes ticket/epic titles are unique within a milestone.

    Note also the ``--limit 200`` cap below: only the 200 most recent issues
    under *milestone* are considered. A milestone with more than 200 issues
    filed against it will silently omit the older ones from the returned dict.

    Passing the numeric id to ``gh issue list --milestone`` is correct here and
    is NOT the same bug as in ``create_issue``: list documents its flag as
    "Filter by milestone number or title", whereas ``issue create``/``issue
    edit`` take a name only. Do not "fix" this one.
    """
    try:
        result = _sp.run(
            [
                "gh", "issue", "list",
                "--milestone", str(milestone),
                "--state", "all",
                "--limit", "200",
                "--json", "number,title",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None, False
    if result.returncode != 0:
        return None, False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(payload, list):
        return None, False
    titles = {
        str(item["title"]): int(item["number"])
        for item in payload
        if isinstance(item, dict) and "title" in item and "number" in item
    }
    return titles, True
