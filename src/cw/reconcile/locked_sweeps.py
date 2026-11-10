"""In-lock sweep wiring for ``cw.reconcile.core._reconcile_locked``.

Everything here runs while ``reconcile()`` holds ``sessions_lock``, called
straight-line from ``_reconcile_locked`` in a fixed order:

- ``_run_pre_guard_sweeps`` — the roster-independent sweeps (foreign-result,
  local harvest, main drift, leaked daemon workers), before the
  daemon-outage guard (the roster is looked up first, from the lockless
  snapshot ``reconcile()`` captured, #2550).
- ``_run_post_guard_sweeps`` — after the outage guard: session-id backfill
  and supervisor check, the RUNNING-row advisories, the emitted-sentinel
  router, liveness, the shipped release and routed-result page, and the
  mid-turn usage-limit sweep.
- ``_run_terminal_backstops_and_sweeps`` — both terminal tails (with and
  without phantoms): the TicketTask backstops and the RFC 0008 capstone
  sweeps.

No ``gh``/``git`` runs here: every such read comes from a store captured
lockless by :mod:`cw.reconcile.prepasses`, and the acts queue their external
stops and ``gh`` calls on the post-lock sink (ADR-0019, #1232). The one
exception is the mid-turn usage-limit daemon stop, which runs under
``sessions_lock`` (``_stop_surface`` in
:mod:`cw.reconcile.usage_limit_mid_turn`; tracked in #2549). Split out of
``cw.reconcile.core`` (#2896).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.config import save_state
from cw.dev_queue import load_dev_queue
from cw.models import SessionOrigin
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _backfill_claude_session_ids,
    _stamp_session_id_mismatch_advisories,
)
from cw.reconcile.codex_reparks import run_codex_live_writer_reparks
from cw.reconcile.concierge import run_concierge_recoveries
from cw.reconcile.escalation import run_escalation_sweep
from cw.reconcile.gate_recipes import run_gate_recipes
from cw.reconcile.idle import _act_on_idle_candidates, _detect_idle_candidates
from cw.reconcile.leaked_workers import sweep_leaked_daemon_workers
from cw.reconcile.liveness import record_session_liveness_changes
from cw.reconcile.local import (
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
)
from cw.reconcile.lost_session_rows import run_lost_session_recovery
from cw.reconcile.main_drift import (
    _act_on_main_drift_candidates,
    _detect_main_drift_candidates,
)
from cw.reconcile.review_recipes import run_review_recipes
from cw.reconcile.routed_result_sessions import sweep_routed_result_sessions
from cw.reconcile.shipped_release import run_shipped_release
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
)
from cw.reconcile.tasks import (
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
    from datetime import datetime

    from cw.models import ClientConfig, CwState, OrchestratorConfig, TicketTask
    from cw.reconcile._shared import ScopeProbes
    from cw.reconcile.cancelled_row_probes import CancelledRowProbes
    from cw.reconcile.codex_boot import CleanProbes
    from cw.reconcile.deferred import DeferredReconcileJobs
    from cw.reconcile.dirty_checks import DirtyChecks
    from cw.reconcile.gate_plan_probes import PlanProbes
    from cw.reconcile.harvest_synthesis import HarvestFacts
    from cw.reconcile.main_drift import MainDriftProbes
    from cw.reconcile.review_recipes._shared import RepoSlugs
    from cw.reconcile.shipped_release import ShippedPrProbes

_log = logging.getLogger(__name__)


def _run_pre_guard_sweeps(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    clients: dict[str, ClientConfig],
    tasks: list[TicketTask],
    task_by_ticket: dict[str, TicketTask],
    deferred: DeferredReconcileJobs,
    codex_probes: CleanProbes | None,
    harvest_facts: HarvestFacts | None,
    drift_probes: MainDriftProbes | None,
) -> list[str]:
    """Run the sweeps that do not depend on the daemon roster.

    Called from ``_reconcile_locked`` after the roster snapshot lookup
    (#2550) and before the daemon-outage guard, so each of these fires even
    when the roster is unavailable. Returns the ticket ids the local harvest
    completed.
    """
    # Foreign-result sweep: completes headless DAEMON sessions whose
    # last_result already carries a terminal sentinel recorded by another
    # authority (#1470). Evidence-only; runs before the outage guard because
    # it does not depend on the daemon roster.
    stalled_candidates = _detect_stalled_candidates(
        state,
        task_by_ticket=task_by_ticket,
    )
    _act_on_stalled_candidates(state, stalled_candidates, now=now, deferred=deferred)

    # Harvest fire-and-forget LOCAL aider sessions whose process has exited
    # (#888). Runs BEFORE the daemon query + outage guard: it depends only on
    # /proc liveness, not `claude agents --json`, so it must fire even when the
    # daemon roster is unavailable (a LOCAL session has no surface on the roster).
    local_harvest_candidates = _detect_local_harvest_candidates(state, tasks)
    local_harvested = _act_on_local_harvest_candidates(
        state,
        local_harvest_candidates,
        now=now,
        task_by_ticket=task_by_ticket,
        config=config,
        codex_probes=codex_probes,
        harvest_facts=harvest_facts,
    )

    # Main-checkout drift sweep (#925/#940): flag live worktree workers whose
    # main checkout is dirty or ahead/diverged — the isolation-breach signal.
    # Its git is prefetched lockless (#2546); with no daemon dependency it runs
    # BEFORE the daemon query + outage guard, like the local-harvest sweep.
    # Advisory: emits SESSION_NEEDS_ATTENTION, mutates no session or queue state.
    main_drift_candidates = _detect_main_drift_candidates(state, clients, drift_probes)
    _act_on_main_drift_candidates(main_drift_candidates)

    # Leaked-daemon-worker sweep (#2480, #2670): stop every roster worker whose
    # surface_ref names a cw session already TERMINAL, or that no cw session
    # names and that is evidenced orphaned (older than the spawn-to-record
    # grace and homed in a configured client's cw worktree base). A finished
    # worker whose completion path never called daemon.stop() otherwise sits
    # live in roster.json forever, and cw.worktree.live_home_reason reports its
    # ticket's worktree occupied indefinitely. Reads roster.json directly
    # (NativeDaemonClient), not `claude agents --json`, so it runs BEFORE the
    # daemon query + outage guard below, mirroring the local-harvest and
    # main-drift sweeps above. Unconditional (not reap_policy-gated, ADR-0006):
    # the terminal arm and the evidenced absent arm touch no live queue/session
    # state. Each stop is queued on `deferred` and runs after sessions_lock
    # releases (#1232); a worker the stalled sweep above already queued a stop
    # for is skipped. `now` and `clients` are the same values doctor's reap
    # passes, so the two callers cannot disagree about what is leaked.
    sweep_leaked_daemon_workers(
        state,
        daemon=_deps.get_native_daemon_client(),
        now=now,
        clients=clients,
        deferred=deferred,
    )
    return local_harvested


def _run_post_guard_sweeps(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    surface_to_full: dict[str, str],
    config: OrchestratorConfig,
    clients: dict[str, ClientConfig],
    tasks: list[TicketTask],
    task_by_ticket: dict[str, TicketTask],
    deferred: DeferredReconcileJobs,
    scope_probes: ScopeProbes | None,
    shipped_probes: ShippedPrProbes | None,
) -> list[str]:
    """Run the roster-dependent sweeps that precede the phantom detect.

    Called from ``_reconcile_locked`` after the daemon-outage guard, so
    *native_live* / *surface_to_full* come from a usable roster. Returns the
    tickets the mid-turn usage-limit sweep reverted to PENDING.
    """
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
        config=config,
        task_by_ticket=task_by_ticket,
        scope_probes=scope_probes,
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
        config=config,
        task_by_ticket=task_by_ticket,
    )

    # Stranded routed-result sessions (#2524). First the shipped release
    # (#2715) closes those whose emitted shipped result has its own OPEN or
    # MERGED PR and a quiesced drain stamp (cw.reconcile.shipped_release);
    # then the page sweep pages every remaining one once and never closes.
    run_shipped_release(
        state,
        now=now,
        native_live=native_live,
        clients=clients,
        config=config,
        tasks=tasks,
        probes=shipped_probes,
        deferred=deferred,
    )
    sweep_routed_result_sessions(
        state,
        now=now,
        native_live=native_live,
        config=config,
        tasks=tasks,
        enabled_clients=clients.keys(),
    )

    # Mid-turn usage-limit sweep (#2324): a roster-present worker whose
    # transcript tail is a usage-limit message with no sentinel. Evidence-based,
    # reap_policy-gated like the phantom sweep; resumes any act already in
    # flight from its row's intent before deciding new ones. Returns only the
    # tickets reverted to PENDING under auto (a signal_only park is not a
    # revert). The phantom sweep and the completed-session backstop below
    # both skip a row still carrying an act.
    return detect_and_park_mid_turn_usage_limits(
        state,
        now=now,
        native_live=native_live,
        config=config,
        clients=_deps.load_effective_clients(),
        tasks=tasks,
    )


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
    cancelled_probes: CancelledRowProbes | None = None,
    scope_probes: ScopeProbes | None = None,
    roster_captured_at: datetime | None = None,
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
    defers a tick. Concierge recipe 3 reads *cancelled_probes* (#2546), and
    recipe 2's salvage *scope_probes* (#2641). Concierge recipes 1 and 2 also
    take *roster_captured_at*, the capture time of the lockless roster
    *native_live* came from (#2550): a session spawned or resumed too recently
    for that capture to list it is not called dead.
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
    # #2715: park record-missing RUNNING rows; page sessionless ones.
    run_lost_session_recovery(now=now, clients=clients, native_live=native_live)
    run_concierge_recoveries(
        now=now,
        native_live=native_live,
        config=config,
        cancelled_probes=cancelled_probes,
        scope_probes=scope_probes,
        roster_captured_at=roster_captured_at,
    )
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
