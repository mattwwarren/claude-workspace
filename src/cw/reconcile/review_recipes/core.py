"""The review-recipes tick entry point + the stateless repeat-fire counter.

Package split (#1315, part 2 of 2). This module holds ``run_review_recipes`` —
the single per-tick detect->act entry point ``cw.reconcile.core`` calls — and the
stateless ``_detect_repeat_fire_counts`` burst counter it consumes once per tick.

``run_review_recipes`` is the package's sole top-level orchestration entry point;
it is the one sanctioned site that imports the sibling recipe modules' public
detect/act pairs (``address_review``, ``auto_fix_ci``, ``request_reviewer``,
``escalate_merge_block``), so the recipe modules themselves stay leaf modules that
only depend on ``_shared``.

Who dispatches (#1229): ``address_review`` and ``auto_fix_ci`` end in a dispatch
(a headless ``/address-review`` spawn; a requeue the dispatch loop then picks
up) that must run after ``sessions_lock`` releases and that mutates PR branches
or the queue. Only the live dispatch loop's ``reconcile()`` call drains that
dispatch, so ``run_review_recipes`` runs those two acts only when handed a
:class:`DeferredReviewDispatch` sink; operator read commands (``cw status`` /
``list`` / ``start`` / ``doctor``) pass none, and the two acts are then skipped
entirely rather than stamping a one-shot latch nobody will honour.
``request_reviewer`` runs for every caller, but its ``gh`` call is likewise
not made under ``sessions_lock``: it is queued as a
:class:`~cw.reconcile.deferred.PostLockJob` on ``reconcile()``'s post-lock sink
(#1232), which wraps that review sink.

Shared cross-recipe infrastructure (the pure ``_detect_by_attention_state``
classifier, the act-phase helpers, and the recipe/attention/payload constants)
lives in ``cw.reconcile.review_recipes._shared``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from pydantic import ValidationError

from cw.config import load_effective_clients
from cw.dev_queue import load_dev_queue
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.reconcile.deferred import PostLockJob
from cw.reconcile.review_recipes._shared import (
    _PAYLOAD_KEY_CLIENT,
    _PAYLOAD_KEY_RECIPE,
    _PAYLOAD_KEY_TICKET_ID,
    _emit_pr_action_failed,
)

# Sanctioned sibling imports — used only by run_review_recipes (the package's
# single top-level tick entry point), never by a leaf recipe helper. See the
# "Orchestrator/tick-infra exception" in the #1315 plan: recipe modules depend
# on _shared only; this one entry-point function is the exception.
from cw.reconcile.review_recipes.address_review import (
    _act_address_review,
    _detect_address_review,
    _dispatch_address_review,
    _DispatchJob,
)
from cw.reconcile.review_recipes.auto_fix_ci import (
    _act_auto_fix_ci,
    _detect_auto_fix_ci,
    _dispatch_auto_fix_ci,
    _RedispatchJob,
)
from cw.reconcile.review_recipes.escalate_merge_block import (
    _act_escalate_merge_block,
    _detect_escalate_merge_block,
)
from cw.reconcile.review_recipes.request_reviewer import (
    _act_request_reviewer,
    _detect_request_reviewer,
    _dispatch_request_reviewer,
    _ReviewerJob,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import OrchestratorConfig
    from cw.reconcile.deferred import DeferredReconcileJobs

_log = logging.getLogger("cw.reconcile.review_recipes")


def _detect_repeat_fire_counts(
    *, config: OrchestratorConfig, now: datetime | None = None
) -> dict[tuple[str, str, str], int]:
    """Count PR_ACTION_TAKEN events per ``(client, ticket_id, recipe)`` in the window.

    Stateless burst detector (#1201): replays PR_ACTION_TAKEN events from the
    inbox and buckets them by ``(client, ticket_id, recipe)`` — ``client`` is
    load-bearing here: ``ticket_id`` alone is a per-repo GitHub issue number, not
    globally unique across this multi-tenant system's clients (same rationale as
    the ``by_key`` dicts in ``_act_address_review`` et al.), so two different
    clients whose numeric issue IDs collide must not share a count. Counts only
    events recorded within ``config.review_recipe_repeat_fire_window_minutes`` of
    *now* (default ``datetime.now(UTC)``) — the window itself is applied via
    ``read_events(since_ts=...)`` rather than a manual post-filter, so events
    outside the window are never even materialized. The act phase compares its
    own about-to-fire event against these counts (``_record_pr_action_taken``) to
    decide whether a repeat-fire burst has crossed the attention threshold.
    Read-only and resilient: a failed inbox read degrades to an empty dict so a
    corrupt inbox never blocks the act phase. Called ONCE per reconcile tick (in
    ``run_review_recipes``), outside every ``dev_queue_lock()``.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    cutoff = resolved_now - timedelta(
        minutes=config.review_recipe_repeat_fire_window_minutes
    )
    try:
        events = read_events(
            event_types=[OrchestratorEventType.PR_ACTION_TAKEN], since_ts=cutoff
        )
    except (OSError, json.JSONDecodeError, ValidationError):
        return {}
    counts: dict[tuple[str, str, str], int] = {}
    for event in events:
        client = event.payload.get(_PAYLOAD_KEY_CLIENT)
        ticket_id = event.payload.get(_PAYLOAD_KEY_TICKET_ID)
        recipe = event.payload.get(_PAYLOAD_KEY_RECIPE)
        if (
            not isinstance(client, str)
            or not isinstance(ticket_id, str)
            or not isinstance(recipe, str)
        ):
            continue
        key = (client, ticket_id, recipe)
        counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass
