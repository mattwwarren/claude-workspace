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

A lost job is recoverable: a queued surface stop that never runs (the drain
was interrupted, or the job raised) leaves a roster worker whose session is
already terminal, which the next tick's leaked-worker sweep
(:mod:`cw.reconcile.leaked_workers`) finds and stops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from cw.reconcile import _deps

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.reconcile.review_recipes.core import DeferredReviewDispatch

_logger = logging.getLogger(__name__)

_SURFACE_STOP_LABEL_PREFIX = "surface_stop:"


@dataclass(frozen=True)
class PostLockJob:
    """One external call to run after ``sessions_lock`` releases.

    *label* names the job in the failure log and lets a producer ask whether an
    equivalent job is already queued (:func:`is_surface_stop_queued`). *run*'s
    return value is discarded; it is typed ``object`` so a job may wrap a
    helper that returns something (e.g. an acted ticket id).
    """

    label: str
    run: Callable[[], object]


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
    """Return True if a stop of *surface_ref* is already queued on *deferred*."""
    label = _surface_stop_label(surface_ref)
    return any(job.label == label for job in deferred.post_lock)


def _run_surface_stop(surface_ref: str) -> None:
    """Stop *surface_ref*, resolving the daemon client at drain time."""
    _deps.get_native_daemon_client().stop(surface_ref)


def defer_surface_stop(deferred: DeferredReconcileJobs, surface_ref: str) -> None:
    """Queue a stop of the daemon surface *surface_ref* on *deferred*.

    Idempotent per surface: a second request for a surface already queued
    this tick adds nothing, so the surface is stopped once.
    """
    if is_surface_stop_queued(deferred, surface_ref):
        return
    deferred.post_lock.append(
        PostLockJob(
            label=_surface_stop_label(surface_ref),
            run=partial(_run_surface_stop, surface_ref),
        )
    )


def run_post_lock_jobs(deferred: DeferredReconcileJobs) -> None:
    """Run every queued post-lock job in insertion order.

    Must be called with NO ``sessions_lock`` held. A job raising an
    ``Exception`` is logged with its traceback and the next job still runs,
    so this function does not raise for an ordinary job failure -- which is
    what lets ``reconcile()`` call it from a ``finally`` without replacing an
    in-flight exception from the locked body. A ``BaseException`` (e.g.
    ``KeyboardInterrupt``) propagates and abandons the remaining jobs.
    """
    for job in deferred.post_lock:
        try:
            job.run()
        except Exception:
            # Why: a broad catch is required here (PYTHON-PATTERNS "When Bare
            # Exception Catches Are Acceptable"). 1. The jobs make external
            # calls (daemon IPC, gh) whose failure modes go beyond any one
            # exception family -- NativeDaemonClient.stop() swallows only
            # FileNotFoundError/TimeoutExpired, so any other OSError escapes.
            # 2. _logger.exception records the full traceback and the job
            # label. 3. Non-critical: every job is best-effort and a lost
            # surface stop is recovered by the next tick's leaked-worker
            # sweep, while letting it escape would skip the sibling jobs and
            # the review dispatch. 4. Paired tests:
            # tests/test_reconcile_deferred.py (raising job isolated).
            _logger.exception("post_lock_job_failed label=%s", job.label)
