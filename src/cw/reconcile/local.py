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
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from cw.config import save_state
from cw.events import record_event
from cw.local_runner import (
    UNEXPECTED_ERROR,
    make_blocked,
    read_process_start_time_ns,
    synthesize_git_result,
)
from cw.models import (
    DEFAULT_LANE,
    CompletionReason,
    LastResultSource,
    OrchestratorEventType,
    SessionOrigin,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    ProposedAction,
    ReapCandidate,
    _apply_sentinel_to_task,
    ticket_id_for_session,
)
from cw.result import emit_result_on

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import CwState, LocalLivenessHandle


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
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[ReapCandidate]:
    """Pure classification phase for dead-process LOCAL harvest candidates.

    A candidate is any ACTIVE, DAEMON-origin session that carries a
    ``local_liveness`` handle, has no ``surface_ref`` (LOCAL sessions never do),
    and whose process is no longer alive. Makes zero writes. ``task_by_ticket``
    stamps ``candidate.lane`` from the owning task; missing tasks default to
    ``DEFAULT_LANE``.
    """
    _task_by_ticket = task_by_ticket or {}
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
        if _local_process_alive(session.local_liveness):
            continue
        ticket_id = ticket_id_for_session(session.name)
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


def _act_on_local_harvest_candidates(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    task_by_ticket: dict[str, TicketTask] | None = None,
) -> list[str]:
    """Act phase: synthesize the git result, advance the task, complete session.

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
    """
    if not candidates:
        return []
    _task_by_ticket = task_by_ticket or {}
    session_by_id = {s.id: s for s in state.sessions}
    clients = _deps.load_effective_clients()
    harvested_ticket_ids: list[str] = []
    pending_events: list[dict[str, object]] = []

    for candidate in candidates:
        session = session_by_id[candidate.session_id]
        if candidate.worktree_path is None:
            continue  # LOCAL DAEMON sessions always carry a worktree; defensive.
        client_cfg = clients.get(session.client)
        default_branch = client_cfg.default_branch if client_cfg is not None else "main"
        task = _task_by_ticket.get(candidate.ticket_id) if candidate.ticket_id else None
        if task is None:
            task = TicketTask(
                ticket_id=candidate.ticket_id or "",
                client=session.client,
                stage=session.stage or Stage.IMPL,
            )

        try:
            sentinel = synthesize_git_result(
                task=task,
                worktree=candidate.worktree_path,
                default_branch=default_branch,
                plan_source="none",
                session_id=candidate.session_id,
            )
        except (OSError, subprocess.CalledProcessError):
            # A git failure on one candidate must not abort the entire harvest
            # sweep. Record UNEXPECTED_ERROR so the task is advanced/reverted
            # through the normal blocked path.
            sentinel = make_blocked(
                ticket_id=candidate.ticket_id or "",
                worktree=candidate.worktree_path,
                reason=UNEXPECTED_ERROR,
            )
        # Task first (before the session status change) so the task is in its
        # terminal/advanced state when revert_completed_silent_tasks runs.
        routed = True
        if candidate.ticket_id:
            outcome = _apply_sentinel_to_task(
                candidate.ticket_id, session, sentinel, now=now
            )
            routed = outcome.routed
        if not routed:
            continue
        # RFC 0012 A3 (#1459): route the git-synthesized completion through the
        # door (source=GIT_SYNTHESIS) instead of writing session.last_result
        # directly. A first-writer-wins refusal (another authority already
        # recorded a terminal result) short-circuits the WHOLE completion for
        # this candidate -- skip the harvested-id count, the session-completion
        # stamp, and the SESSION_COMPLETED event. The task was already routed
        # by _apply_sentinel_to_task above (pre-existing ordering, unchanged);
        # a refusal does not roll that back (Adopted Assumption 2). The door's
        # own warning logs existing_source/attempted_source, so no log here.
        emit_outcome = emit_result_on(
            session,
            sentinel.model_dump(mode="json"),
            source=LastResultSource.GIT_SYNTHESIS,
        )
        if emit_outcome.refused:
            continue
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

    save_state(state)

    for payload in pending_events:
        record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    return harvested_ticket_ids