class DeferredReviewDispatch:
    """Caller-owned sink for review-recipe dispatch jobs run after ``sessions_lock``.

    ``address_review`` (``spawn_create_impl``) ends in a call that re-acquires
    ``sessions_lock``, so it cannot run while ``reconcile()`` holds it (#1229).
    ``reconcile()`` therefore creates one of these, threads it down through
    ``_reconcile_locked`` into :func:`run_review_recipes`, and calls
    :func:`dispatch_deferred_review_jobs` on it from a ``finally`` once its lock
    has released. ``run_review_recipes`` appends each recipe's jobs the moment
    that recipe's act phase returns: the act phase has already stamped the
    one-shot latch and emitted ``PR_ACTION_TAKEN``, so a job living only in a
    local or return value would be lost -- latch burned, no
    ``PR_ACTION_FAILED`` -- to any exception raised before the dispatch. The
    sink is mutable (lists) precisely so the caller can still see the jobs
    after such an exception.
    """

    address_review: list[_DispatchJob] = field(default_factory=list)
    auto_fix_ci: list[_RedispatchJob] = field(default_factory=list)


def _run_isolated(
    dispatch: Callable[[], str | None],
    *,
    ticket_id: str,
    payload_base: dict[str, object],
) -> str | None:
    """Run one deferred job; return its acted ``ticket_id`` or ``None``.

    Per-job isolation (#1229): the job's own dispatch helper absorbs the
    expected ``CwError`` family into a ``PR_ACTION_FAILED`` correction, but
    anything else it raises (``OSError`` from a spawn, ``ValidationError`` from
    a state load/save, a daemon error) would otherwise escape, drop every
    sibling job whose one-shot latch is already burned, and skip the rest of
    the reconcile post-pass. Those are logged with the full traceback and
    recorded as ``PR_ACTION_FAILED`` here instead.
    """
    try:
        return dispatch()
    except Exception as exc:  # noqa: BLE001
        # Sanctioned broad-catch per PYTHON-PATTERNS.md:316-331 (4-part justification):
        # 1. The dispatch helpers shell out to the daemon, the filesystem and the
        #    state/queue stores — failure modes include OSError, ValidationError
        #    and arbitrary daemon errors beyond the CwError family they handle.
        # 2. Logging: _log.exception captures the full traceback, and a durable
        #    PR_ACTION_FAILED correction is recorded with the failure reason.
        # 3. Non-critical to the pass: the sibling jobs (whose latches are already
        #    burned) and the post-lock steps must still run; this job's failure is
        #    surfaced, not swallowed.
        # 4. Paired test: tests/test_reconcile_core.py
        #    test_non_cwerror_job_failure_is_isolated_and_recorded.
        _log.exception("review_recipe_dispatch_crashed ticket=%s", ticket_id)
        try:
            _emit_pr_action_failed(
                payload_base,
                error=f"{type(exc).__name__}: {exc}",
                ticket_id=ticket_id,
            )
        except (OSError, CwError):
            # The failure record is itself an inbox write: a fault that made the
            # job fail (disk full, inbox lock) can fail it too. Letting that
            # escape would skip every sibling job (latches already burned) and,
            # from reconcile()'s ``finally``, replace an in-flight exception, so
            # log it and move on.
            _log.exception(
                "review_recipe_pr_action_failed_emit_crashed ticket=%s", ticket_id
            )
        return None


