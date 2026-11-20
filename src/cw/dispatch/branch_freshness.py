"""Would a REVIEW branch conflict with ``origin/<default>``? (#1823, #2979)

The pre-existing freshness gate (``dispatch/gating/freshness.py``) checks only
the shared *client checkout* at dispatch-tick preflight time -- never the
ticket's own branch, and never at the REVIEW->approval boundary. This module
answers the narrower question that boundary needs: **would merging
``origin/<default_branch>`` into this ticket's branch conflict?**

A branch that is merely behind the default branch is not a reason to act
(#2979, ADR-0022): GitHub merges a clean branch, and a clean merge never causes
a review. Only a merge that would conflict needs a person (or Step 4c.5) to
merge once and resolve, so only that parks the ticket (``branch_behind_main``,
whose name is kept for the persisted rows and events). #1823's original
predicate parked on "behind with overlapping files", which on a fast-moving
default branch fired constantly for textually clean merges.

Three hard contracts:

* **No git under ``dev_queue_lock``.** The gate's routing runs inside the
  dispatch loop's ``dev_queue_lock()`` critical section, so the probe cannot
  run there. :func:`resolve_conflicting_branches` runs once per
  ``consume_completed_sessions`` call, BEFORE the lock (the
  ``dispatch/pr_gate.resolve_stale_pr_ticket_ids`` pattern; ADR-0022
  invariant 3 makes this a rule for this gate), and
  :func:`precomputed_branch_conflicts` hands its verdicts to the in-lock
  predicate :func:`has_conflicting_branch`, which does no I/O.
* **Bounded per tick.** At most :data:`_MAX_PROBES_PER_TICK` probes, and a
  :data:`_SWEEP_BUDGET_SECONDS` wall-clock budget checked before each probe, so
  the worst case is the budget plus one probe (up to three git calls, each
  bounded by :data:`cw.merge_probe.PROBE_TIMEOUT_SECONDS`), not the budget
  alone. Resolving a dispatch-driven row's worktree costs one local
  ``git branch --show-current`` per candidate, before the cap: bounded by the
  RUNNING REVIEW rows a pending event names, not by the cap. A worktree that
  does not resolve or does not verify is a no-probe (fail open).
* **Fail open, never raise.** Anything but a probe-reported ``conflict``
  (``up_to_date``, ``clean``, ``unavailable``, not probed, the cap reached, no
  verdict installed) does not park. A false positive parks a healthy ticket and
  costs an operator; a false negative is caught downstream by finalize Step
  4c.5, which handles a conflicting PR.

It reads the *already-fetched* local ``origin/<default_branch>`` ref and never
fetches; detection latency is bounded by the per-tick fetch cadence, by design.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final

from cw.merge_probe import probe_default_merge
from cw.models import QueueItemStatus, Stage
from cw.worktree import resolve_task_worktree

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from cw.models import ClientConfig, DevQueueStore, OrchestratorEvent, TicketTask

_log = logging.getLogger("cw.dispatch")

#: Cap on probes per sweep, the figure ``dispatch/pr_gate`` uses for ``gh``.
_MAX_PROBES_PER_TICK: Final = 20
#: Wall-clock budget, in seconds, checked only before each probe: a probe that
#: starts inside it may run past it (up to three git calls of 10 s each), so the
#: sweep's worst case is the budget plus one probe, not the budget.
_SWEEP_BUDGET_SECONDS: Final = 30.0

#: ``(client_name, ticket_id)`` of a branch whose probe reported a conflict.
BranchKey = tuple[str, str]

_CONFLICTS: ContextVar[frozenset[BranchKey] | None] = ContextVar(
    "_CONFLICTS", default=None
)


def _gate_worktree(task: TicketTask, client: ClientConfig | None) -> Path | None:
    """The worktree the gate probes, via ``resolve_task_worktree`` (#2990).

    The stamped ``task.worktree_path`` wins (user-origin and test rows). For a
    dispatch-driven row, which carries no stamped path, the branch-derived
    worktree is used only once ``git branch --show-current`` there names the
    row's branch; a stale or foreign checkout is skipped (fail open). The gate
    probes every REVIEW row whose worktree resolves, not only stamped ones: RFC
    0020 runs every epic child as a dispatch row, and D-8 relies on ADR-0022
    applying to each ticket against its own base, so an inert gate for dispatch
    rows would leave that unenforced. The ``.exists()`` check stays because the
    stamped branch of ``resolve_task_worktree`` returns the stamped path without
    checking it.
    """
    path = resolve_task_worktree(task, client)
    if path is None or not path.exists():
        return None
    return path


def _completed_ticket_ids(events: Sequence[OrchestratorEvent]) -> set[str]:
    ids: set[str] = set()
    for event in events:
        ticket_id = event.payload.get("ticket_id")
        if isinstance(ticket_id, str) and ticket_id:
            ids.add(ticket_id)
    return ids


def _candidates(
    events: Sequence[OrchestratorEvent],
    store: DevQueueStore,
    clients: Mapping[str, ClientConfig],
) -> list[tuple[BranchKey, Path, str]]:
    """``(key, worktree, default_branch)`` per RUNNING REVIEW row an event names."""
    pending = _completed_ticket_ids(events)
    found: dict[BranchKey, tuple[Path, str]] = {}
    for task in store.tasks:
        if task.ticket_id not in pending:
            continue
        if task.status != QueueItemStatus.RUNNING or task.stage != Stage.REVIEW:
            continue
        client = clients.get(task.client)
        worktree = _gate_worktree(task, client)
        if client is None or worktree is None:
            continue
        found.setdefault(
            (task.client, task.ticket_id), (worktree, client.default_branch)
        )
    return [(key, path, base) for key, (path, base) in found.items()]


def resolve_conflicting_branches(
    events: Sequence[OrchestratorEvent],
    store: DevQueueStore,
    clients: Mapping[str, ClientConfig],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> frozenset[BranchKey]:
    """The ``(client, ticket)`` pairs whose branch would conflict with main.

    Runs OUTSIDE ``dev_queue_lock``: *store* is a lock-free snapshot (a stale
    read is acceptable for this read-only caller, ADR-0005). Candidates are the
    RUNNING, REVIEW-stage rows whose ticket id a pending SESSION_COMPLETED
    event names, whose client resolves and whose worktree resolves (stamped, or
    derived and verified, ``resolve_task_worktree``) and exists.
    Probes past :data:`_MAX_PROBES_PER_TICK` or :data:`_SWEEP_BUDGET_SECONDS`
    are skipped (fail open: they do not park this tick) and one INFO line
    counts them. The budget is checked before each probe, not during one, so
    the worst case is the budget plus one probe (up to three git calls of 10 s
    each). Each ``unavailable`` probe also logs one INFO line (client, ticket,
    detail) and does not park. Never raises.
    """
    candidates = _candidates(events, store, clients)
    started = clock()
    conflicts: set[BranchKey] = set()
    probed = 0
    for key, worktree, base in candidates:
        if probed >= _MAX_PROBES_PER_TICK or clock() - started >= _SWEEP_BUDGET_SECONDS:
            break
        probed += 1
        probe = probe_default_merge(worktree, base)
        if probe.state == "conflict":
            conflicts.add(key)
        elif probe.state == "unavailable":
            _log.info(
                "branch_freshness: probe unavailable for %s/%s (%s); it does not park",
                key[0],
                key[1],
                probe.detail,
            )
    skipped = len(candidates) - probed
    if skipped:
        _log.info(
            "branch_freshness: %d REVIEW branch probe(s) skipped this tick "
            "(cap %d, budget %.0fs); they do not park",
            skipped,
            _MAX_PROBES_PER_TICK,
            _SWEEP_BUDGET_SECONDS,
        )
    return frozenset(conflicts)


@contextmanager
def precomputed_branch_conflicts(
    conflicts: frozenset[BranchKey],
) -> Iterator[None]:
    """Install *conflicts* for :func:`has_conflicting_branch` while the block runs.

    Scoped to the one locked section that routes the completions the sweep
    probed, and reset on exit (normal or not), so no verdict outlives its
    tick. Nesting restores the outer verdicts.
    """
    token = _CONFLICTS.set(conflicts)
    try:
        yield
    finally:
        _CONFLICTS.reset(token)


def has_conflicting_branch(client_name: str, ticket_id: str) -> bool:
    """True iff the installed sweep found this ticket's branch would conflict.

    No I/O. ``False`` (fail open) outside :func:`precomputed_branch_conflicts`
    and for any key the sweep did not report as a conflict.
    """
    conflicts = _CONFLICTS.get()
    return conflicts is not None and (client_name, ticket_id) in conflicts
