"""The REVIEW staleness tip check, run before ``dev_queue_lock`` (#2994).

``_should_gate_for_review_staleness`` runs ``tolerates_review_move`` (git) for a
REVIEW row whose ``reviewed_sha`` is not HEAD. Inside ``dev_queue_lock`` that
git stalls every other queue writer, so ADR-0022 invariant 3 moves it out: the
consume path runs :func:`tip_sweep_scope` before the lock, which checks the
rows its pending events name and installs the answers for the locked gate to
read (:mod:`cw.dispatch.review_tip_verdicts`).

Three hard contracts:

* **Bounded per tick, for the tip checks.** At most :data:`_MAX_TIP_CHECKS_PER_TICK`
  tip checks (``tolerates_review_move``), and a :data:`_TIP_SWEEP_BUDGET_SECONDS`
  wall-clock budget checked before each check, so the tip checks' worst case is
  the budget plus one check (one check is four prelude git calls, the
  ``TIP_CHECK_BUDGET_SECONDS`` merge budget, and up to three merges, each bounded
  by ``PROBE_TIMEOUT_SECONDS``). At least one check runs per tick, so the queue
  always shrinks. The bound covers the checks only, not every read: planning a
  row (``plan_review_staleness``: one local ``git rev-parse HEAD``, one
  ``git branch --show-current`` for an unstamped row, a ledger read) runs for each
  pending event up to the cut before the cap is consulted, and the locked gate
  plans each row again. Those reads are local and bounded by the pending events,
  as ``branch_freshness``'s checkout check is; they are not metered.
* **Defer, never decide from a skip.** The sweep walks events in order and
  returns a strict prefix. At the first row it cannot afford, it stops; the
  rest stay pending and their rows stay RUNNING for a later tick. A deferred
  row is neither stale nor fresh: nothing is decided for it this tick. While
  its event is pending, the completed-silent backstop leaves its row alone
  (``reconcile.tasks.backstops``), so the recorded result is not dropped.
* **Fall back to inline, never raise past the lock.** An unreadable queue,
  state or client config yields no sweep: every event applies inline, as
  before, and the locked apply surfaces the error on its own read. A verdict
  key miss under the scope also runs the tip check inline, and logs a WARNING.
  Both fallbacks are uncapped by design (a skip inside the router would decide
  from it, because mutations precede the gate); they are the accepted exception
  to ARCHITECTURE section 7 item 16, observable through the WARNING and pinned
  at zero misses on the normal path.

Candidates over-approximate the router's (any non-FINALIZE row whose status is
a scope-gated approval or stage-success status). Over-approximating only costs
budget; ``tests`` pin that the inline check never fires outside this set.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, NamedTuple

from cw.auto_dev_result.schema._vocab import (
    SCOPE_GATED_APPROVAL_STATUSES,
    STAGE_SUCCESS_STATUSES,
)
from cw.config import load_effective_clients, load_state
from cw.dev_queue import load_dev_queue
from cw.dispatch.review_gates import plan_review_staleness
from cw.dispatch.review_tip_verdicts import (
    TipCheck,
    TipKey,
    precomputed_tip_verdicts,
)
from cw.dispatch.session_completions import (
    _running_row_for_event,
    _session_last_result,
)
from cw.exceptions import CwError
from cw.models import Stage
from cw.review_convergence import tolerates_review_move

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from cw.models import (
        ClientConfig,
        CwState,
        DevQueueStore,
        OrchestratorEvent,
    )

_log = logging.getLogger("cw.dispatch")

#: Cap on tip checks per tick (rows). Between the probe caps of the other
#: pre-lock sweeps; a tip check is heavier than one probe.
_MAX_TIP_CHECKS_PER_TICK: Final = 10
#: Wall-clock budget, checked only before each check, not during one.
_TIP_SWEEP_BUDGET_SECONDS: Final = 30.0

#: Statuses the router can send to the REVIEW staleness gate (superset).
_CANDIDATE_STATUSES: Final = SCOPE_GATED_APPROVAL_STATUSES | STAGE_SUCCESS_STATUSES


class TipSweep(NamedTuple):
    """One tick's pre-lock answers: verdicts, the prefix to apply, the held count."""

    verdicts: dict[TipKey, bool]
    events: list[OrchestratorEvent]
    deferred: int


