"""Operator-tolerated worktree paths for the dirty-start and mutation checks (#3693).

``ReviewConfig.tolerated_worktree_paths`` lists exact repo-relative paths that a
per-client operator has chosen to tolerate. Two checks consume it through the
one helper here: the post-review mutation snapshot (``worktree_state``) and the
stage dirty-start check (``capture_cycle_baseline``). A listed path is matched
bare and exact against the ``git status --porcelain`` path field, whatever the
status. Nothing reads file content. A WARNING names the path and the check, so
the tolerance stays visible.

This is a leaf module: it imports nothing from ``cw`` at module scope, so the
review and stage packages can both depend on it without a cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from cw.models.client import ClientConfig

_log = logging.getLogger(__name__)

CHECK_DIRTY_START = "dirty_start"
CHECK_POST_REVIEW_MUTATION = "post_review_mutation"

# Porcelain v1 lines are ``XY<space>path``: the path field starts at offset 3.
_PORCELAIN_PATH_OFFSET = 3


class ToleratedSplit(NamedTuple):
    """Result of removing tolerated lines from a porcelain status.

    ``kept`` is the status text with tolerated lines removed; it is the same
    object as the input when nothing was removed. ``paths`` are the removed
    exact paths, in porcelain order, de-duplicated.
    """

    kept: str
    paths: tuple[str, ...]


def _porcelain_path(line: str) -> str:
    return line[_PORCELAIN_PATH_OFFSET:]


def split_tolerated(
    status: str,
    tolerated: frozenset[str],
    *,
    check: str | None = None,
) -> ToleratedSplit:
    """Remove lines whose exact path field is tolerated; report what was removed.

    A rename or copy line (``old -> new``) never matches: its path field can
    never equal a validated entry, so a rename touching a tolerated path still
    counts. When ``check`` is set, one WARNING per removed path is logged with
    the path and the check name only; no file content is read or logged.
    """
    if not tolerated:
        return ToleratedSplit(status, ())
    kept_lines: list[str] = []
    removed: list[str] = []
    for line in status.splitlines(keepends=True):
        path = _porcelain_path(line.rstrip("\r\n"))
        if path in tolerated:
            if path not in removed:
                removed.append(path)
            continue
        kept_lines.append(line)
    if not removed:
        return ToleratedSplit(status, ())
    if check is not None:
        for path in removed:
            _log.warning(
                "tolerated worktree path is dirty: path=%s check=%s", path, check
            )
    return ToleratedSplit("".join(kept_lines), tuple(removed))


def tolerated_paths(client: ClientConfig) -> frozenset[str]:
    """The client's live-config tolerated paths (never the worker-writable context)."""
    return frozenset(client.review.tolerated_worktree_paths)
