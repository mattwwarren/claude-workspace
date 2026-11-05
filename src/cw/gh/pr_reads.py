"""PR reads: view, branch PR lookups, PR head, open-PR existence."""

from __future__ import annotations

import json
import subprocess as _sp
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path


_GH_PR_STATE_MERGED = "MERGED"


_GH_PR_STATE_CLOSED = "CLOSED"


_GH_PR_STATE_OPEN = "OPEN"


_PR_EXISTS_TIMEOUT = 10


_PR_VIEW_TIMEOUT = 15


# Field list hydrated per PR by the serve-tick pass (GitHub #929). Order is
# asserted by tests/test_gh.py — keep it in sync with the decision-table spec.
_PR_VIEW_FIELDS = (
    "state,mergeable,mergeStateStatus,statusCheckRollup,"
    "reviewDecision,isDraft,reviewRequests,comments"
)


# Field list read by the RFC 0016 adoption intake (``cw.adopt_pr``) for one
# PR. Deliberately separate from ``_PR_VIEW_FIELDS``: hydration is a throttled
# best-effort observer and must not grow ``headRefOid`` (#2943 R2b). Order is
# asserted by tests/test_gh.py.
PR_ADOPT_FIELDS = (
    "state,isCrossRepository,isDraft,mergeable,mergeStateStatus,"
    "statusCheckRollup,headRefName,headRefOid,baseRefName,url"
)


def github_pr_url(repo: str, pr_number: int) -> str:
    """Build a PR's github.com URL from its ``owner/repo`` slug and number.

    Shared by every ``WatchedPr`` producer (RFC 0011 S2) so the format lives
    in exactly one place; round-trips through ``cw.pr_hydrate._PR_URL_RE``.
    """
    return f"https://github.com/{repo}/pull/{pr_number}"


def _fetch_pr_state(
    pr_number: int, timeout: int, *, cwd: Path | None = None
) -> str | None:
    """Return the state string for the given PR number, or None on any error."""
    try:
        result = _sp.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state"],
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
        pr_data: dict[str, Any] = json.loads(result.stdout)
        return str(pr_data.get("state", ""))
    except ValueError:
        return None


