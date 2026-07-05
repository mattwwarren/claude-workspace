"""Non-display session-grouping and derivation over orchestrate summaries.

Pure helpers that reason over :class:`~cw.orchestrate.SessionSummary` and
:class:`~cw.orchestrate.TicketSummary` — grouping by client, bucketing
sessions per client, and deriving worktree contention. These are consumed by
``cw board --detail`` (``cw.board._build_detail_panel``); this module derives,
the board renders. No Rich imports, no formatting, no I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cw.orchestrate import SessionSummary, TicketSummary

_CONTENTION_THRESHOLD = 2


def group_by_client(
    sessions: Iterable[SessionSummary],
    tickets: Iterable[TicketSummary],
) -> list[str]:
    """Return client names sorted by most activity first, then alphabetically."""
    counts: dict[str, int] = {}
    for sess in sessions:
        counts[sess.client] = counts.get(sess.client, 0) + 1
    for ticket in tickets:
        counts[ticket.client] = counts.get(ticket.client, 0) + 1
    return sorted(counts, key=lambda c: (-counts[c], c))


def sessions_by_client(
    sessions: Iterable[SessionSummary],
) -> dict[str, list[SessionSummary]]:
    """Bucket sessions by client. Clients with no sessions are absent."""
    grouped: dict[str, list[SessionSummary]] = {}
    for sess in sessions:
        grouped.setdefault(sess.client, []).append(sess)
    return grouped


def worktree_contention(sessions: Iterable[SessionSummary]) -> dict[str, int]:
    """Map ``str(worktree_path)`` -> count of sessions on that path.

    Sessions with no worktree path are excluded. A path with a count of
    ``_CONTENTION_THRESHOLD`` (2) or more is contended — two or more parallel
    sessions share it.
    """
    counts: dict[str, int] = {}
    for sess in sessions:
        if sess.worktree_path is None:
            continue
        key = str(sess.worktree_path)
        counts[key] = counts.get(key, 0) + 1
    return counts
