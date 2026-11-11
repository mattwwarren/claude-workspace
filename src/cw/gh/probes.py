"""``gh`` identity, availability and repo-visibility probes."""

from __future__ import annotations

import json
import re
import subprocess as _sp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


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
        # Why: deliberately unscoped (no cwd) — the gh identity is a
        # per-user account fact, not a per-repo one, so it is repo-independent
        # and must stay global (GitHub #1269 R3).
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


def check_gh_availability(*, timeout: int) -> bool:
    """Return True iff ``gh auth status`` succeeds (RFC 0011 A5).

    The fleet-wide availability preflight probe for ``dispatch_tick``. Fails
    closed (returns False) on any failure to resolve — gh binary absent, a
    non-zero exit, a timeout, or an OSError — so a probe that cannot confirm
    availability never reports the fleet as available.
    """
    try:
        # Why: deliberately unscoped (no cwd) — ``gh auth status`` probes the
        # user's auth session, a repo-independent fact, so it must stay global
        # (GitHub #1269 R3).
        result = _sp.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False
    except (OSError, _sp.TimeoutExpired):
        return False
    return result.returncode == 0


def fetch_repo_visibility(*, timeout: int, cwd: Path | None) -> str | None:
    """Return the repo's visibility (``PUBLIC``/``PRIVATE``/``INTERNAL``) or None.

    Shaped after :func:`fetch_issue_body`: runs ``gh repo view --json
    visibility`` in *cwd* and returns None on every failure (gh absent, a
    non-zero exit, a timeout, unparsable JSON, a missing or empty value).
    ``cw post-comment`` refuses to post on None (#2810).
    """
    try:
        result = _sp.run(
            ["gh", "repo", "view", "--json", "visibility"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    value = data.get("visibility") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        return None
    return value.upper()


_REPO_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def fetch_repo_name_with_owner(*, timeout: int, cwd: Path | None) -> str | None:
    """Return the repo's ``owner/name`` slug, or None.

    Shaped exactly like :func:`fetch_repo_visibility`: runs ``gh repo view
    --json nameWithOwner`` in *cwd* and returns None on every failure (gh
    absent, a non-zero exit, a timeout, unparsable JSON, a missing, non-string
    or empty value, or a value that is not an ``owner/name`` slug). The IMPL
    executor binds repair feedback to the live repository with it.
    """
    try:
        result = _sp.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    value = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not isinstance(value, str) or not _REPO_SLUG.fullmatch(value):
        return None
    return value
