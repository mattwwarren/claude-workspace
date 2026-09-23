"""Worktree path and base-directory resolution for :mod:`cw.worktree`.

Pure path computation: ``slugify_branch`` -> ``worktree_path_for`` ->
``resolve_task_worktree``. The only git call is the checked-out-branch probe
``resolve_task_worktree`` uses to trust an on-disk worktree.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

from cw.worktree._git import _checked_out_branch, _git_dir

if TYPE_CHECKING:
    from cw.models import ClientConfig, TicketTask


# The native spawn backend (``spawn_create_impl`` / ``claude --bg`` with
# ``cwd=``) has no path-length restriction beyond OS limits (PATH_MAX ~4096).
# The 64-char threshold is a conservative trigger: for any realistic workspace
# path the default candidate exceeds this cap, so the hash-fallback base
# (``~/.cw/wt/``) is used in practice — keeping paths short and predictable.
_WORKTREE_NAME_CAP = 64
_HASH_BASE_SEGMENTS = (".cw", "wt")
# 8 hex chars = 32 bits. For a single-user tool with a handful of
# clients the collision probability is negligible; raising this value
# pushes the hashed base closer to _WORKTREE_NAME_CAP and reduces
# headroom for the branch slug, so increase with care.
_WORKSPACE_HASH_CHARS = 8


def slugify_branch(branch: str) -> str:
    """Convert a branch name to a worktree-safe slug.

    Collapses any run of disallowed characters into a single hyphen, then
    strips leading/trailing hyphens. The allowed charset (``[A-Za-z0-9._-]``)
    matches ``claude -w``'s worktree-name validator — anything outside this
    set (path separators, ``#``, spaces, unicode) becomes ``-``.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")


def resolve_worktree_base(client: ClientConfig) -> Path:
    """Return the worktree base directory for a client.

    Uses ``client.worktree_base`` if set, otherwise defaults to
    ``<git_dir.parent>/.worktrees/<git_dir.name>``.
    """
    if client.worktree_base is not None:
        return client.worktree_base
    ws = _git_dir(client)
    return ws.parent / ".worktrees" / ws.name


def effective_worktree_bases(client: ClientConfig) -> frozenset[Path]:
    """Return all directories that may contain cw-managed worktrees for *client*.

    ``worktree_path_for`` silently redirects to a hash-derived base under
    ``~/.cw/wt/`` when the default sibling path would exceed
    ``_WORKTREE_NAME_CAP``. GC must search *both* to avoid silently skipping
    worktrees created when that fallback was in effect.

    When ``client.worktree_base`` is set explicitly only that directory is
    returned — the user chose a location and there is no hash fallback.
    """
    if client.worktree_base is not None:
        return frozenset({client.worktree_base})
    return frozenset({resolve_worktree_base(client), _hashed_worktree_base(client)})


def _hashed_worktree_base(client: ClientConfig) -> Path:
    """Return a short hash-derived worktree base for a client.

    Used as a fallback when the default sibling layout would exceed
    ``_WORKTREE_NAME_CAP``. The hash seeds from the *resolved* git
    directory so symlinks and non-canonical paths collapse to the
    same digest — ``create_worktree`` and ``remove_worktree`` must
    agree on the location across invocations.
    """
    git_dir = _git_dir(client).resolve()
    digest = hashlib.sha256(str(git_dir).encode("utf-8")).hexdigest()
    return Path.home().joinpath(*_HASH_BASE_SEGMENTS, digest[:_WORKSPACE_HASH_CHARS])


def worktree_path_for(client: ClientConfig, branch: str) -> Path:
    """Return the full worktree path for a branch.

    Falls back to a hash-derived short base under ``~/.cw/wt/`` when the
    default layout would produce a path longer than the 64-char path-length
    threshold. An explicit ``client.worktree_base`` is always honoured, even
    if it produces a path over the threshold — user choice wins over the
    safety net.
    """
    slug = slugify_branch(branch)
    base = resolve_worktree_base(client)
    candidate = base / slug
    if client.worktree_base is not None or len(str(candidate)) <= _WORKTREE_NAME_CAP:
        return candidate
    return _hashed_worktree_base(client) / slug


def resolve_task_worktree(
    task: TicketTask, client_cfg: ClientConfig | None
) -> Path | None:
    """Resolve the on-disk worktree for *task*, or None (#2123).

    ``task.worktree_path`` wins when stamped (USER-origin rows, tests). It is
    ``None`` for every dispatch-driven row -- dispatch stamps ``worktree_path``
    on the Session, never the TicketTask (see ``queue_peek.py``) -- so any
    consumer that reads only that field sees ``None`` on the dominant
    production path. Fall back to the branch-derived worktree
    :func:`worktree_path_for` computes for the feature branch, using the same
    read-only primitives ``create_worktree`` consults to decide reuse, and
    trust it only when the checked-out branch matches: a stale or foreign
    checkout must not lend its state to this ticket.

    Lives here rather than in either consumer because both
    ``dev_queue.lifecycle._local_plan_path`` and
    ``dispatch.review_gates._should_gate_for_review_staleness`` need it, and
    neither package may import the other. Returning the worktree *directory*
    (not a file under it) is what lets the two consumers ask different
    questions of the same resolution.
    """
    if task.worktree_path is not None:
        return task.worktree_path
    if client_cfg is None:
        return None
    branch = f"{client_cfg.feature_branch_prefix}/{task.ticket_id}"
    wt_path = worktree_path_for(client_cfg, branch)
    if not wt_path.exists() or _checked_out_branch(wt_path) != branch:
        return None
    return wt_path