def fetch_pr_view(
    pr_ref: str,
    *,
    timeout: int = _PR_VIEW_TIMEOUT,
    fields: str = _PR_VIEW_FIELDS,
    cwd: Path | None = None,
) -> dict[str, Any] | None:
    """Return the parsed ``gh pr view --json`` response for *pr_ref*, or None.

    *pr_ref* may be a full PR URL (``gh`` infers owner/repo) or a PR number.
    Fetches the ``_PR_VIEW_FIELDS`` set consumed by ``cw.pr_hydrate`` unless
    *fields* names another comma-separated set (``fetch_pr_head`` does); *cwd*
    scopes the call to a client's repo, as for :func:`add_pr_reviewer`.

    Returns None on ANY failure — non-zero exit, timeout, malformed JSON, or a
    missing ``gh`` binary — so the best-effort hydration pass never raises.
    """
    try:
        result = _sp.run(
            ["gh", "pr", "view", pr_ref, "--json", fields],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
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


# Most PRs ``fetch_branch_prs`` asks gh for; a head branch rarely has more.
_BRANCH_PRS_LIMIT = 5


class BranchPr(NamedTuple):
    """One PR whose head is a given branch, as ``gh pr list`` reports it.

    ``state`` is gh's own ``OPEN``/``CLOSED``/``MERGED`` string; ``merged_at``
    is gh's ``mergedAt`` ISO string, or None for an unmerged PR (gh renders it
    as ``null``).
    """

    number: int
    url: str
    state: str
    merged_at: str | None


def _parse_branch_pr(item: object) -> BranchPr | None:
    """Parse one ``gh pr list`` item, or None when its shape is not the contract.

    ``number`` must be a non-bool int, ``url`` and ``state`` strings, and
    ``mergedAt`` a string, ``null`` or absent.
    """
    if not isinstance(item, dict):
        return None
    number = item.get("number")
    url = item.get("url")
    state = item.get("state")
    merged_at = item.get("mergedAt")
    if (
        type(number) is not int
        or not isinstance(url, str)
        or not isinstance(state, str)
        or not (merged_at is None or isinstance(merged_at, str))
    ):
        return None
    return BranchPr(number=number, url=url, state=state, merged_at=merged_at)


def fetch_branch_prs(
    branch: str, timeout: int = _PR_VIEW_TIMEOUT, *, cwd: Path | None = None
) -> tuple[list[BranchPr] | None, bool]:
    """Return (prs, gh_available) for every PR whose head is *branch* (#2715).

    Runs ``gh pr list --head <branch> --state all`` (at most
    ``_BRANCH_PRS_LIMIT`` PRs, in gh's order) with the same ladder as
    :func:`_fetch_branch_merged_pr`:

      (prs, True)   -- the branch's PRs, any state
      ([], True)    -- no PR for the branch
      (None, True)  -- transient error (timeout, OSError, non-zero exit,
                       malformed JSON, a non-list payload, or an item that is
                       not the documented shape)
      (None, False) -- gh binary not found (FileNotFoundError)
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
                "all",
                "--json",
                "number,url,state,mergedAt",
                "--limit",
                str(_BRANCH_PRS_LIMIT),
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
    return _parse_branch_prs(result.stdout), True


def _parse_branch_prs(stdout: str) -> list[BranchPr] | None:
    """Parse ``gh pr list``'s JSON list, or None when any part is malformed."""
    try:
        data = json.loads(stdout)
    except (ValueError, AttributeError):
        return None
    if not isinstance(data, list):
        return None
    prs = [_parse_branch_pr(item) for item in data]
    if any(pr is None for pr in prs):
        return None
    return [pr for pr in prs if pr is not None]


_PR_HEAD_FIELDS = "headRefName,headRefOid,baseRefName,state,autoMergeRequest"


class PrHead(NamedTuple):
    """A PR's head, base branch, state and whether auto-merge is armed (#2771)."""

    ref_name: str
    oid: str
    base_ref_name: str
    state: str
    auto_merge_armed: bool


def fetch_pr_head(
    pr_ref: str, *, timeout: int = _PR_VIEW_TIMEOUT, cwd: Path | None = None
) -> PrHead | None:
    """Read *pr_ref*'s head, base, state and armed state in one ``gh`` call.

    ``gh pr view --json headRefName,headRefOid,baseRefName,state,autoMergeRequest``
    through :func:`fetch_pr_view`: an ``autoMergeRequest`` of ``null`` is
    unarmed, an object is armed. Fails closed to None on anything
    :func:`fetch_pr_view` rejects, a missing ``autoMergeRequest`` key, an
    ``autoMergeRequest`` that is neither null nor an object, or a missing /
    non-string name, oid, base or state, so a caller can never mistake an
    unreadable PR for an unarmed one.
    """
    data = fetch_pr_view(pr_ref, timeout=timeout, fields=_PR_HEAD_FIELDS, cwd=cwd)
    if data is None:
        return None
    return _parse_pr_head(data)


def _parse_pr_head(data: dict[str, Any]) -> PrHead | None:
    """Build a :class:`PrHead` from *data*, or None for any unexpected shape."""
    if "autoMergeRequest" not in data:
        return None
    name, oid, base, state, auto_merge = (
        data.get("headRefName"),
        data.get("headRefOid"),
        data.get("baseRefName"),
        data.get("state"),
        data["autoMergeRequest"],
    )
    if not all(isinstance(v, str) for v in (name, oid, base, state)):
        return None
    if auto_merge is not None and not isinstance(auto_merge, dict):
        return None
    return PrHead(
        ref_name=str(name),
        oid=str(oid),
        base_ref_name=str(base),
        state=str(state),
        auto_merge_armed=auto_merge is not None,
    )


class OpenPrLookup(NamedTuple):
    """Result of :func:`pr_exists_for_branch` (#2740).

    ``open_pr``:
      True   — an open PR exists for this branch
      False  — no open PR
      None   — transient error (timeout, non-zero exit, JSON parse failure)

    ``gh_available``:
      False  — gh binary not found (FileNotFoundError)
      True   — binary present (even if the call failed transiently)

    ``url``: the first open PR's URL as gh lists it, or None when there is no
    open PR, the lookup failed, or gh's item carried no usable ``url``. A None
    URL never weakens ``open_pr``: the bool alone is the gate contract.
    """

    open_pr: bool | None
    gh_available: bool
    url: str | None = None


def _first_open_pr_url(data: object) -> str | None:
    """The first ``gh pr list`` item's non-empty string ``url``, else None."""
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    url = first.get("url")
    return url if isinstance(url, str) and url else None


def pr_exists_for_branch(
    branch: str, *, timeout: int = _PR_EXISTS_TIMEOUT, cwd: Path | None = None
) -> OpenPrLookup:
    """Return whether *branch* has an open PR, gh's availability, and its URL.

    See :class:`OpenPrLookup` for the three fields. Every failure is
    fail-open (``open_pr`` None) and carries no URL; with several open PRs
    on the branch the URL is the first one gh lists.

    *cwd* scopes the ``gh`` call to a client's repo (defaults to None, i.e.
    ambient CWD); multi-client callers MUST pass it (GitHub #1269/#1279).
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
                "number,url",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError:
        return OpenPrLookup(None, False)
    except (OSError, _sp.TimeoutExpired):
        return OpenPrLookup(None, True)

    if result.returncode != 0:
        return OpenPrLookup(None, True)

    try:
        data: list[dict[str, object]] = json.loads(result.stdout)
        return OpenPrLookup(len(data) > 0, True, _first_open_pr_url(data))
    except (ValueError, AttributeError):
        return OpenPrLookup(None, True)
