"""Top-level reconcile orchestration.

``reconcile`` runs the lockless pre-passes -- gh merge state, the codex clean
probes (#2563), the gate recipes' plan-of-record prefetch (#2545), the review
recipes' repo slugs (#2564), the worktree dirty checks (#2548) and, last, the
local harvest's git facts (#2565) -- then
``_reconcile_locked`` under ``sessions_lock`` (the
detect/emit/act sweeps for stalled, idle, and phantom sessions), then the
post-lock gh/git passes. See the package ``__init__`` docstring and
ADR-0005/ADR-0006 for the invariants.

Anything whose side effect re-acquires ``sessions_lock`` (a ``spawn_create_impl``
call, a dispatch tick that re-enters ``reconcile()``) must NOT run inside
``_reconcile_locked``: the lock is not reentrant and the second acquisition
raises ``CwLockReentrancyError`` (#1228), which the callers' ``except
CwError`` would silently swallow. Such work is hoisted to ``reconcile()``'s
post-lock section — ``run_fix_dispatch`` (#2064) is sited there directly, and
the review recipes' ``address_review`` dispatch (#1229) is prepared under the
lock into a caller-owned ``DeferredReviewDispatch`` sink that ``reconcile()``
creates, hands down through ``_reconcile_locked``, and drains from a ``finally``
once the lock has released. That review sink is one member of the broader
post-lock job sink (``cw.reconcile.deferred.DeferredReconcileJobs``, #1232),
which also carries the bounded external calls the act phases decide on under
the lock but run after it -- every daemon surface stop, the gate recipes'
audit comments, and the review recipes' reviewer request.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from cw.config import (
    load_clients,
    load_orchestrator_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue
from cw.gh import resolve_merged_via_pr_state
from cw.models import SessionOrigin
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    ReconcileReport,
    _backfill_claude_session_ids,
    _claude_agents_json,
    _emit_reap_proposed,
    _looks_like_daemon_outage,
    _stamp_session_id_mismatch_advisories,
    compute_drift,
    feature_branch_key,
    ticket_id_for_session,
)
from cw.reconcile.codex_boot import CAPTURE_BUDGET_SECONDS, CleanProbes
from cw.reconcile.codex_reparks import (
    capture_repark_probes,
    run_codex_live_writer_reparks,
)
from cw.reconcile.concierge import run_concierge_recoveries
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.dirty_checks import (
    DIRTY_CHECK_BUDGET_SECONDS,
    DIRTY_CHECK_MAX_PER_TICK,
    DirtyChecks,
    normalize_roster,
    prepass_phantom_ids,
)
from cw.reconcile.escalation import run_escalation_sweep
from cw.reconcile.fix_dispatch import run_fix_dispatch
from cw.reconcile.gate_plan_probes import (
    PLAN_PREFETCH_BUDGET_SECONDS,
    PLAN_PREFETCH_MAX_PER_TICK,
    PlanProbes,
)
from cw.reconcile.gate_recipes import capture_plan_probes, run_gate_recipes
from cw.reconcile.harvest_synthesis import HARVEST_CAPTURE_BUDGET_SECONDS, HarvestFacts
from cw.reconcile.idle import _act_on_idle_candidates, _detect_idle_candidates
from cw.reconcile.leaked_workers import sweep_leaked_daemon_workers
from cw.reconcile.liveness import record_session_liveness_changes
from cw.reconcile.local import (
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
    capture_codex_harvest_probes,
    capture_local_harvest_facts,
)
from cw.reconcile.main_drift import (
    _act_on_main_drift_candidates,
    _detect_main_drift_candidates,
)
from cw.reconcile.phantom import (
    _act_on_phantom_candidates,
    _detect_phantom_candidates,
    capture_phantom_dirty_checks,
)
from cw.reconcile.review_recipes import (
    DeferredReviewDispatch,
    dispatch_deferred_review_jobs,
    run_review_recipes,
)
from cw.reconcile.review_recipes.core import capture_review_repo_slugs
from cw.reconcile.routed_result_sessions import sweep_routed_result_sessions
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
)
from cw.reconcile.tasks import (
    _client_cwd,
    _is_dangling_client,
    capture_backstop_dirty_checks,
    complete_timed_out_merged_tasks,
    park_terminal_sibling_tasks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from cw.reconcile.unowned_running import run_unowned_running_recovery
from cw.reconcile.usage_limit_mid_turn import (
    detect_and_park_mid_turn_usage_limits,
    sessions_with_act_in_flight,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, OrchestratorConfig, TicketTask
    from cw.reconcile.review_recipes._shared import RepoSlugs

_log = logging.getLogger(__name__)


def _run_terminal_backstops_and_sweeps(
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
    clients: dict[str, ClientConfig],
    deferred: DeferredReconcileJobs,
    codex_probes: CleanProbes | None,
    plan_probes: PlanProbes | None,
    repo_slugs: RepoSlugs | None,
    dirty_checks: DirtyChecks | None = None,
) -> tuple[list[str], list[str]]:
    """Run the post-detect TicketTask backstops + RFC 0008 capstone sweeps.

    Both branches of ``_reconcile_locked`` (the no-phantoms early return and
    the normal phantom-handling tail) call this in the same spot: after their
    own detect/act sweep, recover any RUNNING task whose session already
    went TIMED_OUT/COMPLETED without reverting it, park stale PENDING rows
    with a terminal sibling (#876), then run the mechanical recovery reactor
    (opt-in), the live-writer codex-orphan park re-evaluation (#2307,
    unconditional), and durable escalation sweep (unconditional) from GitHub
    #1015.
    All of these load their own fresh dev-queue/state snapshots rather than
    reusing the (possibly now-stale) locals in ``_reconcile_locked``.
    *clients* is the tick's own client scope; the codex re-park sweep acts
    only within it (#2307 review round 1), and reads its clean checks from
    *codex_probes*, captured before the lock (#2563). The gate recipes read
    each plan-of-record body from *plan_probes*, also captured before the
    lock (#2545); ``None`` skips every plan candidate for the tick.
    *repo_slugs*, likewise captured before the lock (#2564), is what the review
    recipes' cross-repo guard reads instead of running git. The TIMED_OUT and
    COMPLETED backstops read each worktree dirty check from *dirty_checks*,
    captured before the lock too (#2548); a session with no usable capture
    defers a tick.
    Extracted to one call site (instead of duplicating 4 lines in each
    branch) to keep ``_reconcile_locked``'s statement count under the
    PLR0915 limit.

    *deferred* is ``reconcile()``'s post-lock job sink (#1232), handed to both
    recipe families, which append the jobs they prepare to it as they go (the
    jobs are NOT returned, so a later step raising cannot lose them):
    ``run_gate_recipes`` queues each audit comment and ``run_review_recipes``
    the reviewer request's gh call on ``post_lock``, and the dispatching
    review recipes put their jobs on its review member (#1229) -- those
    recipes are skipped entirely when that member is ``None``. The reviewer
    request and the audit comments run for every caller, whatever the review
    member: their decisions (one-shot latch, gate release) are stamped
    in-lock either way, and ``reconcile()`` always builds and drains the sink.

    First, it adopts any RUNNING row whose launched session was never
    stamped on it (#2591), so the backstops below see that row bound.

    Returns (timed_out_ticket_ids, completed_silent_ticket_ids).
    """
    run_unowned_running_recovery(clients=clients)
    timed_out_ticket_ids = revert_timed_out_tasks(dirty_checks)
    completed_silent_ticket_ids = revert_completed_silent_tasks(dirty_checks)
    park_terminal_sibling_tasks()
    run_concierge_recoveries(now=now, native_live=native_live, config=config)
    # #2307: re-evaluate codex-orphan parks the boot pass left with an ACTIVE
    # session (live or unprovable writer). Unconditional, like the boot pass,
    # but scoped to this tick's clients: never another client's session.
    run_codex_live_writer_reparks(
        now=now, config=config, clients=clients, probes=codex_probes
    )
    run_gate_recipes(now=now, config=config, deferred=deferred, plan_probes=plan_probes)
    run_review_recipes(config=config, jobs=deferred, repo_slugs=repo_slugs)
    run_escalation_sweep(now=now)
    return timed_out_ticket_ids, completed_silent_ticket_ids


def _without_acts_in_flight(session_ids: list[str]) -> list[str]:
    """Drop phantom session ids whose row carries a mid-turn usage-limit act.

    Such a session may be off the roster because the act stopped it and has
    not closed it yet; the act resumes and finishes it, uncharged (#2324).
    Reads the dev queue fresh, since the mid-turn sweep may have just
    decided or finished acts this tick.
    """
    if not session_ids:
        return session_ids
    acting = sessions_with_act_in_flight(load_dev_queue().tasks)
    return [sid for sid in session_ids if sid not in acting]


def _verify_supervisor_session_id(state: CwState) -> int:
    """Compare stored claude_session_id against the supervisor's resumeSessionId.

    For each ACTIVE/IDLE DAEMON session whose ``surface_ref`` and
    ``claude_session_id`` are both set, reads the supervisor per-session
    ``~/.claude/jobs/<surface_ref>/state.json`` and checks whether its
    ``resumeSessionId`` matches the stored ``claude_session_id``. On
    mismatch: logs a warning and clears ``claude_session_id`` so
    ``_backfill_claude_session_ids`` re-derives it on the next tick.
    ``surface_ref`` is left intact so phantom detection in ``compute_drift``
    continues to observe liveness.

    A missing or unreadable ``state.json`` is treated as "no continuity
    claim from the supervisor" and skipped (not an error). Returns the
    number of sessions whose ``claude_session_id`` was cleared; saves
    state when non-zero. See RFC 0001 Row 8 and GitHub issue #519.
    """
    cleared = 0
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.surface_ref is None or session.claude_session_id is None:
            continue
        resume_id = _deps.read_supervisor_resume_session_id(session.surface_ref)
        if resume_id is None:
            continue
        if resume_id == session.claude_session_id:
            continue
        _log.warning(
            "csid_mismatch: session=%s surface_ref=%s"
            " stored_csid=%s supervisor_resume_id=%s — clearing claude_session_id",
            session.id,
            session.surface_ref,
            session.claude_session_id,
            resume_id,
        )
        session.claude_session_id = None
        cleared += 1
    if cleared:
        save_state(state)
    return cleared


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


def _capture_dirty_checks(*, config: OrchestratorConfig) -> DirtyChecks:
    """Lockless pre-pass: capture the worktree dirty checks (#2548).

    Runs in ``reconcile()`` before ``sessions_lock`` is taken, so neither the
    TIMED_OUT/COMPLETED backstops nor the phantom detect runs git in-lock.
    Backstop sessions with a RUNNING row are captured first, then the
    phantoms; the phantom set comes from a lockless roster call made only
    when a live DAEMON session has a worktree. Bounded by
    ``DIRTY_CHECK_MAX_PER_TICK`` checks and ``DIRTY_CHECK_BUDGET_SECONDS``.

    Like ``_capture_codex_clean_probes`` it must never fail ``reconcile()``
    over a state or dev-queue read error: that is logged and yields an empty
    ``DirtyChecks``, a miss for every worktree session, which then defers
    in-lock. Nothing broader is caught.
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
        roster=_claude_agents_json,
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


def reconcile(*, dispatch_review_jobs: bool = False) -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.

    Returns an empty report without mutating state when
    :func:`_looks_like_daemon_outage` matches — a transient daemon hiccup
    must not trigger mass-reaping.

    *dispatch_review_jobs* (#1229) says whether THIS call may act on the
    ``address_review`` and ``auto_fix_ci`` review recipes. Only the live
    dispatch loop passes True
    (``dispatch.gating.usage_limit._reconcile_usage_limited``).
    Every other caller — ``cw status``/``cw list`` (via
    ``_check_and_mark_dead_sessions``), ``cw start``, ``cw doctor`` — is a
    read-or-housekeeping command that must not, as a side effect, spawn a
    headless ``/address-review`` worker that pushes to a PR branch or burn the
    recipe's one-shot latch. With the default False those two acts are not run
    at all (no latch stamp, no ``PR_ACTION_TAKEN``); ``request_reviewer`` and
    ``escalate_merge_block`` run regardless.

    Post-lock job sink (#1232): every call builds one
    :class:`~cw.reconcile.deferred.DeferredReconcileJobs`, fills it inside the
    lock, and drains it from a ``finally`` after the lock releases. Its review
    member exists only when *dispatch_review_jobs* is True. The act phases
    queue every daemon surface stop on it rather than calling the daemon under
    ``sessions_lock``, and the recipes queue their ``gh`` calls (the gate
    audit comments and the reviewer request) the same way, whatever
    *dispatch_review_jobs* says. The drain runs those queued jobs first, then
    the review dispatch; a job whose decision is already persisted therefore
    still runs if a later in-lock step raises (that exception still
    propagates; per-job failures are isolated and logged or recorded, never
    raised from the ``finally``).

    Write-ordering: the phantom-reconcile path (phantom.py) writes the
    dev-queue first (task → PENDING) then sessions (session → COMPLETED),
    mirroring ``unblock_ticket`` (dev_queue.py) — the canonical safe-fail
    ordering.  A crash between the two writes leaves the session ACTIVE/IDLE
    so phantom detection re-fires on the next tick.  If a session reaches
    COMPLETED with its TicketTask still RUNNING (residual from an older crash
    or the dispatch-consumer path), ``revert_completed_silent_tasks()``
    recovers it within one reconcile tick.  See GitHub #867.

    Lockless pre-passes: before taking ``sessions_lock`` this function runs
    every ``gh``/``git`` call the in-lock sweeps would otherwise need -- PR
    merge state, the codex clean probes (#2563), the gate recipes'
    plan-of-record reads (#2545), the review recipes' repo slugs (#2564), the
    worktree dirty checks (#2548) and the local harvest's git facts (#2565).
    The in-lock code only reads their results, and a candidate with no usable
    result defers to the next tick. The harvest capture runs last: its facts
    expire ``HARVEST_FACTS_MAX_AGE_SECONDS`` after capture, so no other
    pre-pass may run between it and the lock. Stacked worst case before the
    lock is requested, all lockless: codex probes 60 s, repo slugs 30 s, dirty
    checks 30 s plus one in-flight check, and one 15 s roster call, harvest
    facts 60 s plus one in-flight candidate's four 10 s git calls; only the
    harvest pass's own span (at most 100 s) counts against its 180 s age
    limit. The dirty checks expire ``DIRTY_CHECK_MAX_AGE_SECONDS`` after
    capture; one aged out by a slow harvest pass or lock wait only defers.
    """
    # Pre-pass: check PR merge state for ACTIVE/IDLE DAEMON sessions before
    # acquiring sessions_lock. gh subprocess must NOT run under the lock
    # (liveness requirement, #485). Mirrors complete_timed_out_merged_tasks().
    pre_state = load_state()
    # Load clients once for branch-key resolution (feature_branch_prefix SSOT, #728).
    _clients = load_clients()
    # GitHub #975: loaded a second time this tick (also loaded later inside
    # _reconcile_locked) -- deliberate, to avoid threading a new parameter
    # through _reconcile_locked's signature/control-flow.
    _orchestrator_config = load_orchestrator_config()
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
        _branch = feature_branch_key(_session.client, _ticket_id, _clients)
        if _is_dangling_client(_session.client, _clients):
            # Client was configured but has since been removed/renamed --
            # route to gh_blocked (SESSION_NEEDS_ATTENTION downstream)
            # rather than risk an unscoped gh call (GitHub #1269).
            _gh_blocked_tids.append(_ticket_id)
            continue
        _cwd = _client_cwd(_session.client, _clients)
        _merged, _gh_avail = resolve_merged_via_pr_state(
            _ticket_id,
            _session.client,
            _task_by_ticket,
            max_age_seconds=_orchestrator_config.pr_hydration_interval_seconds,
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
    # Second lockless pre-pass (#2563): the codex orphan clean check's git
    # (`git status`, `git rev-parse`) must not run under sessions_lock either.
    # It must stay above the lock; the in-lock sweeps only read its probes.
    codex_probes = _capture_codex_clean_probes(
        config=_orchestrator_config, clients=_clients
    )
    # Third lockless pre-pass (#2545): the gate recipes' plan-of-record read
    # (`gh issue view`, and `git` for the `.cw/plan.md` fallback).
    plan_probes = _capture_plan_probes(config=_orchestrator_config, clients=_clients)
    # Fourth (#2564): the review recipes' repo-slug `git remote get-url`.
    repo_slugs = capture_review_repo_slugs(
        config=_orchestrator_config, dispatching=dispatch_review_jobs
    )
    # Fifth (#2548): the worktree dirty checks the phantom sweep and the
    # TIMED_OUT/COMPLETED backstops read in-lock.
    dirty_checks = _capture_dirty_checks(config=_orchestrator_config)
    # Sixth and LAST (#2565): the local harvest's git facts. Last because their
    # max age is measured from capture, so no other pre-pass may run between
    # it and the lock.
    harvest_facts = _capture_harvest_facts()

    jobs = DeferredReconcileJobs(
        review=DeferredReviewDispatch() if dispatch_review_jobs else None
    )
    try:
        # bounded=True (#2491): everything before this point is read-only, so a
        # timeout loses nothing. It lets `cw list`/`cw status`/`cw start` fail
        # with an actionable error behind a wedged serve instead of hanging, and
        # lets dispatch_tick skip the tick (_reconcile_usage_limited re-raises
        # it). A timeout means the lock was never acquired, so `jobs` is still
        # empty and the drain in the `finally` below is a no-op.
        with sessions_lock(bounded=True):
            locked_report = _reconcile_locked(
                merged_ticket_ids=merged_ticket_ids,
                gh_blocked_ticket_ids=gh_blocked_ticket_ids,
                clients=_clients,
                deferred=jobs,
                codex_probes=codex_probes,
                plan_probes=plan_probes,
                repo_slugs=repo_slugs,
                harvest_facts=harvest_facts,
                dirty_checks=dirty_checks,
            )
    finally:
        # Post-lock drain (#1232, #1229). Everything in `jobs` was decided and
        # persisted inside _reconcile_locked but must not run under
        # sessions_lock: a daemon surface stop is an external call (up to 10s
        # each), the gate audit comments and the reviewer request are gh
        # subprocess calls, and the address_review spawn calls
        # spawn_create_impl, which re-acquires sessions_lock(). They run here,
        # after the with-block above has released the lock. A ``finally``
        # because each decision is already stamped (session terminal, gate
        # released, one-shot latch burned): were a later in-lock step (another
        # recipe, run_escalation_sweep, save_state) to raise, the jobs would
        # otherwise be lost for the episode. Both drains isolate each job (log,
        # plus PR_ACTION_FAILED for review-recipe jobs, then the next job), so
        # neither raises for an ordinary failure and the drain cannot replace
        # an in-flight exception from the locked body. The post-lock jobs
        # (stops, gh calls) drain first, so a finished surface is torn down
        # before the review dispatch spawns any new worker; review dispatch
        # runs second from a nested ``finally``, because its latches are
        # already burned and its jobs must run even if the first drain is
        # interrupted, whereas a lost stop is recovered by the next tick's
        # leaked-worker sweep. Both run before the post-pass steps below,
        # which are not protected the same way.
        _drain_deferred_jobs(jobs)

    # Post-pass: runs AFTER sessions_lock releases so no gh subprocess
    # executes under the session lock (liveness — #485 SHOULD_FIX 4).
    completed_ticket_ids = complete_timed_out_merged_tasks()

    # Sited here (#2064), not in _run_terminal_backstops_and_sweeps:
    # dispatch_fix_agent's spawn_create_impl() call re-acquires sessions_lock(),
    # so it cannot run from inside _reconcile_locked's sessions_lock() hold
    # without a CwLockReentrancyError (#1228). Runs unconditionally (no
    # gate, by design, #2017) -- must sit BEFORE the completed_ticket_ids
    # early return below, not after.
    #
    # This hoist also moves run_fix_dispatch to AFTER run_escalation_sweep
    # (called inside _reconcile_locked above, line ~106) instead of before it
    # as in the old _run_terminal_backstops_and_sweeps ordering. Still safe,
    # but not because the two touch disjoint status sets. run_fix_dispatch's
    # ordinary path acts on RUNNING rows and transitions them RUNNING->PENDING
    # (fix_dispatch.py's module docstring: status stays RUNNING for the whole
    # handoff), and escalation eligibility requires
    # BLOCKED_ON_USER/AWAITING_OPERATOR_SIGNOFF/FAILED (escalation.py's
    # _is_escalation_eligible), so that path really is invisible to the sweep.
    # Its unresolvable-remote-ref park (#2209) is NOT: it moves a row
    # RUNNING->BLOCKED_ON_USER with an escalation-eligible disposition. The
    # reorder is safe anyway, because the only consequence is that such a row
    # is first seen by run_escalation_sweep on the NEXT tick -- and the sweep
    # pages nothing until ESCALATION_PARK_MINUTES (45) have elapsed since that
    # first sighting, so a one-tick delay is not observable.
    run_fix_dispatch(config=_orchestrator_config)

    if not completed_ticket_ids:
        return locked_report

    return ReconcileReport(
        phantom_session_ids=locked_report.phantom_session_ids,
        phantom_session_names=locked_report.phantom_session_names,
        reverted_ticket_ids=locked_report.reverted_ticket_ids,
        completed_ticket_ids=locked_report.completed_ticket_ids + completed_ticket_ids,
        usage_limited=locked_report.usage_limited,
    )


def _drain_deferred_jobs(jobs: DeferredReconcileJobs) -> None:
    """Run *jobs*' post-lock work: queued stops and gh calls, then review dispatch.

    Called from ``reconcile()``'s ``finally`` with no ``sessions_lock`` held
    (#1232). The review dispatch sits in a nested ``finally`` so it still runs
    when the post-lock job drain is interrupted by a ``BaseException`` (which
    then propagates). A separate function so ``reconcile()`` gains no branch.
    """
    try:
        run_post_lock_jobs(jobs)
    finally:
        if jobs.review is not None:
            dispatch_deferred_review_jobs(jobs.review)


def _reconcile_locked(
    *,
    merged_ticket_ids: frozenset[str] = frozenset(),
    gh_blocked_ticket_ids: frozenset[str] = frozenset(),
    clients: dict[str, ClientConfig] | None = None,
    deferred: DeferredReconcileJobs,
    codex_probes: CleanProbes | None = None,
    plan_probes: PlanProbes | None = None,
    repo_slugs: RepoSlugs | None = None,
    harvest_facts: HarvestFacts | None = None,
    dirty_checks: DirtyChecks | None = None,
) -> ReconcileReport:
    """Body of reconcile(), called while sessions_lock is held.

    Separated so reconcile() holds exactly one lock acquisition and the
    sweep helpers can save_state directly without re-acquiring the lock.

    *deferred* is the caller-owned post-lock job sink (#1232): the act phases
    (stalled, leaked-worker, idle, phantom) queue their daemon surface stops on
    it, and it is forwarded through ``_run_terminal_backstops_and_sweeps`` to
    the gate and review recipes, which append the jobs they prepare under the
    lock (gh calls on ``post_lock``, review dispatches on its review member,
    #1229). The caller drains it after releasing the lock. A ``None`` review
    member means this call may not dispatch, so the
    ``address_review``/``auto_fix_ci`` recipes are skipped. The daemon-outage
    early return never reaches the recipes, but the stalled and leaked-worker
    sweeps before it may already have queued stops.

    merged_ticket_ids / gh_blocked_ticket_ids come from a lockless pre-pass in
    reconcile() (GitHub #637); no gh subprocess executes under sessions_lock.
    They are consumed as-is by the phantom sweep below.
    clients comes from the same lockless pre-pass's `load_clients()` call
    (feature_branch_prefix SSOT, #728) — threaded through so the main-drift
    sweep (#940) doesn't re-read clients.yaml a second time this tick, and
    as the client scope of the codex live-writer re-park sweep (#2307).
    codex_probes comes from reconcile()'s lockless codex clean-probe pre-pass
    (#2563): the re-park sweep and the local harvest sweep's codex branch
    read their git checks from it, so neither runs git under sessions_lock.
    ``None`` (nothing captured) makes every such candidate defer a tick.
    plan_probes comes from reconcile()'s lockless plan prefetch pre-pass
    (#2545): the gate recipes read each plan-of-record body from it, so no
    gh/git runs for them under sessions_lock; ``None`` skips every plan
    candidate for the tick.
    repo_slugs comes from reconcile()'s lockless review repo-slug pre-pass
    (#2564) and is forwarded to the review recipes the same way.
    harvest_facts comes from reconcile()'s last lockless pre-pass (#2565): the
    local harvest builds each git-backed result from it, so no git runs for it
    under sessions_lock; ``None`` makes every git-backed candidate defer a tick.
    dirty_checks comes from reconcile()'s lockless dirty-check pre-pass
    (#2548): the phantom detect and the TIMED_OUT/COMPLETED backstops read
    each worktree dirty check from it; ``None`` makes every worktree session
    they would check defer a tick.

    Since the process-kill-timeout removal, no sweep in here dispositions a
    session off elapsed time or transcript quietness: the foreign-result and
    emitted-sentinel sweeps act only on positive completion evidence, the
    phantom sweep acts only on roster absence (the process is genuinely
    gone), the mid-turn usage-limit sweep acts only on a transcript tail that
    ends on a usage-limit message (#2324), and the liveness sweep is
    signal-only, as is the stranded-routed-result sweep (#2524), which pages
    once and never closes. The phantom and usage-limit sweeps' destructive acts are both
    gated by ``reap_policy`` (ADR-0006).
    """
    if clients is None:
        clients = load_clients()
    state = load_state()
    now = datetime.now(UTC)

    # Foreign-result sweep: completes headless DAEMON sessions whose
    # last_result already carries a terminal sentinel recorded by another
    # authority (#1470). Evidence-only; runs before the outage guard because
    # it does not depend on the daemon roster.
    orchestrator_config = load_orchestrator_config()
    # Load dev queue once here; pass to all sweeps to avoid duplicate
    # filesystem reads within the same reconcile tick. See GitHub issue #326.
    shared_tasks = load_dev_queue().tasks
    shared_task_by_ticket = {t.ticket_id: t for t in shared_tasks}
    stalled_candidates = _detect_stalled_candidates(
        state,
        task_by_ticket=shared_task_by_ticket,
    )
    _act_on_stalled_candidates(state, stalled_candidates, now=now, deferred=deferred)

    # Harvest fire-and-forget LOCAL aider sessions whose process has exited
    # (#888). Runs BEFORE the daemon query + outage guard: it depends only on
    # /proc liveness, not `claude agents --json`, so it must fire even when the
    # daemon roster is unavailable (a LOCAL session has no surface on the roster).
    local_harvest_candidates = _detect_local_harvest_candidates(state, shared_tasks)
    local_harvested = _act_on_local_harvest_candidates(
        state,
        local_harvest_candidates,
        now=now,
        task_by_ticket=shared_task_by_ticket,
        config=orchestrator_config,
        codex_probes=codex_probes,
        harvest_facts=harvest_facts,
    )

    # Main-checkout drift sweep (#925/#940): flag live worktree workers whose
    # main checkout is dirty or ahead/diverged — the isolation-breach signal.
    # A local git read (no daemon dependency), so it runs BEFORE the daemon
    # query + outage guard, mirroring the local-harvest sweep above. Advisory
    # only: emits SESSION_NEEDS_ATTENTION, mutates no session or queue state.
    main_drift_candidates = _detect_main_drift_candidates(state, clients)
    _act_on_main_drift_candidates(main_drift_candidates)

    # Leaked-daemon-worker sweep (#2480): stop every roster worker whose
    # surface_ref names a cw session already TERMINAL, or no cw session at
    # all -- a finished worker whose completion path never called
    # daemon.stop() otherwise sits live in roster.json forever, and
    # cw.worktree.live_home_reason reports its ticket's worktree occupied
    # indefinitely. Reads roster.json directly (NativeDaemonClient), not
    # `claude agents --json`, so it runs BEFORE the daemon query + outage
    # guard below, mirroring the local-harvest and main-drift sweeps above.
    # Unconditional (not reap_policy-gated, ADR-0006): the owning session, if
    # any, is already terminal, so there is no live queue/session state this
    # stop could clobber. Each stop is queued on `deferred` and runs after
    # sessions_lock releases (#1232); a worker the stalled sweep above already
    # queued a stop for is skipped.
    sweep_leaked_daemon_workers(
        state, daemon=_deps.get_native_daemon_client(), deferred=deferred
    )

    try:
        # `claude agents --json` returns sessionId as a full UUID
        # (e.g. "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"). cw's surface_ref
        # is the 8-char short id (prefix of the UUID) — same shape
        # `claude --bg` returns at spawn. Normalize to short id for
        # comparison; otherwise reconcile sees every native session as a
        # phantom because UUID != short-id.
        _agents = _claude_agents_json()
        native_live, surface_to_full = normalize_roster(_agents)
        daemon_errored = False
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        native_live = set()
        surface_to_full = {}
        daemon_errored = True
    if _looks_like_daemon_outage(state, daemon_errored, native_live):
        return ReconcileReport(
            completed_ticket_ids=list(dict.fromkeys(local_harvested)),
        )
    _backfill_claude_session_ids(state, surface_to_full)
    _verify_supervisor_session_id(state)
    # #1762: re-derive the operator advisory for RUNNING rows whose session_id
    # no longer resolves to a live session. Signal-only -- writes
    # TicketTask.advisory_note and nothing else; never dispositions a row.
    _stamp_session_id_mismatch_advisories(state, native_live, now=now)

    # Emitted-sentinel router (#578): routes sessions whose transcript already
    # carries a sentinel that signal_stop never routed. Evidence-only —
    # constructive completion, never a reap.
    idle_candidates = _detect_idle_candidates(
        state,
        now=now,
        native_live=native_live,
        config=orchestrator_config,
        task_by_ticket=shared_task_by_ticket,
    )
    _act_on_idle_candidates(state, idle_candidates, now=now, deferred=deferred)

    # Signal-only liveness-bucket sweep (RFC 0008 W2): latches
    # Session.liveness_bucket transitions, emits session.liveness_changed, and
    # emits the operator distress signal on a top-bucket crossing. No
    # disposition, no queue mutation. See GitHub #1001.
    record_session_liveness_changes(
        state,
        now=now,
        native_live=native_live,
        config=orchestrator_config,
        task_by_ticket=shared_task_by_ticket,
    )

    # Stranded routed-result sweep (#2524): a live session whose staged result
    # a #2458 partial route already routed, with no occupied row bound to it
    # and a stale transcript. After the outage guard (it needs the roster);
    # policy-independent and signal-only (ADR-0014) -- pages once via
    # reap_proposed_at and never closes; the operator does (cw doctor --reap).
    sweep_routed_result_sessions(
        state,
        now=now,
        native_live=native_live,
        config=orchestrator_config,
        tasks=shared_tasks,
        enabled_clients=clients.keys(),
    )

    # Mid-turn usage-limit sweep (#2324): a roster-present worker whose
    # transcript tail is a usage-limit message with no sentinel. Evidence-based,
    # reap_policy-gated like the phantom sweep; resumes any act already in
    # flight from its row's intent before deciding new ones. Returns only the
    # tickets reverted to PENDING under auto (a signal_only park is not a
    # revert). The phantom sweep and the completed-session backstop below
    # both skip a row still carrying an act.
    mid_turn_usage_limit_reverted = detect_and_park_mid_turn_usage_limits(
        state,
        now=now,
        native_live=native_live,
        config=orchestrator_config,
        clients=_deps.load_effective_clients(),
        tasks=shared_tasks,
    )

    drift = compute_drift(state, native_live, now=now)
    phantom_session_ids = _without_acts_in_flight(drift.phantom_session_ids)
    if not phantom_session_ids:
        # No phantom sessions to reap, but still run the TIMED_OUT,
        # COMPLETED-silent, and terminal-sibling sweeps so any tasks whose
        # sessions completed or timed out without reverting their queue task
        # are recovered, and stale PENDING rows with terminal siblings are parked.
        timed_out_ticket_ids, completed_silent_ticket_ids = (
            _run_terminal_backstops_and_sweeps(
                now=now,
                native_live=native_live,
                config=orchestrator_config,
                clients=clients,
                deferred=deferred,
                codex_probes=codex_probes,
                plan_probes=plan_probes,
                repo_slugs=repo_slugs,
                dirty_checks=dirty_checks,
            )
        )
        all_reverted = list(
            dict.fromkeys(
                mid_turn_usage_limit_reverted
                + timed_out_ticket_ids
                + completed_silent_ticket_ids
            )
        )
        return ReconcileReport(
            reverted_ticket_ids=all_reverted,
            completed_ticket_ids=list(dict.fromkeys(local_harvested)),
        )

    phantom_set = set(phantom_session_ids)
    phantom_candidates = _detect_phantom_candidates(
        state,
        phantom_set,
        task_by_ticket=shared_task_by_ticket,
        now=now,
        config=orchestrator_config,
        dirty_checks=dirty_checks,
    )
    _emit_reap_proposed(state, phantom_candidates, native_live=native_live, now=now)
    (
        reverted,
        phantom_names,
        phantom_usage_limited,
        _salvaged_phantom_ticket_ids,
        _salvaged_phantom_results,
        merged_from_phantom,
    ) = _act_on_phantom_candidates(
        state,
        phantom_candidates,
        now=now,
        config=orchestrator_config,
        merged_ticket_ids=merged_ticket_ids,
        gh_blocked_ticket_ids=gh_blocked_ticket_ids,
        deferred=deferred,
    )

    # Sweep for TIMED_OUT and DAEMON-COMPLETED sessions whose owning TicketTask
    # was not yet reverted (e.g. signal_stop crashed after setting status but
    # before touching the queue, or a headless session completed without
    # the dispatch consumer processing it). TIMED_OUT/COMPLETED sessions are
    # already terminal; the only state mutation for these sessions is the
    # reap_reason stamp inside revert_timed_out_tasks /
    # revert_completed_silent_tasks (in-place + save_state, serialized by
    # the sessions_lock this function runs under).
    timed_out_ticket_ids, completed_silent_ticket_ids = (
        _run_terminal_backstops_and_sweeps(
            now=now,
            native_live=native_live,
            config=orchestrator_config,
            clients=clients,
            deferred=deferred,
            codex_probes=codex_probes,
            plan_probes=plan_probes,
            repo_slugs=repo_slugs,
            dirty_checks=dirty_checks,
        )
    )
    all_reverted = list(
        dict.fromkeys(
            reverted
            + mid_turn_usage_limit_reverted
            + timed_out_ticket_ids
            + completed_silent_ticket_ids
        )
    )

    all_merged_completed = list(dict.fromkeys(merged_from_phantom + local_harvested))
    return ReconcileReport(
        phantom_session_ids=phantom_session_ids,
        phantom_session_names=phantom_names,
        reverted_ticket_ids=all_reverted,
        completed_ticket_ids=all_merged_completed,
        usage_limited=phantom_usage_limited,
    )
