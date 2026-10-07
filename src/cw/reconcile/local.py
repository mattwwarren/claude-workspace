"""Harvest path for fire-and-forget LocalExecutor aider sessions (RFC 0005 F3).

A LOCAL session is left ACTIVE with a :class:`LocalLivenessHandle` (PID +
process creation-time) by ``LocalExecutor.spawn`` after it launches aider
fire-and-forget. When that process exits, the session is still ACTIVE in cw
state but the PID is gone (or recycled to an unrelated process). This module
detects that dead-process condition and synthesizes the git-based completion:
it advances the owning task through the shared staged-advance authority and
marks the session COMPLETED/NORMAL.

Mirrors the detect/act split of ``phantom.py``/``idle.py``: ``_detect_*`` is
pure classification (zero writes); ``_act_*`` performs the mutations,
save_state, and event emission. See GitHub #888, ADR-0006.

A recorded ``aider`` backend is not trusted: a handle that predates the
``backend`` field migrates to an explicit ``"aider"`` (#2369), so
``_resolve_harvest_backend`` verifies it against the worktree's launch logs and
the session's spawn stage, and parks the row when it cannot (#2512).

A ``codex``-backend handle (RFC 0014 A1, #2387) is detected the same way but
never harvested through a result synthesizer: a crashed codex review leaves no
sentinel to synthesize. ``_act_on_local_harvest_candidates`` branches it out
before ``_synthesize_harvest_sentinel`` into an audited clean-requeue gate —
the four checks ``cw.reconcile.codex_boot`` requeues on (``reap_policy:
auto``, codex fix loop off, worktree clean apart from the review verdict, HEAD
unmoved since the review's baseline), all evaluated so the audit event records
each one. Any failing check parks; a clean pre-lock result defers because it
cannot safely authorize a requeue without a final worktree check.
Either way the session closes ``COMPLETED``/``CRASHED``, only after its
``SESSION_COMPLETED`` audit event is recorded. The sweep does not repeat the
boot pass's live-writer process scan: the recycled-PID guard has already
proven the codex process dead. The two git checks never run under the
caller's ``sessions_lock`` (#2563): ``reconcile()`` captures them first,
lockless, through :func:`capture_codex_harvest_probes`, and the gate reads
those pre-lock probes. A session whose probe is missing or stale is left
untouched (``PROBE_UNAVAILABLE``) for the next tick.

``act_on_codex_harvest_candidate`` has a second caller,
``cw.codex_legacy_recovery`` (``cw codex migrate-legacy``, RFC 0014 B1,
#2389). A legacy session carries no liveness handle, so that caller runs the
boot pass's live-writer scan itself before acting, captures the session's
clean probe before taking the lock, passes no handle, and passes a
``legacy_reason`` that marks the audit event as a legacy recovery.
The act helper returns a :class:`CodexHarvestOutcome` so that caller never
re-derives what happened from state.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from cw.codex_background import _resolve_codex_fix_loop_enabled
from cw.config import load_effective_config, save_state
from cw.events import record_event
from cw.local_runner import (
    AIDER_LOG_RELATIVE_PATH,
    LOCAL_EXECUTOR_FAILURE_ACTION,
    UNEXPECTED_ERROR,
    make_blocked,
    read_process_start_time_ns,
    synthesize_git_result,
)
from cw.models import (
    CODEX_BACKEND,
    DEFAULT_LANE,
    OCCUPIED_LANE_STATUSES,
    OPENCODE_BACKEND,
    CodexHarvestOutcome,
    CompletionReason,
    LastResultSource,
    OrchestratorEventType,
    ReapPolicy,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.opencode_runner import (
    OPENCODE_LOG_RELATIVE_PATH,
    stage_entry_marker,
    synthesize_opencode_result,
)
from cw.opencode_runner import (
    make_blocked as make_opencode_blocked,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    ProposedAction,
    ReapCandidate,
    StageRefusalPage,
    _apply_sentinel_to_task_audited,
    emit_stage_refusal_pages,
    stage_refusal_latched,
    stage_refusal_page,
    ticket_id_for_session,
)
from cw.reconcile.codex_boot import (
    _CLOSE_DISPOSITION_PARKED,
    _CLOSE_DISPOSITION_REQUEUED,
    _PARK_REASON_DIRTY_WORKTREE,
    _PARK_REASON_FIX_LOOP_ENABLED,
    _PARK_REASON_GIT_ERROR,
    _PARK_REASON_HEAD_MOVED,
    _PARK_REASON_REAP_POLICY_NOT_AUTO,
    CAPTURE_BUDGET_SECONDS,
    CleanProbeUnavailableError,
    lookup_probe,
)
from cw.reconcile.tasks import _resolve_task_policy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.models import (
        ClientConfig,
        CwState,
        LocalLivenessBackend,
        LocalLivenessHandle,
        OrchestratorConfig,
        Session,
    )
    from cw.reconcile.codex_boot import CleanProbes, ProbeSource

_log = logging.getLogger(__name__)

# TICKET_REQUEUED ``reason`` for a dead codex process whose worktree passed
# every clean-requeue check — the harvest-sweep sibling of
# codex_boot.CODEX_ORPHAN_CLEAN_REQUEUE_REASON.
CODEX_HARVEST_CLEAN_REQUEUE_REASON = "codex_harvest_clean_requeue"

# Short reason code stamped as a parked task's disposition and carried as the
# SESSION_NEEDS_ATTENTION payload's ``paused_status``.
CODEX_HARVEST_ORPHANED_DISPOSITION = "codex_review_orphaned_at_harvest"

_CODEX_HARVEST_BREADCRUMBS = (
    "The codex process recorded for this session exited without a result"
    " (crash or kill), found by the local harvest sweep — inspect the worktree"
    " for a partial commit or orphaned scratch dir before reclaiming."
)

AIDER_BACKEND: LocalLivenessBackend = "aider"


def _refusal_latch_binds(
    session: Session,
    ticket_id: str | None,
    tasks: Sequence[TicketTask],
) -> bool:
    """True while *session*'s latched refusal is still its row's live disposition.

    A latch stops the harvest re-refusing the same dead result every tick, but
    only while the row it refused is still bound to this session in an occupied
    status. "Bound" is decided exactly like the router's
    ``_lookup_matching_task``: ANY row in *tasks* with this ticket id AND this
    session id in an occupied status. A ticket-id-keyed dict cannot answer that:
    duplicate ``(client, ticket_id)`` rows exist (add-after-terminal), the dict
    keeps one of them, and when it keeps another row than the one owning this
    session the latch would be ignored, the session re-offered, and the router
    would re-refuse and re-page every tick. Once the row moved on (requeued,
    cancelled, session id cleared or replaced, ticket gone) nothing else
    completes the dead session -- idle/phantom/stalled skip the same markers --
    so honoring the latch would leave a stale ACTIVE record behind that makes
    name lookups ambiguous (#2490). Not-bound means: re-offer it, and the
    harvest completes it the ordinary way.
    """
    return bool(ticket_id) and any(
        task.ticket_id == ticket_id
        and task.session_id == session.id
        and task.status in OCCUPIED_LANE_STATUSES
        for task in tasks
    )


def _local_process_alive(handle: LocalLivenessHandle) -> bool:
    """Return True iff the handle's PID still names the same live process.

    Re-reads the process creation-time and requires it to match the value
    captured at spawn. A PID with no live process, or a start-time mismatch
    (PID recycled to an unrelated process) both read as NOT alive — the latter is
    the recycled-PID guard that keeps harvest from being fooled by PID reuse.
    """
    current = read_process_start_time_ns(handle.pid)
    return current is not None and current == handle.start_time_ns


def _detect_local_harvest_candidates(
    state: CwState,
    tasks: Sequence[TicketTask] = (),
) -> list[ReapCandidate]:
    """Pure classification phase for dead-process LOCAL harvest candidates.

    A candidate is any ACTIVE, DAEMON-origin session that carries a
    ``local_liveness`` handle, has no ``surface_ref`` (LOCAL sessions never do),
    and whose process is no longer alive. Makes zero writes. *tasks* is the full
    dev-queue row list: it stamps ``candidate.lane`` from the row for the
    session's ticket id (the last such row; missing rows default to
    ``DEFAULT_LANE``) and decides whether a latch still binds.

    A session carrying a stage-refusal latch (#2490) is skipped only while its
    row is still bound to it (:func:`_refusal_latch_binds`); once the row moved
    on, the session is offered again so it completes normally.
    """
    _task_by_ticket = {t.ticket_id: t for t in tasks}
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        if session.status is not SessionStatus.ACTIVE:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.local_liveness is None:
            continue
        if session.surface_ref is not None:
            continue
        ticket_id = ticket_id_for_session(session.name)
        if stage_refusal_latched(session) and _refusal_latch_binds(
            session, ticket_id, tasks
        ):
            continue
        if _local_process_alive(session.local_liveness):
            continue
        task = _task_by_ticket.get(ticket_id) if ticket_id else None
        lane = task.lane if task else DEFAULT_LANE
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.HARVEST_LOCAL_COMPLETE,
                ticket_id=ticket_id,
                lane=lane,
                client=session.client,
                worktree_path=session.worktree_path,
            )
        )
    return candidates


def _harvest_via_git(
    task: TicketTask, worktree: Path, default_branch: str, session_id: str
) -> AutoDevResult:
    return synthesize_git_result(
        task=task,
        worktree=worktree,
        default_branch=default_branch,
        plan_source="none",
        session_id=session_id,
    )


def _harvest_via_opencode_log(
    task: TicketTask, worktree: Path, default_branch: str, session_id: str
) -> AutoDevResult:
    del default_branch  # the sentinel comes from the JSONL log, not git facts
    return synthesize_opencode_result(
        task=task, worktree=worktree, session_id=session_id
    )


# Harvest-time result synthesizer per LocalLivenessHandle.backend (#2369).
# Each entry is normalized to (task, worktree, default_branch, session_id).
_HARVEST_SYNTHESIZERS: dict[
    LocalLivenessBackend,
    Callable[[TicketTask, Path, str, str], AutoDevResult],
] = {
    AIDER_BACKEND: _harvest_via_git,
    cast("LocalLivenessBackend", OPENCODE_BACKEND): _harvest_via_opencode_log,
}


def _log_exists(worktree: Path, relative_path: Path) -> bool:
    """True iff *relative_path* exists under *worktree*; ``OSError`` reads as absent."""
    with contextlib.suppress(OSError):
        return (worktree / relative_path).exists()
    return False


def _resolve_harvest_backend(
    backend: LocalLivenessBackend,
    session: Session,
    task: TicketTask,
    worktree: Path,
) -> LocalLivenessBackend | None:
    """The backend that really launched *session*, or ``None`` if it cannot be proven.

    A ``local_liveness`` handle written before the ``backend`` field existed
    (#2369) migrates to an explicit ``"aider"`` that is byte-identical to a genuine
    aider handle, so a recorded ``"aider"`` is verified here, from facts outside
    the handle (GitHub #2512). Log *contents* are never read; only presence is
    probed. Rules, in order:

    1. Recorded ``opencode`` / ``codex``: returned unchanged.
    2. Recorded ``aider``. ``.cw/aider.log`` is opened before ``Popen`` by a
       genuine aider launch, so it is always left behind:

       - only ``.cw/opencode.log``: it was an opencode run -> ``"opencode"``;
       - only ``.cw/aider.log``: ``"aider"`` (a misconfigured ``local`` backend on
         a non-IMPL stage is still refused and paged by the stage guard);
       - both or neither: the spawn stage decides. ``session.stage`` is the stage
         the executor was chosen for (``_create_executor_session``); the row's
         stage may have advanced since, so it is only the fallback when the
         session carries none. IMPL -> ``"aider"`` (git synthesis is
         stage-correct there); any other stage -> ``None``.
    """
    if backend != AIDER_BACKEND:
        return backend
    has_opencode_log = _log_exists(worktree, OPENCODE_LOG_RELATIVE_PATH)
    has_aider_log = _log_exists(worktree, AIDER_LOG_RELATIVE_PATH)
    if has_opencode_log and not has_aider_log:
        _log.warning(
            "harvest_backend_overridden: session=%s ticket=%s recorded=aider"
            " effective=opencode (only .cw/opencode.log present; GitHub #2512)",
            session.id,
            task.ticket_id,
        )
        return cast("LocalLivenessBackend", OPENCODE_BACKEND)
    if has_aider_log and not has_opencode_log:
        return AIDER_BACKEND
    return AIDER_BACKEND if (session.stage or task.stage) is Stage.IMPL else None


# Operator hint on the blocked result parked for a backend that could not be proven.
_UNPROVEN_BACKEND_NEXT_ACTIONS: list[str] = [LOCAL_EXECUTOR_FAILURE_ACTION]
# Under the 200-char cap the blocked-reason breadcrumb renders (#2512).
_UNPROVEN_BACKEND_DETAILS = (
    "The executor backend could not be proven from the liveness handle or the"
    " worktree launch logs, so no result was synthesized."
)


def _synthesize_harvest_sentinel(
    worktree: Path,
    task: TicketTask,
    default_branch: str,
    session_id: str,
    backend: LocalLivenessBackend | None,
) -> AutoDevResult:
    """Synthesize the harvest sentinel with the synthesizer for *backend*.

    Dispatches through ``_HARVEST_SYNTHESIZERS`` on the resolved backend
    (opencode → JSONL log parse, aider → git-fact synthesis); an unregistered
    backend falls back to git synthesis. ``None`` (backend unproven, see
    :func:`_resolve_harvest_backend`) never synthesizes: it returns a blocked
    ``unexpected_error`` result at the row's own entry marker, which the
    existing routing parks BLOCKED_ON_USER and pages. A git/opencode failure on
    one candidate must not abort the entire harvest sweep — the fallback returns a
    blocked result at the row's entry marker (never a later one, which would walk
    the pointer, never ``stage2_impl`` on a non-IMPL row) and logs the exception.
    """
    stage_reached = stage_entry_marker(task.stage.value)
    if backend is None:
        return make_blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=UNEXPECTED_ERROR,
            details=_UNPROVEN_BACKEND_DETAILS,
            stage_reached=stage_reached,
            next_actions=_UNPROVEN_BACKEND_NEXT_ACTIONS.copy(),
        )
    synthesize = _HARVEST_SYNTHESIZERS.get(backend, _harvest_via_git)
    try:
        return synthesize(task, worktree, default_branch, session_id)
    except (OSError, subprocess.CalledProcessError):
        _log.warning(
            "harvest_synthesis_failed: session=%s ticket=%s backend=%s;"
            " parking at the row's stage (GitHub #2512)",
            session_id,
            task.ticket_id,
            backend,
            exc_info=True,
        )
        blocked = (
            make_opencode_blocked
            if backend == cast("LocalLivenessBackend", OPENCODE_BACKEND)
            else make_blocked
        )
        return blocked(
            ticket_id=task.ticket_id,
            worktree=worktree,
            reason=UNEXPECTED_ERROR,
            stage_reached=stage_reached,
        )


@dataclass(frozen=True)
class _CodexGateResult:
    """The clean-requeue gate's verdict for one dead codex process.

    ``checks`` maps each gate check to whether it passed; a git check that
    could not be answered counts as failed. ``reason`` is the first failure in
    ``codex_boot._gate_clean_requeue``'s priority order, or
    ``CODEX_HARVEST_CLEAN_REQUEUE_REASON`` when every check passed.
    """

    checks: dict[str, bool]
    should_requeue: bool
    reason: str


def _codex_gate_reason(
    *,
    auto: bool,
    fix_loop_disabled: bool,
    clean: bool | None,
    head_matches: bool | None,
) -> str:
    """Pick the park reason the boot pass would give, or the requeue reason.

    ``None`` from a git check reads as the boot pass's git-error reason, never
    as dirt or a moved HEAD.
    """
    failures = (
        (not auto, _PARK_REASON_REAP_POLICY_NOT_AUTO),
        (not fix_loop_disabled, _PARK_REASON_FIX_LOOP_ENABLED),
        (clean is None, _PARK_REASON_GIT_ERROR),
        (clean is False, _PARK_REASON_DIRTY_WORKTREE),
        (head_matches is None, _PARK_REASON_GIT_ERROR),
        (head_matches is False, _PARK_REASON_HEAD_MOVED),
    )
    return next(
        (reason for failed, reason in failures if failed),
        CODEX_HARVEST_CLEAN_REQUEUE_REASON,
    )


def _evaluate_codex_clean_requeue_gate(
    worktree: Path,
    task: TicketTask,
    client: ClientConfig,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    probe_source: ProbeSource,
) -> _CodexGateResult:
    """Run all four clean-requeue checks for a dead codex process.

    Reuses the boot pass's primitives unchanged but, unlike its short-
    circuiting ``_gate_clean_requeue``, evaluates every check so the audit
    event can report each result. No live-writer scan: the harvest sweep
    already proved the process dead through the recycled-PID guard, and the
    legacy caller (``cw.codex_legacy_recovery``) runs
    ``codex_boot.live_writer_park`` before it gets here.

    The two git checks come from *probe_source* (#2563): a lockless capture
    in the pre-pass, an in-lock lookup in the act helper, which raises
    ``CleanProbeUnavailableError`` when it has no usable probe.
    """
    auto = _resolve_task_policy(task.client, task.lane, clients, config) is (
        ReapPolicy.AUTO
    )
    fix_loop_disabled = not _resolve_codex_fix_loop_enabled(client, task, config)
    probe = probe_source(worktree, task, clients)
    checks = {
        "reap_policy_auto": auto,
        "fix_loop_disabled": fix_loop_disabled,
        "worktree_clean": bool(probe.clean),
        "head_unmoved": bool(probe.head_matches),
    }
    return _CodexGateResult(
        checks=checks,
        should_requeue=all(checks.values()),
        reason=_codex_gate_reason(
            auto=auto,
            fix_loop_disabled=fix_loop_disabled,
            clean=probe.clean,
            head_matches=probe.head_matches,
        ),
    )


def _codex_recovery_audit_payload(
    session: Session,
    handle: LocalLivenessHandle | None,
    task: TicketTask,
    gate: _CodexGateResult,
    *,
    legacy_reason: str | None = None,
) -> dict[str, object]:
    """SESSION_COMPLETED payload for a dead codex process this sweep closes.

    The crashed, nothing-salvaged shape of ``codex_boot._close_audit_payload``
    plus the recovery evidence: the executor, the status transition, every
    gate check, and the PID/start-time the recycled-PID guard judged dead.
    ``crashed: True`` also keeps the dispatch consumer from completing the
    task off this event.

    *legacy_reason* marks a legacy recovery (``cw codex migrate-legacy``,
    #2389): it replaces ``reason``, the gate's own reason moves to
    ``detail`` (the ``_close_audit_payload`` field), and ``legacy: True`` is
    added. A legacy session has no liveness handle, so ``pid`` and
    ``start_time_ns`` are None. Without it the payload is unchanged.
    """
    payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "ticket_id": task.ticket_id,
        "client": session.client,
        "executor": CODEX_BACKEND,
        "crashed": True,
        "salvaged": False,
        "prior_status": session.status.value,
        "resulting_status": SessionStatus.COMPLETED.value,
        "disposition": (
            _CLOSE_DISPOSITION_REQUEUED
            if gate.should_requeue
            else _CLOSE_DISPOSITION_PARKED
        ),
        "reason": gate.reason,
        "gate_checks": dict(gate.checks),
        "pid": handle.pid if handle is not None else None,
        "start_time_ns": handle.start_time_ns if handle is not None else None,
    }
    if legacy_reason is not None:
        payload["reason"] = legacy_reason
        payload["legacy"] = True
        payload["detail"] = gate.reason
    return payload


def _requeue_codex_harvest_orphan(session: Session, task: TicketTask) -> bool:
    """Revert the task to PENDING; report it only if the revert happened.

    Payload mirrors ``codex_boot._requeue_clean_orphan`` field for field.
    Returns whether the revert happened.
    """
    # Deferred for the import-cycle reason codex_boot documents.
    from cw.dispatch.claim import _revert_claimed_task_to_pending

    if not _revert_claimed_task_to_pending(
        session.client, task.ticket_id, expected_session_id=session.id
    ):
        _log.warning(
            "reconcile.local: %s/%s no longer belongs to codex session %s;"
            " requeue skipped",
            session.client,
            task.ticket_id,
            session.id,
        )
        return False
    record_event(
        OrchestratorEventType.TICKET_REQUEUED,
        {
            "ticket_id": task.ticket_id,
            "client": session.client,
            "from_stage": task.stage,
            "to_stage": task.stage,
            "reason": CODEX_HARVEST_CLEAN_REQUEUE_REASON,
            "session_id": session.id,
        },
        correlation_id=task.ticket_id,
    )
    return True


def _defer_prelock_requeue(
    probes: CleanProbes | None, gate: _CodexGateResult
) -> None:
    """Reject a requeue that lacks a final worktree check."""
    if probes is not None and gate.should_requeue:
        raise CleanProbeUnavailableError


def act_on_codex_harvest_candidate(
    state: CwState,
    session: Session,
    handle: LocalLivenessHandle | None,
    task: TicketTask,
    client: ClientConfig,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    *,
    worktree: Path,
    now: datetime,
    probes: CleanProbes | None,
    legacy_reason: str | None = None,
) -> CodexHarvestOutcome:
    """Gate, audit, close, then requeue or park one dead codex process.

    The gate's git checks are read from *probes*, captured before the caller
    took ``sessions_lock`` (#2563); nothing here runs git. A missing or stale
    probe (``None`` means none was captured), or a clean probe that would
    authorize a requeue without a final worktree check, leaves everything
    untouched and returns ``PROBE_UNAVAILABLE``, before any audit event, so
    the next tick retries.

    Audit before effect (``codex_boot._close_session_audited``'s ordering): a
    failed audit write transitions nothing (``AUDIT_FAILED``), so the next
    tick re-detects the same dead PID and retries. Session before task, the
    boot pass's crash-recoverable order: a closed session whose task is still
    RUNNING is picked up by reconcile's ``revert_completed_silent_tasks``
    backstop. The task transition re-verifies ``expected_session_id`` under
    the dev-queue lock, so a row re-claimed since the caller's snapshot is
    left alone and reported as ``TRANSITION_LOST``.

    Mutates *session* and persists the caller's *state* with ``save_state``,
    which takes no lock, and never re-reads the session: the caller must hold
    ``sessions_lock`` across the call. An ``OSError`` from that write or from
    a task transition propagates. *handle* is None and *legacy_reason* set
    only for ``cw.codex_legacy_recovery`` (see
    :func:`_codex_recovery_audit_payload`).
    """
    try:
        gate = _evaluate_codex_clean_requeue_gate(
            worktree, task, client, clients, config, lookup_probe(probes)
        )
        _defer_prelock_requeue(probes, gate)
    except CleanProbeUnavailableError:
        _log.warning(
            "reconcile.local: codex session %s (%s/%s) has no usable clean"
            " probe; leaving it for the next tick",
            session.id,
            session.client,
            task.ticket_id,
        )
        return CodexHarvestOutcome.PROBE_UNAVAILABLE
    try:
        record_event(
            OrchestratorEventType.SESSION_COMPLETED,
            _codex_recovery_audit_payload(
                session, handle, task, gate, legacy_reason=legacy_reason
            ),
            correlation_id=task.ticket_id,
        )
    except OSError:
        _log.exception(
            "reconcile.local: could not record the audit event for dead codex"
            " session %s (%s/%s); leaving it for the next tick",
            session.id,
            session.client,
            task.ticket_id,
        )
        return CodexHarvestOutcome.AUDIT_FAILED
    if not gate.should_requeue:
        # Persist the task disposition with the session closure.  If this
        # process dies before the park helper runs, the completed-session
        # backstop must preserve this failed-gate outcome instead of applying
        # its generic RUNNING -> PENDING fallback.
        session.recovery_disposition = CODEX_HARVEST_ORPHANED_DISPOSITION
        session.recovery_reason = gate.reason
    session.status = SessionStatus.COMPLETED
    session.completed_reason = CompletionReason.CRASHED
    session.completed_at = now
    save_state(state)
    if gate.should_requeue:
        if _requeue_codex_harvest_orphan(session, task):
            return CodexHarvestOutcome.REQUEUED
        return CodexHarvestOutcome.TRANSITION_LOST
    # Deferred for the import-cycle reason codex_boot documents.
    from cw.dispatch.claim import _park_running_task_blocked_on_user

    if not _park_running_task_blocked_on_user(
        ticket_id=task.ticket_id,
        client_name=session.client,
        expected_session_id=session.id,
        disposition=CODEX_HARVEST_ORPHANED_DISPOSITION,
        breadcrumbs=f"{_CODEX_HARVEST_BREADCRUMBS} ({gate.reason}).",
    ):
        _log.warning(
            "reconcile.local: %s/%s no longer belongs to codex session %s;"
            " park skipped",
            session.client,
            task.ticket_id,
            session.id,
        )
        return CodexHarvestOutcome.TRANSITION_LOST
    return CodexHarvestOutcome.PARKED


def _codex_gate_target(
    session: Session, real_task: TicketTask | None, client: ClientConfig | None
) -> tuple[TicketTask, ClientConfig] | None:
    """The row and client a dead codex session is gated on, or None.

    The one predicate the sweep and its lockless probe pre-pass share: a
    synthetic task carries no lane, baseline, or claim to gate or transition,
    so a missing row, a missing client config, or another client's row is
    never gated.
    """
    if (
        real_task is None
        or client is None
        or real_task.client != session.client
        or (real_task.session_id or real_task.codex_orphan_session_id)
        != session.id
    ):
        return None
    return real_task, client


def _harvest_codex_candidate(
    state: CwState,
    session: Session,
    handle: LocalLivenessHandle,
    candidate: ReapCandidate,
    *,
    worktree: Path,
    real_task: TicketTask | None,
    client: ClientConfig | None,
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    now: datetime,
    probes: CleanProbes | None,
) -> CodexHarvestOutcome:
    """Gate a dead codex process on its real dev-queue row and client config.

    *real_task* is the row looked up before the sweep's synthetic-task
    fallback: a synthetic task carries no lane, baseline, or claim to gate or
    transition, so a missing row (or one belonging to another client) leaves
    the session untouched for a later tick rather than guessing (``NO_ROW``).
    Otherwise returns the act helper's outcome, gated on *probes* (#2563).
    """
    target = _codex_gate_target(session, real_task, client)
    if target is None:
        _log.warning(
            "reconcile.local: dead codex session %s (%s/%s) has no matching"
            " dev-queue row or client config; leaving it for the next tick",
            session.id,
            session.client,
            candidate.ticket_id,
        )
        return CodexHarvestOutcome.NO_ROW
    gated_task, gated_client = target
    return act_on_codex_harvest_candidate(
        state,
        session,
        handle,
        gated_task,
        gated_client,
        clients,
        config,
        worktree=worktree,
        now=now,
        probes=probes,
    )


def _codex_probe_targets(
    state: CwState, tasks: Sequence[TicketTask]
) -> list[tuple[Session, Path, TicketTask | None]]:
    """The dead codex sessions the sweep would branch to its codex gate.

    The sweep's own detect pass, filtered exactly as its act phase filters:
    a worktree and a codex liveness handle, paired with the row looked up the
    same way (``{ticket_id: row}``, last one wins).
    """
    task_by_ticket = {t.ticket_id: t for t in tasks}
    session_by_id = {s.id: s for s in state.sessions}
    targets: list[tuple[Session, Path, TicketTask | None]] = []
    for candidate in _detect_local_harvest_candidates(state, tasks):
        session = session_by_id[candidate.session_id]
        handle = session.local_liveness
        if (
            candidate.worktree_path is None
            or handle is None
            or handle.backend != CODEX_BACKEND
        ):
            continue
        real_task = (
            task_by_ticket.get(candidate.ticket_id) if candidate.ticket_id else None
        )
        targets.append((session, candidate.worktree_path, real_task))
    return targets


def capture_codex_harvest_probes(
    state: CwState,
    tasks: Sequence[TicketTask],
    *,
    config: OrchestratorConfig,
    probes: CleanProbes,
) -> None:
    """Lockless pre-pass: capture each gateable dead codex session's probe (#2563).

    The candidates are exactly the ones the sweep's codex branch would gate
    (same detect pass, same row and client guard), and each is evaluated
    through the sweep's own gate with *probes*' capture as the probe source;
    the gate result is discarded, only the probes recorded in *probes* are
    kept. Clients load only when there is a codex candidate. Once the capture
    budget is spent the rest get no probe, so they defer in-lock. Runs git,
    so it must never be called with ``sessions_lock`` held.
    """
    targets = _codex_probe_targets(state, tasks)
    if not targets:
        return
    clients = _deps.load_effective_clients()
    for index, (session, worktree, real_task) in enumerate(targets):
        target = _codex_gate_target(session, real_task, clients.get(session.client))
        if target is None:
            continue
        task, client = target
        try:
            _evaluate_codex_clean_requeue_gate(
                worktree, task, client, clients, config, probes.capture
            )
        except CleanProbeUnavailableError:
            _log.warning(
                "reconcile: codex clean-probe budget (%.0fs) spent;"
                " %d candidate(s) deferred to the next tick",
                CAPTURE_BUDGET_SECONDS,
                len(targets) - index,
            )
            return


def _act_on_local_harvest_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    task_by_ticket: dict[str, TicketTask] | None = None,
    config: OrchestratorConfig | None = None,
    codex_probes: CleanProbes | None = None,
) -> list[str]:
    """Act phase: synthesize the git result, advance the task, complete session.

    A ``codex``-backend candidate branches out first to
    ``_harvest_codex_candidate`` (#2387): no sentinel is synthesized or routed
    for it, and its ticket id is never counted as harvested. *config* is the
    orchestrator config its clean-requeue gate resolves against; omitted, it
    is loaded with ``load_effective_config``. Its gate reads *codex_probes*,
    the clean probes ``reconcile()`` captured before taking the lock
    (#2563); omitted, none was captured and every codex candidate defers.

    For each candidate, in canonical order (task first, then session — mirroring
    ``phantom._apply_phantom_routed_mutations`` and the ``_apply_sentinel_to_task``
    docstring): synthesize an AutoDevResult from git facts, route it through the
    shared staged-advance authority, then mark the session COMPLETED/NORMAL. Emits
    a ``SESSION_COMPLETED`` event with ``crashed: False`` and no result payload;
    dispatch simply reads ``last_result`` as written by the RFC 0012 door.
    Returns the harvested ticket IDs. Acquires no gh subprocess; runs entirely
    under the caller's ``sessions_lock``.

    GitHub #1031 (extends #1019's phantom-path guard): when
    ``_apply_sentinel_to_task`` reports ``routed=False`` (a stage-mismatch
    refusal, the #986 incident), the candidate's session must NOT be
    completed and its ticket_id must NOT be counted as harvested -- the task
    row was left untouched, so completing the session here would orphan it.

    GitHub #2490: that refusal is permanent -- the process is dead, so no
    matching sentinel will follow -- and used to repeat silently every tick
    (one ``sentinel.stage_mismatch`` event per tick, the worker's real result
    never applied, nobody paged). It now emits one ``session.needs_attention``
    (``paused_status=sentinel_stage_mismatch_dead_session``) and only THEN
    latches the refusal on that session (detection skips it from then on, while
    its row is still bound to it): a session whose page write failed stays
    un-latched and is re-paged next tick
    (:func:`cw.reconcile._shared.emit_stage_refusal_pages`).

    GitHub #2140: a ``not routed`` outcome can also mean
    ``task_already_terminal`` -- the dev-queue task was raced to a genuinely
    terminal status by a concurrent caller before this lookup ran, not a
    stage-mismatch refusal. Without this carve-out, every subsequent tick
    would re-detect the same dead-PID candidate and re-synthesize the harvest
    sentinel (a real git/opencode subprocess call) forever, re-hitting the
    identical race deterministically. That case is now admitted past this
    bail so it flows through the shared audited result door -- the same
    first-writer and audit ordering the ordinary path uses.
    """
    if not candidates:
        return []
    _task_by_ticket = task_by_ticket or {}
    _config = config if config is not None else load_effective_config()
    session_by_id = {s.id: s for s in state.sessions}
    clients = _deps.load_effective_clients()
    harvested_ticket_ids: list[str] = []
    pending_events: list[dict[str, object]] = []
    refusal_pages: list[StageRefusalPage] = []

    for candidate in candidates:
        session = session_by_id[candidate.session_id]
        if candidate.worktree_path is None or session.local_liveness is None:
            # Unreachable: detection admits only sessions with a worktree and a
            # liveness handle; the guard narrows both for the calls below.
            continue
        client_cfg = clients.get(session.client)
        default_branch = client_cfg.default_branch if client_cfg is not None else "main"
        task = _task_by_ticket.get(candidate.ticket_id) if candidate.ticket_id else None
        if task is None:
            task = TicketTask(
                ticket_id=candidate.ticket_id or "",
                client=session.client,
                stage=session.stage or Stage.IMPL,
            )
        if session.local_liveness.backend == CODEX_BACKEND:
            # Never the synthetic `task` above: only a real row can be gated.
            # The outcome is discarded: every non-final one (AUDIT_FAILED,
            # NO_ROW) leaves the session ACTIVE, so the next tick re-detects
            # it; only cw.codex_legacy_recovery needs to tell them apart.
            _harvest_codex_candidate(
                state,
                session,
                session.local_liveness,
                candidate,
                worktree=candidate.worktree_path,
                real_task=(
                    _task_by_ticket.get(candidate.ticket_id)
                    if candidate.ticket_id
                    else None
                ),
                client=client_cfg,
                clients=clients,
                config=_config,
                now=now,
                probes=codex_probes,
            )
            continue

        # A recorded "aider" is verified against the launch logs and spawn stage
        # (#2512); None = unproven, which parks the row instead of guessing.
        backend = _resolve_harvest_backend(
            session.local_liveness.backend, session, task, candidate.worktree_path
        )
        sentinel = _synthesize_harvest_sentinel(
            worktree=candidate.worktree_path,
            task=task,
            default_branch=default_branch,
            session_id=candidate.session_id,
            backend=backend,
        )
        # Task first (before the session status change) so the task is in its
        # terminal/advanced state when revert_completed_silent_tasks runs.
        routed = True
        task_already_terminal = False
        audited = _apply_sentinel_to_task_audited(
            candidate.ticket_id,
            session,
            sentinel,
            source=LastResultSource.GIT_SYNTHESIS,
        )
        if audited.emit is not None and audited.emit.refused:
            continue
        outcome = audited.route
        if outcome is not None:
            routed = outcome.routed
            task_already_terminal = outcome.task_already_terminal
        if not routed and not task_already_terminal:
            refusal_page = stage_refusal_page(
                session,
                outcome,
                ticket_id=task.ticket_id,
                lane=task.lane,
                sentinel=sentinel,
                subject=f"dead {backend or session.local_liveness.backend} process",
                live=False,
            )
            if refusal_page is not None:
                refusal_pages.append(refusal_page)
            continue
        # RFC 0012 A3 (#1459): route the git-synthesized completion through the
        # door (source=GIT_SYNTHESIS) instead of writing session.last_result
        # directly. A first-writer-wins refusal (another authority already
        # recorded a terminal result) short-circuits the WHOLE completion for
        # this candidate -- skip the harvested-id count, the session-completion
        # stamp, and the SESSION_COMPLETED event. The shared audited seam has
        # already arbitrated first-writer-wins and recorded the event before
        # persisting a routed queue mutation.
        if candidate.ticket_id:
            harvested_ticket_ids.append(candidate.ticket_id)
        session.status = SessionStatus.COMPLETED
        session.completed_reason = CompletionReason.NORMAL
        session.completed_at = now
        harvest_payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "crashed": False,
        }
        if candidate.ticket_id:
            harvest_payload["ticket_id"] = candidate.ticket_id
        pending_events.append(harvest_payload)

    # Pages BEFORE the save and before every SESSION_COMPLETED write: the latch
    # stamped inside is persisted by this one save_state, so a session is never
    # durably latched without its page, and a failing SESSION_COMPLETED write
    # below cannot suppress any page (#2490). At-least-once: see
    # emit_stage_refusal_pages.
    emit_stage_refusal_pages(refusal_pages)

    save_state(state)

    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    return harvested_ticket_ids
