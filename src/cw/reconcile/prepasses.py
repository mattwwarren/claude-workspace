"""Lockless pre-lock capture orchestration for ``reconcile()``.

``cw.reconcile.core.reconcile`` calls these before it takes ``sessions_lock``,
in this order: the gh merge-state loop (``_capture_merge_state``), the codex
clean probes (#2563), the gate recipes' plan-of-record prefetch (#2545), the
review recipes' repo slugs (#2564, ``capture_review_repo_slugs``, called from
core directly), the worktree dirty checks (#2548), then
``_capture_closing_prepasses``: the shipped-release PR lookups (#2715), the
main-drift and concierge recipe 3 git reads (#2546), the salvaged-sentinel
scope (#2641) and, last, the local harvest's git facts (#2565). Every ``gh``
and ``git`` call the in-lock sweeps would otherwise need runs here; the
in-lock code only reads the results (ADR-0019).

The codex clean probes, plan probes, dirty checks and harvest facts each own
their own state and dev-queue read, contain a read error
(``OSError``/``ValueError``) as a logged empty store and catch nothing
broader. ``_capture_merge_state`` is the exception: ``reconcile()`` feeds it
``pre_state`` and it reads the dev queue with no containment, so a dev-queue
read error there propagates out of ``reconcile()`` before the lock.
``_capture_closing_prepasses`` delegates to the drivers. The drift/cancelled
and scope drivers live in
:mod:`cw.reconcile.lockless_prepass`; this module sequences them. Split out
of ``cw.reconcile.core`` (#2896).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from cw.config import load_state
from cw.dev_queue import load_dev_queue
from cw.gh import resolve_merged_via_pr_state
from cw.models import SessionOrigin
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    feature_branch_key,
    ticket_id_for_session,
)
from cw.reconcile.codex_boot import CAPTURE_BUDGET_SECONDS, CleanProbes
from cw.reconcile.codex_reparks import capture_repark_probes
from cw.reconcile.dirty_checks import (
    DIRTY_CHECK_BUDGET_SECONDS,
    DIRTY_CHECK_MAX_PER_TICK,
    DirtyChecks,
    prepass_phantom_ids,
)
from cw.reconcile.gate_plan_probes import (
    PLAN_PREFETCH_BUDGET_SECONDS,
    PLAN_PREFETCH_MAX_PER_TICK,
    PlanProbes,
)
from cw.reconcile.gate_recipes import capture_plan_probes
from cw.reconcile.harvest_synthesis import HARVEST_CAPTURE_BUDGET_SECONDS, HarvestFacts
from cw.reconcile.local import (
    capture_codex_harvest_probes,
    capture_local_harvest_facts,
)
from cw.reconcile.lockless_prepass import (
    capture_drift_and_cancelled_probes,
    capture_salvage_scope_probes,
)
from cw.reconcile.phantom import capture_phantom_dirty_checks
from cw.reconcile.shipped_release import capture_shipped_pr_probes
from cw.reconcile.tasks import (
    _client_cwd,
    _is_dangling_client,
    capture_backstop_dirty_checks,
)
from cw.reconcile.usage_limit_mid_turn import sessions_with_act_in_flight

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, OrchestratorConfig, TicketTask
    from cw.reconcile._shared import ScopeProbes
    from cw.reconcile.cancelled_row_probes import CancelledRowProbes
    from cw.reconcile.dirty_checks import RosterReader
    from cw.reconcile.main_drift import MainDriftProbes
    from cw.reconcile.shipped_release import ShippedPrProbes

_log = logging.getLogger(__name__)


def _capture_merge_state(
    pre_state: CwState,
    *,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> tuple[frozenset[str], frozenset[str]]:
    """Lockless pre-pass: PR merge state for ACTIVE/IDLE DAEMON sessions.

    The gh subprocess must NOT run under ``sessions_lock`` (liveness
    requirement, #485); mirrors ``complete_timed_out_merged_tasks()``. A
    fresh ``pr_state`` on the session's row answers without a gh call
    (#975). Reads the dev queue itself, even when *pre_state* has no
    sessions.

    Returns ``(merged_ticket_ids, gh_blocked_ticket_ids)``: the phantom sweep
    consumes both as-is under the lock (GitHub #637).
    """
    _task_by_ticket: dict[tuple[str, str], TicketTask] = {
        (t.client, t.ticket_id): t for t in load_dev_queue().tasks
    }
    # (client, ticket_id) pairs, for consumers that must not match a merged
    # ticket against a *different* client's same-numbered ticket (#1054) —
    # ticket_id strings are not globally unique across clients. merged_ticket_ids
    # (bare) is derived from this below rather than accumulated in parallel, so
    # the two sets cannot drift out of sync.
    _merged_client_tids: list[tuple[str, str]] = []
    _gh_blocked_tids: list[str] = []
    _gh_available = True
    for _session in pre_state.sessions:
        if _session.status not in _LIVE_STATUSES:
            continue
        if _session.origin is not SessionOrigin.DAEMON:
            continue
        _ticket_id = ticket_id_for_session(_session.name)
        if _ticket_id is None:
            continue
        if not _gh_available:
            _gh_blocked_tids.append(_ticket_id)
            continue
        _branch = feature_branch_key(_session.client, _ticket_id, clients)
        if _is_dangling_client(_session.client, clients):
            # Client was configured but has since been removed/renamed --
            # route to gh_blocked (SESSION_NEEDS_ATTENTION downstream)
            # rather than risk an unscoped gh call (GitHub #1269).
            _gh_blocked_tids.append(_ticket_id)
            continue
        _cwd = _client_cwd(_session.client, clients)
        _merged, _gh_avail = resolve_merged_via_pr_state(
            _ticket_id,
            _session.client,
            _task_by_ticket,
            max_age_seconds=config.pr_hydration_interval_seconds,
            gh_fallback=partial(
                _deps.pr_is_merged_for_ticket, _ticket_id, branch=_branch, cwd=_cwd
            ),
        )
        if not _gh_avail:
            _gh_available = False
            _gh_blocked_tids.append(_ticket_id)
            continue
        if _merged is None:
            # merged=None is a transient per-ticket error (e.g. network blip on
            # a single PR lookup); fall through to normal revert so the session
            # is not silently stuck.  A structural gh outage sets _gh_avail=False
            # (above), which routes ALL subsequent tickets to gh_blocked_tids.
            continue
        if _merged:
            _merged_client_tids.append((_session.client, _ticket_id))
    merged_ticket_ids = frozenset(tid for _client, tid in _merged_client_tids)
    gh_blocked_ticket_ids = frozenset(_gh_blocked_tids)
    return merged_ticket_ids, gh_blocked_ticket_ids


def _capture_codex_clean_probes(
    *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
) -> CleanProbes:
    """Lockless pre-pass: capture the codex clean-check git per candidate (#2563).

    Runs in ``reconcile()`` before ``sessions_lock`` is taken, so the in-lock
    re-park sweep and the harvest sweep's codex branch run no git: each
    module re-runs its own detect predicate here over a fresh snapshot and
    records the probes, which the in-lock code only reads. Bounded by
    ``CAPTURE_BUDGET_SECONDS``.

    ``reconcile()`` also serves ``cw status``/``cw list``/``cw start``/``cw
    doctor``, so this must never fail it: a state or dev-queue read error
    (``json.JSONDecodeError`` and pydantic's ``ValidationError`` are both
    ``ValueError``) is logged and yields an empty ``CleanProbes``, a miss for
    every candidate, which then defers in-lock. Nothing broader is caught.
    """
    try:
        state = load_state()
        tasks = load_dev_queue().tasks
    except (OSError, ValueError):
        _log.warning(
            "reconcile: codex clean-probe pre-pass could not read state;"
            " deferring all codex candidates this tick",
            exc_info=True,
        )
        return CleanProbes()
    probes = CleanProbes(budget_seconds=CAPTURE_BUDGET_SECONDS)
    now = datetime.now(UTC)
    capture_repark_probes(
        state, tasks, now=now, clients=clients, config=config, probes=probes
    )
    capture_codex_harvest_probes(state, tasks, config=config, probes=probes)
    return probes


def _capture_plan_probes(
    *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
) -> PlanProbes:
    """Lockless pre-pass: prefetch the gate recipes' plan-of-record reads (#2545).

    Runs in ``reconcile()`` before ``sessions_lock`` is taken, so the in-lock
    plan recipe runs no ``gh``/``git``: ``capture_plan_probes`` re-runs the
    plan detect here over a fresh snapshot and records each body, which the
    in-lock detect only reads. Bounded by ``PLAN_PREFETCH_MAX_PER_TICK`` reads
    and ``PLAN_PREFETCH_BUDGET_SECONDS``. Reads nothing when the gate-recipe
    master switch is off.

    Like ``_capture_codex_clean_probes`` it must never fail ``reconcile()``
    (which serves ``cw status``/``list``/``start``/``doctor``) over a state or
    dev-queue read error: that is logged and yields an empty ``PlanProbes``,
    so every plan candidate is skipped this tick. Nothing broader is caught.
    """
    if not config.gate_recipes_enabled:
        return PlanProbes()
    try:
        state = load_state()
        tasks = load_dev_queue().tasks
    except (OSError, ValueError):
        _log.warning(
            "reconcile: plan prefetch pre-pass could not read state;"
            " skipping all plan-gate candidates this tick",
            exc_info=True,
        )
        return PlanProbes()
    probes = PlanProbes(
        budget_seconds=PLAN_PREFETCH_BUDGET_SECONDS,
        max_captures=PLAN_PREFETCH_MAX_PER_TICK,
    )
    capture_plan_probes(state, tasks, clients=clients, config=config, probes=probes)
    return probes


def _capture_dirty_checks(
    *, config: OrchestratorConfig, roster: RosterReader
) -> DirtyChecks:
    """Lockless pre-pass: capture the worktree dirty checks (#2548).

    Runs in ``reconcile()`` before ``sessions_lock`` is taken, so neither the
    TIMED_OUT/COMPLETED backstops nor the phantom detect runs git in-lock.
    Backstop sessions with a RUNNING row are captured first, then the
    phantoms; the phantom set comes from a lockless roster call made only
    when a live DAEMON session has a worktree. Bounded by
    ``DIRTY_CHECK_MAX_PER_TICK`` checks and ``DIRTY_CHECK_BUDGET_SECONDS``.

    *roster* is that roster call: ``reconcile()`` passes
    ``cw.reconcile.core._claude_agents_json`` (#2896), so a patch on core's
    binding reaches this capture too.

    Like ``_capture_codex_clean_probes`` it must never fail ``reconcile()``
    over a state or dev-queue read error: that is logged and yields an empty
    ``DirtyChecks``, a miss for every worktree session, which then defers
    in-lock. Nothing broader is caught. After both captures it settles the
    tick's dirty-check outages once (#2662): the edge-triggered
    ``worktree_probe_unavailable`` signal and the next tick's capture order.
    The early unreadable-state return skips that, leaving both untouched.
    """
    try:
        state = load_state()
        tasks = load_dev_queue().tasks
    except (OSError, ValueError):
        _log.warning(
            "reconcile: dirty-check pre-pass could not read state;"
            " deferring all dirty-checked sessions this tick",
            exc_info=True,
        )
        return DirtyChecks()
    checks = DirtyChecks(
        budget_seconds=DIRTY_CHECK_BUDGET_SECONDS,
        max_captures=DIRTY_CHECK_MAX_PER_TICK,
    )
    now = datetime.now(UTC)
    capture_backstop_dirty_checks(state, tasks, now=now, checks=checks)
    phantom_set = prepass_phantom_ids(
        state,
        roster=roster,
        acting=sessions_with_act_in_flight(tasks),
        now=now,
    )
    capture_phantom_dirty_checks(
        state,
        phantom_set,
        {t.ticket_id: t for t in tasks},
        now=now,
        config=config,
        checks=checks,
    )
    checks.settle_outages()
    return checks


def _capture_harvest_facts() -> HarvestFacts:
    """Lockless pre-pass: capture the local harvest's git facts (#2565).

    Runs in ``reconcile()`` before ``sessions_lock`` is taken, so the in-lock
    local harvest runs no git: ``capture_local_harvest_facts`` re-runs the
    harvest detect here over a fresh snapshot and records each git-backed
    candidate's facts, which the in-lock act only looks up. Bounded by
    ``HARVEST_CAPTURE_BUDGET_SECONDS`` plus one candidate's bounded git calls.

    Like ``_capture_codex_clean_probes`` it must never fail ``reconcile()``
    over a state or dev-queue read error: that is logged and yields an empty
    ``HarvestFacts``, a miss for every git-backed candidate, which then defers
    in-lock. Nothing broader is caught.
    """
    try:
        state = load_state()
        tasks = load_dev_queue().tasks
    except (OSError, ValueError):
        _log.warning(
            "reconcile: harvest-facts pre-pass could not read state;"
            " deferring all git-backed harvest candidates this tick",
            exc_info=True,
        )
        return HarvestFacts()
    facts = HarvestFacts(budget_seconds=HARVEST_CAPTURE_BUDGET_SECONDS)
    capture_local_harvest_facts(state, tasks, facts=facts)
    return facts


def _capture_closing_prepasses(
    *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
) -> tuple[
    ShippedPrProbes, MainDriftProbes, CancelledRowProbes, ScopeProbes, HarvestFacts
]:
    """Shipped PRs (#2715), main drift + recipe 3 (#2546), salvaged scope
    (#2641), harvest facts last."""
    shipped = capture_shipped_pr_probes(config=config, clients=clients)
    drift, cancelled = capture_drift_and_cancelled_probes(
        config=config, clients=clients
    )
    scope = capture_salvage_scope_probes()
    return shipped, drift, cancelled, scope, _capture_harvest_facts()
