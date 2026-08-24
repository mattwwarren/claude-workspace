"""Top-level reconcile orchestration.

``reconcile`` runs the lockless gh pre-pass, then ``_reconcile_locked`` under
``sessions_lock`` (the detect/emit/act sweeps for stalled, idle, and phantom
sessions), then the post-lock gh/git passes. See the package ``__init__``
docstring and ADR-0005/ADR-0006 for the invariants.
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
    compute_drift,
    feature_branch_key,
    ticket_id_for_session,
)
from cw.reconcile.concierge import run_concierge_recoveries
from cw.reconcile.escalation import run_escalation_sweep
from cw.reconcile.gate_recipes import run_gate_recipes
from cw.reconcile.idle import _act_on_idle_candidates, _detect_idle_candidates
from cw.reconcile.liveness import record_session_liveness_changes
from cw.reconcile.local import (
    _act_on_local_harvest_candidates,
    _detect_local_harvest_candidates,
)
from cw.reconcile.main_drift import (
    _act_on_main_drift_candidates,
    _detect_main_drift_candidates,
)
from cw.reconcile.phantom import (
    _act_on_phantom_candidates,
    _detect_phantom_candidates,
)
from cw.reconcile.review_recipes import run_review_recipes
from cw.reconcile.stalled import (
    _act_on_stalled_candidates,
    _detect_stalled_candidates,
)
from cw.reconcile.tasks import (
    _client_cwd,
    _is_dangling_client,
    complete_timed_out_merged_tasks,
    park_terminal_sibling_tasks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, CwState, OrchestratorConfig, TicketTask

_log = logging.getLogger(__name__)


def _run_terminal_backstops_and_sweeps(
    *,
    now: datetime,
    native_live: set[str],
    config: OrchestratorConfig,
) -> tuple[list[str], list[str]]:
    """Run the post-detect TicketTask backstops + RFC 0008 capstone sweeps.

    Both branches of ``_reconcile_locked`` (the no-phantoms early return and
    the normal phantom-handling tail) call this in the same spot: after their
    own detect/act sweep, recover any RUNNING task whose session already
    went TIMED_OUT/COMPLETED without reverting it, park stale PENDING rows
    with a terminal sibling (#876), then run the mechanical recovery reactor
    (opt-in) and durable escalation sweep (unconditional) from GitHub #1015.
    All of these load their own fresh dev-queue/state snapshots rather than
    reusing the (possibly now-stale) locals in ``_reconcile_locked``.
    Extracted to one call site (instead of duplicating 4 lines in each
    branch) to keep ``_reconcile_locked``'s statement count under the
    PLR0915 limit.

    Returns (timed_out_ticket_ids, completed_silent_ticket_ids).
    """
    timed_out_ticket_ids = revert_timed_out_tasks()
    completed_silent_ticket_ids = revert_completed_silent_tasks()
    park_terminal_sibling_tasks()
    run_concierge_recoveries(now=now, native_live=native_live, config=config)
    run_gate_recipes(now=now, config=config)
    run_review_recipes(config=config)
    run_escalation_sweep(now=now)
    return timed_out_ticket_ids, completed_silent_ticket_ids


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


def reconcile() -> ReconcileReport:
    """Apply drift reconciliation against the persisted state.

    Flips phantom ACTIVE/IDLE sessions to COMPLETED with
    ``completed_reason = CRASHED``, emits a ``SESSION_COMPLETED`` event
    with ``crashed: True``, and reverts any RUNNING TicketTask whose
    ticket-id can be recovered from the session name back to PENDING so
    the dispatch loop will retry.

    Returns an empty report without mutating state when
    :func:`_looks_like_daemon_outage` matches — a transient daemon hiccup
    must not trigger mass-reaping.

    Write-ordering: the phantom-reconcile path (phantom.py) writes the
    dev-queue first (task → PENDING) then sessions (session → COMPLETED),
    mirroring ``unblock_ticket`` (dev_queue.py) — the canonical safe-fail
    ordering.  A crash between the two writes leaves the session ACTIVE/IDLE
    so phantom detection re-fires on the next tick.  If a session reaches
    COMPLETED with its TicketTask still RUNNING (residual from an older crash
    or the dispatch-consumer path), ``revert_completed_silent_tasks()``
    recovers it within one reconcile tick.  See GitHub #867.
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

    with sessions_lock():
        locked_report = _reconcile_locked(
            merged_ticket_ids=merged_ticket_ids,
            gh_blocked_ticket_ids=gh_blocked_ticket_ids,
            clients=_clients,
        )

    # Post-pass: runs AFTER sessions_lock releases so no gh subprocess
    # executes under the session lock (liveness — #485 SHOULD_FIX 4).
    completed_ticket_ids = complete_timed_out_merged_tasks()

    if not completed_ticket_ids:
        return locked_report

    return ReconcileReport(
        phantom_session_ids=locked_report.phantom_session_ids,
        phantom_session_names=locked_report.phantom_session_names,
        reverted_ticket_ids=locked_report.reverted_ticket_ids,
        completed_ticket_ids=locked_report.completed_ticket_ids + completed_ticket_ids,
        usage_limited=locked_report.usage_limited,
    )


def _reconcile_locked(
    *,
    merged_ticket_ids: frozenset[str] = frozenset(),
    gh_blocked_ticket_ids: frozenset[str] = frozenset(),
    clients: dict[str, ClientConfig] | None = None,
) -> ReconcileReport:
    """Body of reconcile(), called while sessions_lock is held.

    Separated so reconcile() holds exactly one lock acquisition and the
    sweep helpers can save_state directly without re-acquiring the lock.

    merged_ticket_ids / gh_blocked_ticket_ids come from a lockless pre-pass in
    reconcile() (GitHub #637); no gh subprocess executes under sessions_lock.
    They are consumed as-is by the phantom sweep below.
    clients comes from the same lockless pre-pass's `load_clients()` call
    (feature_branch_prefix SSOT, #728) — threaded through so the main-drift
    sweep (#940) doesn't re-read clients.yaml a second time this tick.

    Since the process-kill-timeout removal, no sweep in here dispositions a
    session off elapsed time or transcript quietness: the foreign-result and
    emitted-sentinel sweeps act only on positive completion evidence, the
    phantom sweep acts only on roster absence (the process is genuinely
    gone), and the liveness sweep is signal-only.
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
    shared_task_by_ticket = {t.ticket_id: t for t in load_dev_queue().tasks}
    stalled_candidates = _detect_stalled_candidates(
        state,
        task_by_ticket=shared_task_by_ticket,
    )
    _act_on_stalled_candidates(state, stalled_candidates, now=now)

    # Harvest fire-and-forget LOCAL aider sessions whose process has exited
    # (#888). Runs BEFORE the daemon query + outage guard: it depends only on
    # /proc liveness, not `claude agents --json`, so it must fire even when the
    # daemon roster is unavailable (a LOCAL session has no surface on the roster).
    local_harvest_candidates = _detect_local_harvest_candidates(
        state, task_by_ticket=shared_task_by_ticket
    )
    local_harvested = _act_on_local_harvest_candidates(
        state,
        local_harvest_candidates,
        now=now,
        task_by_ticket=shared_task_by_ticket,
    )

    # Main-checkout drift sweep (#925/#940): flag live worktree workers whose
    # main checkout is dirty or ahead/diverged — the isolation-breach signal.
    # A local git read (no daemon dependency), so it runs BEFORE the daemon
    # query + outage guard, mirroring the local-harvest sweep above. Advisory
    # only: emits SESSION_NEEDS_ATTENTION, mutates no session or queue state.
    main_drift_candidates = _detect_main_drift_candidates(state, clients)
    _act_on_main_drift_candidates(main_drift_candidates)

    try:
        # `claude agents --json` returns sessionId as a full UUID
        # (e.g. "04bf1c48-6b3a-401b-bc3a-0d61b5b7a6ac"). cw's surface_ref
        # is the 8-char short id (prefix of the UUID) — same shape
        # `claude --bg` returns at spawn. Normalize to short id for
        # comparison; otherwise reconcile sees every native session as a
        # phantom because UUID != short-id.
        _agents = _claude_agents_json()
        native_live = {
            sid[:8] for a in _agents if isinstance(sid := a.get("sessionId"), str)
        }
        surface_to_full = {
            sid[:8]: sid for a in _agents if isinstance(sid := a.get("sessionId"), str)
        }
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
    _act_on_idle_candidates(state, idle_candidates, now=now)

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

    drift = compute_drift(state, native_live, now=now)
    if not drift.phantom_session_ids:
        # No phantom sessions to reap, but still run the TIMED_OUT,
        # COMPLETED-silent, and terminal-sibling sweeps so any tasks whose
        # sessions completed or timed out without reverting their queue task
        # are recovered, and stale PENDING rows with terminal siblings are parked.
        timed_out_ticket_ids, completed_silent_ticket_ids = (
            _run_terminal_backstops_and_sweeps(
                now=now, native_live=native_live, config=orchestrator_config
            )
        )
        all_reverted = list(
            dict.fromkeys(timed_out_ticket_ids + completed_silent_ticket_ids)
        )
        return ReconcileReport(
            reverted_ticket_ids=all_reverted,
            completed_ticket_ids=list(dict.fromkeys(local_harvested)),
        )

    phantom_set = set(drift.phantom_session_ids)
    phantom_candidates = _detect_phantom_candidates(
        state,
        phantom_set,
        task_by_ticket=shared_task_by_ticket,
        now=now,
        config=orchestrator_config,
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
            now=now, native_live=native_live, config=orchestrator_config
        )
    )
    all_reverted = list(
        dict.fromkeys(reverted + timed_out_ticket_ids + completed_silent_ticket_ids)
    )

    all_merged_completed = list(dict.fromkeys(merged_from_phantom + local_harvested))
    return ReconcileReport(
        phantom_session_ids=drift.phantom_session_ids,
        phantom_session_names=phantom_names,
        reverted_ticket_ids=all_reverted,
        completed_ticket_ids=all_merged_completed,
        usage_limited=phantom_usage_limited,
    )