def _plan_for_event(
    event: OrchestratorEvent,
    store: DevQueueStore,
    state: CwState,
    clients: dict[str, ClientConfig],
) -> bool | TipCheck | None:
    """The gate plan for the row *event* names, or ``None`` if it has none.

    ``None`` (no RUNNING row, FINALIZE, or a status the gate never sees) costs
    no budget. A ``bool`` is a final answer that needs no git.
    """
    row = _running_row_for_event(store, event)
    if row is None:
        return None
    last_result = _session_last_result(state, row.session_id)
    status = last_result.get("status") if last_result is not None else None
    if row.task.stage is Stage.FINALIZE or status not in _CANDIDATE_STATUSES:
        return None
    return plan_review_staleness(row.task, last_result, clients)


def _held(
    events: Sequence[OrchestratorEvent],
    index: int,
    verdicts: dict[TipKey, bool],
    checked: int,
) -> TipSweep:
    """Cut the batch at *index*, logging the deferral once (INFO)."""
    deferred = len(events) - index
    _log.info(
        "review_tip: %d completion event(s) held for a later tick (%d tip "
        "check(s) run, cap %d, budget %.0fs); their rows stay RUNNING and are "
        "examined next tick",
        deferred,
        checked,
        _MAX_TIP_CHECKS_PER_TICK,
        _TIP_SWEEP_BUDGET_SECONDS,
    )
    return TipSweep(verdicts, list(events[:index]), deferred)


def resolve_review_tip_sweep(
    events: Sequence[OrchestratorEvent],
    store: DevQueueStore,
    state: CwState,
    clients: dict[str, ClientConfig],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> TipSweep:
    """Check the tips the pending *events* need, up to the per-tick bound.

    Runs OUTSIDE ``dev_queue_lock`` on a lock-free snapshot (ADR-0005: a stale
    read is acceptable for this read-only caller). A repeated key costs one
    check. Never raises for a bound; the planner and checker are the same
    code the in-lock gate would run.
    """
    started = clock()
    verdicts: dict[TipKey, bool] = {}
    checked = 0
    for index, event in enumerate(events):
        plan = _plan_for_event(event, store, state, clients)
        if not isinstance(plan, TipCheck) or plan.key in verdicts:
            continue
        over_bound = (
            checked >= _MAX_TIP_CHECKS_PER_TICK
            or clock() - started >= _TIP_SWEEP_BUDGET_SECONDS
        )
        if checked >= 1 and over_bound:
            return _held(events, index, verdicts, checked)
        checked += 1
        verdicts[plan.key] = tolerates_review_move(
            plan.client,
            plan.worktree,
            reviewed_sha=plan.key.reviewed_sha,
            head_sha=plan.key.head_sha,
        )
    return TipSweep(verdicts, list(events), 0)


def review_tip_sweep_lock_free(
    events: Sequence[OrchestratorEvent],
) -> TipSweep | None:
    """The sweep for *events* on a lock-free read, or ``None`` if unreadable.

    An unreadable queue, state or client config (``OSError``, ``ValueError``,
    ``CwError``) yields ``None`` with one WARNING: the caller applies every
    event inline, and the locked apply re-reads and raises on its own if the
    error persists (deferring everything would stall the pipeline).
    """
    try:
        store = load_dev_queue()
        state = load_state()
        clients = load_effective_clients()
    except (OSError, ValueError, CwError):
        _log.warning(
            "review_tip: could not read the queue, state or clients for the "
            "REVIEW staleness sweep; applying every event inline",
            exc_info=True,
        )
        return None
    return resolve_review_tip_sweep(events, store, state, clients)


@contextmanager
def tip_sweep_scope(
    events: Sequence[OrchestratorEvent],
) -> Iterator[list[OrchestratorEvent]]:
    """Yield the events to apply and install their verdicts for the block.

    On an unreadable input no scope is installed and every event is yielded,
    so the gate runs its inline check as before. Otherwise the verdicts are
    active only inside the block, and the block applies the prefix.
    """
    sweep = review_tip_sweep_lock_free(events)
    if sweep is None:
        yield list(events)
        return
    with precomputed_tip_verdicts(sweep.verdicts):
        yield sweep.events
