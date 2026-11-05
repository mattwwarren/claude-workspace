"""Origin branch reads via ``gh api``."""

from __future__ import annotations

import subprocess as _sp
from typing import TYPE_CHECKING
from urllib.parse import quote as _urlquote

from cw._git import FULL_COMMIT_ID_RE

if TYPE_CHECKING:
    from pathlib import Path


def _fetch_branch_exists_on_origin(
    branch: str, timeout: int, *, cwd: Path | None = None
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
            cwd=cwd,
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
    branch: str, *, timeout: int = 10, cwd: Path | None = None
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

    *cwd* scopes the ``gh`` call to a client's repo (defaults to None, i.e.
    ambient CWD); multi-client callers MUST pass it (GitHub #1269/#1279).
    """
    return _fetch_branch_exists_on_origin(branch, timeout, cwd=cwd)


def _fetch_branch_head_sha_on_origin(
    branch: str, timeout: int, *, cwd: Path | None = None
) -> tuple[str | None, bool]:
    """(head_sha, gh_available) via the refs endpoint + ``--jq .object.sha``.

    Anything but one commit SHA (a 404, ``null``, GitHub's prefix-match
    array) is (None, True)."""
    ref_path = _urlquote(branch, safe="/")
    try:
        result = _sp.run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/git/refs/heads/{ref_path}",
                "--jq",
                ".object.sha",
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

    sha = result.stdout.strip()
    if result.returncode == 0 and FULL_COMMIT_ID_RE.fullmatch(sha):
        return sha, True
    return None, True


def branch_head_sha_on_origin(
    branch: str, *, timeout: int = 10, cwd: Path | None = None
) -> tuple[str | None, bool]:
    """*branch*'s head commit SHA on origin as (sha, gh_available) (#2337).

    (None, True) = absent/transient/unparseable; (None, False) = no gh. The
    scope-drift approval refuses on (None, *): no head to bind to. *cwd*
    scopes the call to a client's repo, as for branch_exists_on_origin.
    """
    return _fetch_branch_head_sha_on_origin(branch, timeout, cwd=cwd)
