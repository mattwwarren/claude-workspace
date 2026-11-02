"""Lockless capture of concierge recipe 3's git read (#2546).

Recipe 3 (cancelled-row restore) asks git whether a CANCELLED row's worktree
has commits ahead of its base branch. That ``git log`` must not run under
``sessions_lock``, so ``reconcile()`` captures it here before the lock and the
in-lock detect (``concierge._detect_cancelled_row_candidates``) only looks the
answer up. Capture and detect share :func:`cancelled_row_targets`, so they
cannot disagree about which rows are eligible.

Restore is a side-effecting write, so uncertainty never triggers it: a git
error is already a stored False (``_has_commits_beyond_base`` fails to False),
and a missing, stale or budget-exhausted capture reads as False too. The row
is retried on the next tick.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from cw.config import get_client
from cw.exceptions import CwError
from cw.models import QueueItemStatus
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError
from cw.worktree import _has_commits_beyond_base

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import TicketTask

_log = logging.getLogger(__name__)

# Wall-clock budget for one capture pass, checked before each git read.
CANCELLED_ROW_PROBE_BUDGET_SECONDS: float = 20.0
# A capture is usable for this long. It is taken after the shipped pass and
# before the harvest pass (at most 100 s), so a tighter bound could age it out
# during a slow harvest pass; a long lock wait only defers the row.
CANCELLED_ROW_PROBE_MAX_AGE_SECONDS: float = 180.0


class CancelledRowKey(NamedTuple):
    """Everything the git read depends on; a change to any field is a miss."""

    client: str
    ticket_id: str
    worktree_path: Path
    default_branch: str


type CancelledRowProbes = BoundedProbeStore[CancelledRowKey, bool]


def new_cancelled_row_probes(
    *, budget_seconds: float | None = CANCELLED_ROW_PROBE_BUDGET_SECONDS
) -> CancelledRowProbes:
    """An empty store; ``budget_seconds=None`` means unbounded.

    No per-tick cap: a stable set of zero-commit CANCELLED rows would
    otherwise starve every later row forever.
    """
    return BoundedProbeStore(
        budget_seconds=budget_seconds,
        max_captures=None,
        max_age_seconds=CANCELLED_ROW_PROBE_MAX_AGE_SECONDS,
    )


def cancelled_row_targets(tasks: list[TicketTask]) -> list[CancelledRowKey]:
    """The rows recipe 3 would ask git about: CANCELLED, with an existing
    worktree, on a configured client."""
    keys: list[CancelledRowKey] = []
    for task in tasks:
        if task.status != QueueItemStatus.CANCELLED:
            continue
        if task.worktree_path is None or not task.worktree_path.exists():
            continue
        try:
            client_cfg = get_client(task.client)
        except CwError:
            continue
        keys.append(
            CancelledRowKey(
                client=task.client,
                ticket_id=task.ticket_id,
                worktree_path=task.worktree_path,
                default_branch=client_cfg.default_branch,
            )
        )
    return keys


def _read_commits_ahead(key: CancelledRowKey, _captured_at: datetime) -> bool:
    return _has_commits_beyond_base(key.worktree_path, key.default_branch)


def capture_cancelled_row_probes(
    tasks: list[TicketTask], *, probes: CancelledRowProbes
) -> None:
    """Capture each target's commits-ahead answer into *probes*, lock-free.

    Stops at the first budget refusal with one warning; the uncaptured rows
    defer. Any other exception from the read propagates and stores nothing.
    """
    keys = cancelled_row_targets(tasks)
    for index, key in enumerate(keys):
        try:
            probes.capture(key, read=partial(_read_commits_ahead, key))
        except ProbeStoreUnavailableError as err:
            _log.warning(
                "reconcile: cancelled-row probe capture stopped (%s); %d captured,"
                " %d of %d CANCELLED row(s) deferred to the next tick",
                err.reason,
                index,
                len(keys) - index,
                len(keys),
            )
            return


def cancelled_row_has_commits(
    probes: CancelledRowProbes | None, key: CancelledRowKey
) -> bool:
    """The in-lock read: *key*'s captured answer, never git.

    ``None`` behaves like an empty store. Any miss logs one warning and reads
    as False, so the row is not restored this tick.
    """
    store = probes if probes is not None else new_cancelled_row_probes()
    try:
        return store.lookup(key)
    except ProbeStoreUnavailableError as err:
        _log.warning(
            "cancelled_row_probe_unavailable ticket=%s client=%s reason=%s;"
            " not restored this tick",
            key.ticket_id,
            key.client,
            err.reason,
        )
        return False
