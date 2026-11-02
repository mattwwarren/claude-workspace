"""Caller-owned sink for external calls deferred until ``sessions_lock`` releases.

``reconcile()`` builds one :class:`DeferredReconcileJobs` per tick, threads it
down through ``_reconcile_locked`` into every act phase that would otherwise
make a bounded-but-slow external call under the lock (#1232), and drains it
from a ``finally`` once the lock has released. An act phase still makes its
decision and persists it under the lock (session stamped terminal,
``save_state`` flushed, ``SESSION_COMPLETED`` emitted); only the external
side effect -- today a daemon surface stop -- is queued as a
:class:`PostLockJob` and run later.

The sink composes the pre-existing review-recipe dispatch sink
(:class:`~cw.reconcile.review_recipes.core.DeferredReviewDispatch`, #1229) as
its ``review`` member rather than replacing it, so there is one threaded
object and one drain point.

A lost stop is usually recoverable: a queued surface stop that never runs (the
drain was interrupted, or the job raised) leaves a roster worker whose session
is already terminal, which the next tick's leaked-worker sweep
(:mod:`cw.reconcile.leaked_workers`) finds and stops. The exception is a
``CARRIED`` stop (below): if a ``BaseException`` interrupts the drain, the
``CARRIED`` stops that had not yet run are returned to the front of the
carry-over, and every other unrun job is abandoned.

The drain is serial and each stop is bounded at ~10 s, so a tick that queued
many stops would hold the caller for N x 10 s. The drain therefore runs at
most :data:`MAX_STOP_JOBS_PER_TICK` stop jobs per tick (#2551). The surplus is
handled by the job's :class:`StopKind`: a ``RECOVERABLE`` stop is dropped (the
next leaked-worker sweep re-finds it) and the drop is logged with a count and
the dropped labels; a ``CARRIED`` stop -- one whose side effect the sweep does
not redo, such as the shipped-release stop that finalizes a durable close
audit -- is held in a process-lifetime carry-over and runs first on the next
drain. The carry-over is in-process only: if the process exits first the carried
stop is lost, the surface is still stopped by the next tick's sweep, and the
audit stays ``pending_stop`` in its durable outbox until the doctor retry
finalizes it. Non-stop jobs are never capped, counted toward the cap or dropped,
and keep their relative order.
"""

from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from cw.reconcile import _deps

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.reconcile.review_recipes.core import DeferredReviewDispatch

_logger = logging.getLogger(__name__)

_SURFACE_STOP_LABEL_PREFIX = "surface_stop:"

# Per-tick cap on stop jobs in the serial post-lock drain (#2551). Each stop is
# bounded at ~10 s, so the worst-case drain wall time is ~50 s rather than
# unbounded. Matches PLAN_PREFETCH_MAX_PER_TICK in gate_plan_probes.
MAX_STOP_JOBS_PER_TICK = 5


class StopKind(enum.Enum):
    """How the per-tick stop cap treats a :class:`PostLockJob` (#2551)."""

    NOT_A_STOP = "not_a_stop"
    """Not a daemon stop (e.g. a ``gh`` call). Never capped, counted or dropped."""

    RECOVERABLE = "recoverable"
    """A stop whose session is already terminal. Past the cap it is dropped and
    logged: the next tick's leaked-worker sweep re-finds the roster worker."""

    CARRIED = "carried"
    """A stop the sweep cannot fully redo. Past the cap it is carried to the
    next drain, where it runs first and counts against that tick's cap."""


@dataclass(frozen=True)
class PostLockJob:
    """One external call to run after ``sessions_lock`` releases.

    *label* names the job in the failure log and lets a producer ask whether an
    equivalent job is already queued (:func:`is_surface_stop_queued`). *run*'s
    return value is discarded; it is typed ``object`` so a job may wrap a
    helper that returns something (e.g. an acted ticket id). *stop* classifies
    the job for the per-tick stop cap; the default leaves a job uncapped.
    """

    label: str
    run: Callable[[], object]
    stop: StopKind = StopKind.NOT_A_STOP