def _reviewer_request_job(job: _ReviewerJob) -> PostLockJob:
    """Wrap one prepared reviewer request as a post-lock job (#1232).

    The gh call runs through :func:`_run_isolated`, so a crash is logged and
    corrected with ``PR_ACTION_FAILED`` exactly as a review dispatch job's is.
    """
    return PostLockJob(
        label=f"reviewer_request:{job.client}:{job.ticket_id}",
        run=partial(
            _run_isolated,
            partial(_dispatch_request_reviewer, job),
            ticket_id=job.ticket_id,
            payload_base=job.payload_base,
        ),
    )


def dispatch_deferred_review_jobs(deferred: DeferredReviewDispatch) -> list[str]:
    """Execute the jobs in the sink; return the acted ticket_ids.

    Must be called with NO ``sessions_lock`` held (#1229) — see
    :class:`DeferredReviewDispatch`. Guarantees: address_review jobs run first,
    then auto_fix_ci jobs (the order ``run_review_recipes`` fires the recipes
    in); a job that fails for ANY reason is logged and recorded as
    ``PR_ACTION_FAILED`` and the next job still runs, so this function does not
    raise for an ordinary ``Exception`` from a job, nor when the failure record
    itself cannot be written (``OSError``/``CwError`` from the inbox are logged);
    a ``BaseException`` such as ``KeyboardInterrupt`` still propagates and
    abandons the undispatched jobs. That non-raising property is what lets
    ``reconcile()`` call it from a ``finally`` without masking an in-flight
    exception from the locked body.
    """
    acted: list[str] = []
    for address_job in deferred.address_review:
        ticket_id = _run_isolated(
            partial(_dispatch_address_review, address_job),
            ticket_id=address_job.ticket_id,
            payload_base=address_job.payload_base,
        )
        if ticket_id is not None:
            acted.append(ticket_id)
    for redispatch_job in deferred.auto_fix_ci:
        ticket_id = _run_isolated(
            partial(_dispatch_auto_fix_ci, redispatch_job),
            ticket_id=redispatch_job.ticket_id,
            payload_base=redispatch_job.payload_base,
        )
        if ticket_id is not None:
            acted.append(ticket_id)
    return acted


