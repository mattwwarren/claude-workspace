"""Top-level reconcile orchestration.

``reconcile`` runs the lockless pre-passes (``cw.reconcile.prepasses``) -- gh
merge state, the codex clean probes (#2563), the gate recipes'
plan-of-record prefetch (#2545), the review recipes' repo slugs (#2564), the
worktree dirty checks (#2548), the local harvest's git facts (#2565) and,
last, the daemon-roster snapshot refresh (#2550,
``cw.reconcile.roster_snapshot``) -- then ``_reconcile_locked`` under
``sessions_lock`` (the detect/emit/act sweeps for stalled, idle, and phantom
sessions, wired in ``cw.reconcile.locked_sweeps``), then the post-lock gh/git
passes. See the package ``__init__`` docstring and ADR-0005/ADR-0006/ADR-0019
for the invariants. This module keeps the single ``sessions_lock``
acquisition, the roster snapshot and its in-lock lookup and outage guard, the
phantom tail and the post-lock drain; the rest was split out (#2896).

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

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import (
    load_clients,
    load_orchestrator_config,
    load_state,
    sessions_lock,
)
from cw.dev_queue import load_dev_queue
from cw.reconcile._shared import (
    ReconcileReport,
    _claude_agents_json,
    _emit_reap_proposed,
    _looks_like_daemon_outage,
    compute_drift,
)
from cw.reconcile.deferred import DeferredReconcileJobs, run_post_lock_jobs
from cw.reconcile.dirty_checks import normalize_roster
from cw.reconcile.fix_dispatch import run_fix_dispatch
from cw.reconcile.locked_sweeps import (
    _run_post_guard_sweeps,
    _run_pre_guard_sweeps,
    _run_terminal_backstops_and_sweeps,
    _without_acts_in_flight,
)
from cw.reconcile.phantom import (
    _act_on_phantom_candidates,
    _detect_phantom_candidates,
)
from cw.reconcile.prepasses import (
    _capture_closing_prepasses,
    _capture_codex_clean_probes,
    _capture_dirty_checks,
    _capture_merge_state,
    _capture_plan_probes,
)
from cw.reconcile.review_recipes import (
    DeferredReviewDispatch,
    dispatch_deferred_review_jobs,
)
from cw.reconcile.review_recipes.core import capture_review_repo_slugs
from cw.reconcile.roster_snapshot import (
    RosterSnapshot,
    RosterUnavailableError,
    lookup_roster,
)
from cw.reconcile.tasks import complete_timed_out_merged_tasks

if TYPE_CHECKING:
    from cw.models import ClientConfig
    from cw.reconcile._shared import ScopeProbes
    from cw.reconcile.cancelled_row_probes import CancelledRowProbes
    from cw.reconcile.codex_boot import CleanProbes
    from cw.reconcile.dirty_checks import DirtyChecks
    from cw.reconcile.gate_plan_probes import PlanProbes
    from cw.reconcile.harvest_synthesis import HarvestFacts
    from cw.reconcile.main_drift import MainDriftProbes
    from cw.reconcile.review_recipes._shared import RepoSlugs
    from cw.reconcile.shipped_release import ShippedPrProbes


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
    Every other caller — ``cw start`` and ``cw doctor --reap`` — is a
    housekeeping command that must not, as a side effect, spawn a headless
    ``/address-review`` worker that pushes to a PR branch or burn the recipe's
    one-shot latch. With the default False those two acts are not run at all
    (no latch stamp, no ``PR_ACTION_TAKEN``); ``request_reviewer`` and
    ``escalate_merge_block`` run regardless. ``cw status`` and ``cw list`` do
    not call ``reconcile()`` at all (#2938): they are pure reads.

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
    worktree dirty checks (#2548), the shipped-release PR lookups (#2715), the
    main-drift checks and concierge recipe 3's git read (#2546), the local
    harvest's git facts (#2565) and the daemon roster (#2550).
    The in-lock code only reads their results, and a candidate with no usable
    result defers to the next tick. The harvest capture is the last git/gh
    pre-pass: its facts expire ``HARVEST_FACTS_MAX_AGE_SECONDS`` after
    capture, so only the roster refresh follows it. Stacked worst case before
    the lock is requested, all lockless: codex probes 60 s, repo slugs 30 s,
    dirty checks 30 s plus one in-flight check, and one 10 s roster call,
    shipped PRs 20 s plus one in-flight 15 s lookup, main-drift and
    cancelled-row probes 20 s each as a budget checked between items plus the
    in-flight git (``_run_git`` passes no timeout, so not a hard cap), harvest
    facts 60 s plus one in-flight candidate's four 10 s git calls, then a
    second 10 s roster refresh when the first read is older than
    ``ROSTER_REUSE_MAX_AGE_SECONDS``; the harvest pass's own span (at most
    100 s) plus that final refresh (at most 10 s) counts against its 180 s
    age limit. The dirty checks expire ``DIRTY_CHECK_MAX_AGE_SECONDS`` after
    capture, and that 90 s budget can already be exceeded in this stacked
    worst case (a stale dirty check only defers, never authorizes); the extra
    roster refresh only widens a case that defers. The drift and
    cancelled-row probes expire 180 s after capture (so a slow harvest pass
    cannot age them out); one aged out by a lock wait only defers.

    Roster snapshot (#2550): ``claude agents --json`` runs only before the
    lock, into one :class:`~cw.reconcile.roster_snapshot.RosterSnapshot`
    shared by the dirty-check pre-pass and the final refresh. The in-lock
    lookup fails closed: a snapshot at least ``ROSTER_SNAPSHOT_MAX_AGE_SECONDS``
    (20 s) old, or none, skips the roster-dependent sweeps for the tick, and a
    failed read is treated as a daemon outage, as before.
    """
    # Pre-pass: check PR merge state for ACTIVE/IDLE DAEMON sessions before
    # acquiring sessions_lock. gh subprocess must NOT run under the lock
    # (liveness requirement, #485). Mirrors complete_timed_out_merged_tasks().
    pre_state = load_state()
    # Load clients once for branch resolution (resolve_feature_branch, #728).
    _clients = load_clients()
    # GitHub #975: loaded a second time this tick (also loaded later inside
    # _reconcile_locked) -- deliberate, to avoid threading a new parameter
    # through _reconcile_locked's signature/control-flow.
    _orchestrator_config = load_orchestrator_config()
    merged_ticket_ids, gh_blocked_ticket_ids = _capture_merge_state(
        pre_state, clients=_clients, config=_orchestrator_config
    )
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
    # TIMED_OUT/COMPLETED backstops read in-lock. Its roster read goes
    # through the tick's roster snapshot (#2550), so the refresh below can
    # reuse it.
    roster = RosterSnapshot()
    dirty_checks = _capture_dirty_checks(
        config=_orchestrator_config, roster=roster.shared(_claude_agents_json)
    )
    # Sixth (#2715), the shipped-release `gh pr list`, then the main-drift and
    # recipe-3 git (#2546), then the salvaged-sentinel scope git (#2641), then
    # (#2565) the local harvest's git facts, whose max age is measured from
    # capture.
    shipped_probes, drift_probes, cancelled_probes, scope_probes, harvest_facts = (
        _capture_closing_prepasses(config=_orchestrator_config, clients=_clients)
    )
    # LAST (#2550): `claude agents --json`, lockless, so the roster is young
    # when the lock is taken; re-reads only if the dirty-check read is not a
    # success younger than ROSTER_REUSE_MAX_AGE_SECONDS.
    roster.refresh(_claude_agents_json)

    jobs = DeferredReconcileJobs(
        review=DeferredReviewDispatch() if dispatch_review_jobs else None
    )
    interrupted = False
    try:
        # bounded=True (#2491): everything before this point is read-only, so a
        # timeout loses nothing. It lets `cw start`/`cw doctor --reap` fail
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
                shipped_probes=shipped_probes,
                drift_probes=drift_probes,
                cancelled_probes=cancelled_probes,
                scope_probes=scope_probes,
                roster_snapshot=roster,
            )
    except (KeyboardInterrupt, SystemExit):
        interrupted = True
        raise
    finally:
        # Post-lock drain (#1232, #1229). Everything in `jobs` was decided
        # inside _reconcile_locked but must not run under sessions_lock: a
        # daemon surface stop is an external call (up to 10s each), the gate
        # audit comments and the reviewer request are gh subprocess calls, and
        # the address_review spawn calls spawn_create_impl, which re-acquires
        # sessions_lock(). They run here, after the with-block above has
        # released the lock. A ``finally`` because the post-lock jobs' decisions
        # are already persisted (session terminal, gate released): were a later
        # in-lock step (another recipe, run_escalation_sweep, save_state) to
        # raise an ordinary exception, they would otherwise be lost. Both
        # drains isolate each job (log, plus PR_ACTION_FAILED for review-recipe
        # jobs, then the next job), so neither raises for an ordinary failure
        # and the drain cannot replace an in-flight exception from the locked
        # body. The post-lock jobs (stops, gh calls) drain first, so a finished
        # surface is torn down before the review dispatch spawns any new
        # worker. The stop part of that drain is capped per tick
        # (MAX_STOP_JOBS_PER_TICK, #2551): a surplus recoverable stop is dropped
        # for the sweep to re-find, a surplus CARRIED stop runs first next
        # drain, and non-stop jobs are never capped.
        #
        # Shutdown rule (#2507): a KeyboardInterrupt or SystemExit in the
        # locked body or the post-lock drain skips the review dispatch, so no
        # push-capable worker is spawned while the loop goes down. Nothing is
        # lost: the review jobs' one-shot latches commit only after their
        # dispatch outcome, so the next start re-fires them. The post-lock
        # drain itself still runs on an interrupt, because its work was
        # committed under the lock. Both run before the post-pass steps below,
        # which are not protected the same way.
        _drain_deferred_jobs(jobs, interrupted=interrupted)

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
        reverted_ticket_ids=locked_report.reverted_ticket_ids,
        completed_ticket_ids=locked_report.completed_ticket_ids + completed_ticket_ids,
        usage_limited=locked_report.usage_limited,
    )