class _StopCarryover:
    """Process-lifetime holding area for stops the cap carried to a later tick.

    Same shape as ``_AttemptLatch`` in ``review_recipes.changelog_repair``:
    ``reconcile()`` runs on poller and anyio threads, so the list swaps are
    guarded by a lock held only for the swap (no file lock, no I/O).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: list[PostLockJob] = []

    def put(self, jobs: list[PostLockJob], *, front: bool = False) -> None:
        """Hold *jobs*, preserving FIFO order.

        By default they go behind anything already held. With *front* they go
        ahead of it, keeping their own relative order: an interrupted drain
        returns the stops it admitted but never ran, which are older than the
        surplus already put back.
        """
        if not jobs:
            return
        with self._lock:
            self._jobs = [*jobs, *self._jobs] if front else [*self._jobs, *jobs]

    def take(self) -> list[PostLockJob]:
        """Return and clear every carried job, oldest first."""
        with self._lock:
            jobs, self._jobs = self._jobs, []
        return jobs

    def labels(self) -> frozenset[str]:
        """Return the labels of every carried job."""
        with self._lock:
            return frozenset(job.label for job in self._jobs)

    def clear(self) -> None:
        """Drop every carried job (test isolation)."""
        with self._lock:
            self._jobs = []


_CARRYOVER = _StopCarryover()


@dataclass
class DeferredReconcileJobs:
    """Everything one ``reconcile()`` tick defers until after its lock.

    *review* is the review-recipe dispatch sink (#1229), present only when the
    caller may dispatch review jobs (``reconcile(dispatch_review_jobs=True)``).
    *post_lock* is an ordered list -- never keyed by label -- so two jobs that
    happen to share a label both run.
    """

    review: DeferredReviewDispatch | None = None
    post_lock: list[PostLockJob] = field(default_factory=list)


def _surface_stop_label(surface_ref: str) -> str:
    return f"{_SURFACE_STOP_LABEL_PREFIX}{surface_ref}"


def is_surface_stop_queued(deferred: DeferredReconcileJobs, surface_ref: str) -> bool:
    """Return True if a stop of *surface_ref* is queued on *deferred* or carried.

    A stop the cap carried from an earlier tick counts as queued, so the next
    tick's leaked-worker sweep does not queue (and audit) it a second time.
    """
    label = _surface_stop_label(surface_ref)
    return any(job.label == label for job in deferred.post_lock) or (
        label in _CARRYOVER.labels()
    )


def _run_surface_stop(surface_ref: str) -> None:
    """Stop *surface_ref*, resolving the daemon client at drain time."""
    _deps.get_native_daemon_client().stop(surface_ref)


def defer_surface_stop(
    deferred: DeferredReconcileJobs,
    surface_ref: str,
    *,
    kind: StopKind = StopKind.RECOVERABLE,
) -> None:
    """Queue a stop of the daemon surface *surface_ref* on *deferred*.

    Idempotent per surface: a second request for a surface already queued
    this tick (or carried from an earlier one) adds nothing, so the surface is
    stopped once. *kind* is how the per-tick cap treats a surplus stop.
    """
    if is_surface_stop_queued(deferred, surface_ref):
        return
    deferred.post_lock.append(
        PostLockJob(
            label=_surface_stop_label(surface_ref),
            run=partial(_run_surface_stop, surface_ref),
            stop=kind,
        )
    )


def _plan_drain(
    jobs: list[PostLockJob], max_stops: int
) -> tuple[list[PostLockJob], list[PostLockJob], list[PostLockJob]]:
    """Split *jobs* into ``(run, carry, dropped)`` under a stop cap.

    Walks in order. A non-stop job always runs. A stop runs while fewer than
    *max_stops* stops have been admitted; after that it is carried when its
    kind is ``CARRIED`` and dropped otherwise. ``run`` keeps the original
    relative order, so a capped stop never reorders or delays a non-stop job.
    """
    run: list[PostLockJob] = []
    carry: list[PostLockJob] = []
    dropped: list[PostLockJob] = []
    stops_admitted = 0
    for job in jobs:
        if job.stop is StopKind.NOT_A_STOP:
            run.append(job)
        elif stops_admitted < max_stops:
            stops_admitted += 1
            run.append(job)
        elif job.stop is StopKind.CARRIED:
            carry.append(job)
        else:
            dropped.append(job)
    return run, carry, dropped


def _log_cap_outcome(
    carry: list[PostLockJob], dropped: list[PostLockJob], cap: int
) -> None:
    """Warn once per non-empty surplus bucket, naming the labels.

    The labels make a persistently stuck worker (its stop times out but it
    stays on the roster, so the sweep re-queues it every tick and it keeps
    consuming a cap slot) identifiable from the log.
    """
    if dropped:
        _logger.warning(
            "post_lock_stop_cap_dropped count=%d cap=%d labels=%s",
            len(dropped),
            cap,
            ",".join(job.label for job in dropped),
        )
    if carry:
        _logger.warning(
            "post_lock_stop_cap_carried count=%d cap=%d labels=%s",
            len(carry),
            cap,
            ",".join(job.label for job in carry),
        )


def run_post_lock_jobs(
    deferred: DeferredReconcileJobs, *, max_stops: int = MAX_STOP_JOBS_PER_TICK
) -> None:
    """Run the queued post-lock jobs in insertion order, capping stop jobs.

    Must be called with NO ``sessions_lock`` held. Stops carried from an
    earlier tick run first, then ``deferred.post_lock``. At most *max_stops*
    stop jobs run (a stop that raises still counts: the cap bounds wall time);
    see :class:`StopKind` for what happens to the surplus. Non-stop jobs are
    never capped. A job raising an ``Exception`` is logged with its traceback
    and the next job still runs, so this function does not raise for an
    ordinary job failure -- which is what lets ``reconcile()`` call it from a
    ``finally`` without replacing an in-flight exception from the locked body.
    A ``BaseException`` (e.g. ``KeyboardInterrupt``) propagates unchanged and
    abandons the jobs that have not run yet, except the ``CARRIED`` ones: the
    sweep cannot redo those, so they are returned to the front of the
    carry-over (ahead of the surplus already held) for the next drain. The
    job that raised was attempted and is not re-queued.
    """
    jobs = [*_CARRYOVER.take(), *deferred.post_lock]
    run, carry, dropped = _plan_drain(jobs, max_stops)
    _CARRYOVER.put(carry)
    _log_cap_outcome(carry, dropped, max_stops)
    pending = iter(run)
    try:
        for job in pending:
            try:
                job.run()
            except Exception:
                # Why: a broad catch is required here (PYTHON-PATTERNS "When
                # Bare Exception Catches Are Acceptable"). 1. The jobs make
                # external calls (daemon IPC, gh) whose failure modes go beyond
                # any one exception family -- NativeDaemonClient.stop() swallows
                # only FileNotFoundError/TimeoutExpired, so any other OSError
                # escapes. 2. _logger.exception records the full traceback and
                # the job label. 3. Non-critical: every job is best-effort and
                # a lost recoverable surface stop is re-found by the next
                # tick's leaked-worker sweep (a CARRIED stop that raises is not
                # re-queued, its surface is still swept), while letting it
                # escape would skip the sibling jobs and the review dispatch.
                # 4. Paired tests: tests/test_reconcile_deferred.py (raising
                # job isolated).
                _logger.exception("post_lock_job_failed label=%s", job.label)
    except BaseException:
        # *pending* is the shared iterator, so what is left in it is exactly
        # the jobs that never started. Re-carry the CARRIED ones and let the
        # interrupt propagate.
        _CARRYOVER.put(
            [job for job in pending if job.stop is StopKind.CARRIED], front=True
        )
        raise