def run_review_recipes(
    *, config: OrchestratorConfig, jobs: DeferredReconcileJobs
) -> list[str]:
    """Run all enabled review recipes for one reconcile tick (P2: detect → act).

    No-op (returns ``[]`` immediately) unless ``config.review_recipes_enabled``
    is True. Loads a fresh dev-queue snapshot
    itself rather than accepting one
    from the caller — by the wiring point in ``_reconcile_locked`` several prior
    sweeps have already mutated and saved the queue, so a caller-supplied
    snapshot would be stale (mirrors ``run_gate_recipes``). No ``load_state()``
    call: candidate ``client``/``lane`` come straight off the task, so no
    ``CwState`` lookup is needed. No ``now`` parameter: the act phase performs no
    time-stamped mutation.

    Per-lane enablement (RFC 0010 P3) is resolved against effective clients —
    ``load_effective_clients`` so lane pause/override state is honoured, matching
    ``run_gate_recipes``. Loaded once and threaded into both the detect and act
    phases (mirrors ``run_gate_recipes``) rather than re-read inside the act
    phase's lock.

    P4 (#1099) adds three sibling recipes, each routed by a distinct PR
    attention state (1:1 with a recipe; see the routing test): ``auto_fix_ci``
    (re-dispatches a ci_failing PR into auto-dev), ``request_reviewer`` (requests
    a reviewer per the repo's review_strategy on a no_reviewer PR), and
    ``escalate_merge_block`` (fires one durable escalation per merge-blocked
    episode). Each detect->act pair runs against the same fresh ``tasks``
    snapshot; the ``request_reviewer``, ``escalate_merge_block``,
    ``auto_fix_ci``, and (GitHub #1206) ``address_review`` act phases each
    perform a small latch write (all four act phases now write a one-shot
    latch field, not a status transition — none remain purely read-only under
    their lock).

    Returns the ticket_ids the ``escalate_merge_block`` recipe reports as
    acted — it finishes its action inline, with no ``sessions_lock``
    re-entry and no external call.

    *jobs* is ``reconcile()``'s post-lock sink (#1232). Every recipe appends
    the work it prepares to it IMMEDIATELY after that recipe's act returns,
    before the next recipe runs, so a later exception cannot lose a job whose
    latch is already stamped; ``reconcile()`` drains the sink after releasing
    the lock. ``request_reviewer`` runs for every caller: its ``gh pr edit``
    call becomes a ``reviewer_request:<client>:<ticket_id>``
    :class:`~cw.reconcile.deferred.PostLockJob` on ``jobs.post_lock``, run
    through :func:`_run_isolated`. Its acted ids are therefore not returned.

    ``jobs.review`` selects whether the dispatching recipes run (#1229). The
    ``address_review`` and ``auto_fix_ci`` recipes end in a dispatch that
    re-acquires ``sessions_lock`` (``reconcile()`` still holds it when this
    runs) and can spawn headless workers that push to PR branches, so they run
    ONLY when the caller supplies a review sink it will drain: with
    ``jobs.review`` ``None`` (every ``reconcile()`` caller except the live
    dispatch loop — ``cw status``/``list``/``start``/``doctor``) their acts are
    skipped entirely, so they neither stamp their one-shot latch nor emit
    ``PR_ACTION_TAKEN`` for a dispatch nobody would perform. The caller
    dispatches the review sink with :func:`dispatch_deferred_review_jobs`.
    """
    if not config.review_recipes_enabled:
        return []
    tasks = load_dev_queue().tasks
    clients = load_effective_clients()
    # Compute the repeat-fire burst counts ONCE per tick (#1201), outside every
    # dev_queue_lock() — one read_events replay threaded into all four act
    # phases, mirroring how clients/tasks are loaded once and shared.
    repeat_fire_counts = _detect_repeat_fire_counts(config=config)
    if jobs.review is not None:
        jobs.review.address_review.extend(
            _act_address_review(
                _detect_address_review(tasks, clients=clients, config=config),
                clients=clients,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            )
        )
        jobs.review.auto_fix_ci.extend(
            _act_auto_fix_ci(
                _detect_auto_fix_ci(tasks, clients=clients, config=config),
                clients=clients,
                config=config,
                repeat_fire_counts=repeat_fire_counts,
            )
        )
    for reviewer_job in _act_request_reviewer(
        _detect_request_reviewer(tasks, clients=clients, config=config),
        clients=clients,
        config=config,
        repeat_fire_counts=repeat_fire_counts,
    ):
        jobs.post_lock.append(_reviewer_request_job(reviewer_job))
    return _act_escalate_merge_block(
        _detect_escalate_merge_block(tasks, clients=clients, config=config),
        config=config,
        repeat_fire_counts=repeat_fire_counts,
    )