def _drain_deferred_jobs(
    jobs: DeferredReconcileJobs, *, interrupted: bool = False
) -> None:
    """Run *jobs*' post-lock work: queued stops and gh calls, then review dispatch.

    The stop jobs are capped per tick (``MAX_STOP_JOBS_PER_TICK``, #2551);
    gh calls and the review dispatch are never capped.

    Called from ``reconcile()``'s ``finally`` with no ``sessions_lock`` held
    (#1232). The review dispatch sits in a nested ``finally`` so it still runs
    when the post-lock job drain raises an ordinary exception. *interrupted*
    is True when ``reconcile()``'s locked body was interrupted by a
    ``KeyboardInterrupt`` or ``SystemExit``; an interrupt out of the post-lock
    drain sets it too. Either way the review dispatch is skipped (the #2507
    shutdown rule): its latches are still armed, so the next start re-fires
    the jobs. A separate function so ``reconcile()`` gains no branch.
    """
    try:
        run_post_lock_jobs(jobs)
    except (KeyboardInterrupt, SystemExit):
        interrupted = True
        raise
    finally:
        if jobs.review is not None and not interrupted:
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
    shipped_probes: ShippedPrProbes | None = None,
    drift_probes: MainDriftProbes | None = None,
    cancelled_probes: CancelledRowProbes | None = None,
    scope_probes: ScopeProbes | None = None,
    roster_snapshot: RosterSnapshot | None = None,
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
    (resolve_feature_branch, #728) — threaded through so the main-drift
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
    drift_probes / cancelled_probes come from reconcile()'s lockless pre-pass
    (#2546); ``None`` makes every such client or CANCELLED row defer a tick.
    scope_probes comes from reconcile()'s lockless salvage-scope pre-pass
    (#2641): the idle, phantom and concierge sentinel parses correct a
    salvaged scope from it, never git; ``None`` leaves every scope as reported.
    roster_snapshot comes from reconcile()'s lockless roster capture (#2550):
    it is looked up first (``lookup_roster``, never a subprocess), so the
    in-lock sweeps' run time does not age it. A failed read (error, timeout,
    unparseable) is an empty roster with ``daemon_errored`` set, fed to the
    unchanged outage guard. A stale or missing snapshot (``None`` is missing)
    returns right after the roster-independent pre-guard sweeps, so no
    roster-dependent sweep runs on a fabricated empty roster. Its capture
    time is passed to ``compute_drift`` and the concierge recipes, which spare
    a session spawned or resumed too recently for the capture to list it.

    Since the process-kill-timeout removal, no sweep in here dispositions a
    session off elapsed time or transcript quietness: the foreign-result and
    emitted-sentinel sweeps act only on positive completion evidence, the
    phantom sweep acts only on roster absence (the process is genuinely
    gone), the mid-turn usage-limit sweep acts only on a transcript tail that
    ends on a usage-limit message (#2324), and the liveness sweep is
    signal-only, as is the #2524 page; the shipped release before it closes on
    a recorded shipped result with its own OPEN/MERGED PR only (#2715;
    ``shipped_probes=None`` makes it a no-op). The phantom and usage-limit
    sweeps' destructive acts are both gated by ``reap_policy`` (ADR-0006).
    """
    if clients is None:
        clients = load_clients()
    state = load_state()
    now = datetime.now(UTC)

    orchestrator_config = load_orchestrator_config()
    # Load dev queue once here; pass to all sweeps to avoid duplicate
    # filesystem reads within the same reconcile tick. See GitHub issue #326.
    shared_tasks = load_dev_queue().tasks
    shared_task_by_ticket = {t.ticket_id: t for t in shared_tasks}
    # The roster lookup runs first (#2550, M3): it is pure, and doing it
    # before the sweeps keeps their run time from aging the snapshot. Only the
    # early-return decision waits for the pre-guard sweeps. `claude agents
    # --json` returns sessionId as a full UUID; normalize_roster keys it by
    # the 8-char short id cw stores as surface_ref.
    unavailable: RosterUnavailableError | None = None
    try:
        native_live, surface_to_full = normalize_roster(lookup_roster(roster_snapshot))
    except RosterUnavailableError as exc:
        native_live, surface_to_full = set(), {}
        unavailable = exc
    # The roster-independent sweeps (foreign-result, local harvest, main
    # drift, leaked workers) run BEFORE the outage guard below.
    local_harvested = _run_pre_guard_sweeps(
        state,
        now=now,
        config=orchestrator_config,
        clients=clients,
        tasks=shared_tasks,
        task_by_ticket=shared_task_by_ticket,
        deferred=deferred,
        codex_probes=codex_probes,
        harvest_facts=harvest_facts,
        drift_probes=drift_probes,
    )

    roster_captured_at = (
        roster_snapshot.captured_at if roster_snapshot is not None else None
    )

    # A stale or missing snapshot skips every roster-dependent sweep (M2); a
    # failed read keeps the outage heuristic with daemon_errored=True.
    if (unavailable is not None and unavailable.skips_tick) or (
        _looks_like_daemon_outage(state, unavailable is not None, native_live)
    ):
        return ReconcileReport(
            completed_ticket_ids=list(dict.fromkeys(local_harvested)),
        )
    mid_turn_usage_limit_reverted = _run_post_guard_sweeps(
        state,
        now=now,
        native_live=native_live,
        surface_to_full=surface_to_full,
        config=orchestrator_config,
        clients=clients,
        tasks=shared_tasks,
        task_by_ticket=shared_task_by_ticket,
        deferred=deferred,
        scope_probes=scope_probes,
        shipped_probes=shipped_probes,
    )

    drift = compute_drift(
        state, native_live, now=now, roster_captured_at=roster_captured_at
    )
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
                cancelled_probes=cancelled_probes,
                scope_probes=scope_probes,
                roster_captured_at=roster_captured_at,
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
        scope_probes=scope_probes,
    )
    _emit_reap_proposed(state, phantom_candidates, native_live=native_live, now=now)
    (
        reverted,
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
            cancelled_probes=cancelled_probes,
            scope_probes=scope_probes,
            roster_captured_at=roster_captured_at,
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
        reverted_ticket_ids=all_reverted,
        completed_ticket_ids=all_merged_completed,
        usage_limited=phantom_usage_limited,
    )
