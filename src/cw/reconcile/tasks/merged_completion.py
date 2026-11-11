"""Timed-out-merged and never-claimed task completion for reconcile (#2631).

Completes PENDING tasks whose TIMED_OUT session's PR merged, and refuses the
never-claimed rows that cannot be completed that way. See GitHub #488, #1269.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from cw.config import load_clients, load_orchestrator_config, load_state
from cw.dev_queue import (
    _stamp_salvage_stage,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.gh import (
    TIMED_OUT_MERGED_LOOKBACK_DAYS,
    resolve_merged_via_pr_state,
)
from cw.models import (
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
    TicketTask,
    resolve_feature_branch,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _NEVER_CLAIMED_COMPLETION_REASON,
    _TIMED_OUT_MERGED_REASON,
    ticket_id_for_session,
)
from cw.reconcile.tasks._client_config import (
    _client_cwd,
    _is_dangling_client,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, Session


def _collect_timed_out_merged_candidates(
    sessions: list[Session],
    task_by_ticket: dict[tuple[str, str], TicketTask],
    cutoff: datetime,
) -> list[tuple[Session, str]]:
    """Cheap pre-gh filter for ``complete_timed_out_merged_tasks`` (Phase 1).

    Returns (session, ticket_id) pairs for TIMED_OUT DAEMON sessions inside the
    lookback window whose dev-queue task is still PENDING. Makes zero gh calls.
    """
    # session.branch is None for all DAEMON sessions (spawn.py never sets it).
    candidates: list[tuple[Session, str]] = []
    for session in sessions:
        if session.status != SessionStatus.TIMED_OUT:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        # Guard: completed_at may be None in legacy state files.
        if session.completed_at is None:
            continue
        if session.completed_at < cutoff:
            continue
        ticket_id = ticket_id_for_session(session.name)
        if ticket_id is None:
            continue
        # Idempotency gate: only PENDING tasks are safe to auto-complete.
        # RUNNING means a new session already picked it up; terminal means done.
        task = task_by_ticket.get((session.client, ticket_id))
        if task is None or task.status != QueueItemStatus.PENDING:
            continue
        candidates.append((session, ticket_id))
    return candidates


def _is_never_claimed(task: TicketTask) -> bool:
    """True when *task* shows no evidence of ever attaching to a worker.

    Refuses when every attempt this row made died on the generic
    spawn-error path (dispatch/claim.py:793, stamp_backoff=True increments
    spawn_error_count in lockstep with attempts at claim.py:435), i.e.
    attempts == spawn_error_count. The original #1387 shape (attempts == 0,
    session_id is None) is a degenerate case of this: attempts == 0 forces
    spawn_error_count == 0 too (it can only increment after a claim already
    happened), so attempts == 0 == spawn_error_count always holds and
    #1387's refusal is preserved unchanged (R4).

    No legitimate completion can satisfy this: attempts increments only at
    claim time (dispatch/claim.py:250,278) and is never decremented on
    revert; session_id is stamped only on spawn and cleared on every
    revert-to-PENDING path. A task in this shape reaching this call site is
    definitionally a reconciler false-match (GitHub #1385, #1623), not a
    genuine completion.

    Second disjunct (#1631): ``not task.ever_spawned``. The first disjunct
    alone cannot see a row whose every attempt failed via UsageLimitError,
    because that revert path deliberately does not stamp spawn_error_count
    (stamp_backoff=False -- #868 fleet-wide backoff, see #1623 R7), leaving
    attempts > spawn_error_count. On the task record such a row is identical
    to one that legitimately ran, shipped and timed out inside its first
    stage (both have stage_high_water is None), so no arrangement of the
    existing counters can separate them -- it takes the durable
    ``ever_spawned`` fact, stamped True at dispatch/claim.py's one
    spawn-success seam and never cleared. Rows predating that field, and any
    built without it, default to True and so are untouched by this disjunct
    (fail-open: see the field's comment in models/tasks.py).
    """
    return (
        task.session_id is None and task.attempts == task.spawn_error_count
    ) or not task.ever_spawned


def _emit_never_claimed_refusals(
    refused: list[tuple[str, Session, str, bool]],
) -> None:
    """Emit SESSION_NEEDS_ATTENTION for each refused false-completion.

    Called after dev_queue_lock releases, mirroring this module's existing
    SESSION_COMPLETED emission (Phase 4); emitting after the queue lock is a
    choice ADR-0019 permits, not a lock-order requirement.

    Emits one of two cause-specific breadcrumb variants, selected by the
    tuple's ``ever_spawned`` element: the two disjuncts of
    :func:`_is_never_claimed` describe genuinely different histories, and a
    single string naming only the spawn-error path would misattribute every
    #1631 usage-limit refusal to a mechanism that provably did not fire for
    it (that path leaves spawn_error_count at 0 by design).
    """
    for ticket_id, session, lane, ever_spawned in refused:
        if ever_spawned:
            breadcrumbs = (
                f"matched timed-out session {session.id!r} but the dev-queue "
                "task was never claimed (session_id=None, every attempt "
                "died on the spawn-error path) -- refusing false completion"
            )
        else:
            breadcrumbs = (
                f"matched timed-out session {session.id!r} but the dev-queue "
                "task never successfully spawned a session (ever_spawned=False, "
                "every attempt died before a genuine spawn -- e.g. usage-limit "
                "reverts, which leave spawn_error_count at 0) -- refusing false "
                "completion"
            )
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": "",
                "session_name": "",
                "client": session.client,
                "ticket_id": ticket_id,
                "claude_session_id": None,
                "paused_status": _NEVER_CLAIMED_COMPLETION_REASON,
                "breadcrumbs": breadcrumbs,
                "crashed": False,
                "lane": lane,
            },
            correlation_id=ticket_id,
        )


def _filter_merged_candidates(
    candidates: list[tuple[Session, str]],
    clients: dict[str, ClientConfig],
    task_by_ticket: dict[tuple[str, str], TicketTask],
    max_age_seconds: int,
) -> list[tuple[Session, str]]:
    """One gh call per candidate to keep only merged-PR tickets (Phase 2).

    Runs outside any lock. Stops scanning if the gh binary is absent; skips
    candidates with transient gh errors, unmerged PRs, or a dangling client
    (present in the candidate but absent from a populated *clients*, GitHub
    #1269 -- see :func:`_is_dangling_client`).

    Each candidate's branch comes from :func:`cw.models.resolve_feature_branch`
    with its *task_by_ticket* row (the row's recorded branch, else the client's
    prefix/ticket) so it matches what the staged pipeline provisions (GitHub
    #728, #2948).

    GitHub #975: *task_by_ticket* / *max_age_seconds* let each candidate's
    merged verdict come from its task's fresh persisted pr_state first,
    falling back to the gh call only when stale/absent.
    """
    to_complete: list[tuple[Session, str]] = []
    for session, ticket_id in candidates:
        if _is_dangling_client(session.client, clients):
            continue
        branch = resolve_feature_branch(
            ticket_id,
            clients.get(session.client),
            task_by_ticket.get((session.client, ticket_id)),
        )
        cwd = _client_cwd(session.client, clients)
        merged, gh_available = resolve_merged_via_pr_state(
            ticket_id,
            session.client,
            task_by_ticket,
            max_age_seconds=max_age_seconds,
            gh_fallback=partial(
                _deps.pr_is_merged_for_ticket, ticket_id, branch=branch, cwd=cwd
            ),
        )
        if not gh_available:
            # gh binary absent — skip all remaining candidates.
            break
        if merged is None:
            # Transient error — skip this session only.
            continue
        if merged:
            to_complete.append((session, ticket_id))
        # merged is False → leave PENDING.
    return to_complete


def complete_timed_out_merged_tasks() -> list[str]:
    """Upgrade PENDING TicketTasks to COMPLETED when their PR merged.

    Targets TIMED_OUT DAEMON sessions in the lookback window whose PR merged.

    Post-pass over TIMED_OUT DAEMON sessions in the lookback window. For each
    whose dev-queue task is still PENDING and whose linked PR is MERGED (via
    issue-linkage), upgrades the task to COMPLETED and emits SESSION_COMPLETED
    with reason="timed_out_merged".

    A matching PENDING row with no claim history -- either attempts ==
    spawn_error_count with session_id is None (every attempt died on the
    generic spawn-error path), or ever_spawned False (no attempt ever
    reached a genuine spawn, the usage-limit shape whose revert leaves
    spawn_error_count at 0) -- is refused rather than completed. Such a row
    was never dispatched and its match is a reconciler false-match (GitHub
    #1385), not a genuine completion. It is left PENDING and a
    SESSION_NEEDS_ATTENTION event is emitted with
    paused_status=_NEVER_CLAIMED_COMPLETION_REASON and a breadcrumb naming
    which of the two causes fired (#1387 belt-and-braces guard, widened by
    #1623 to cover attempts > 0 spawn-error-only histories and by #1631 to
    cover usage-limit-only ones).

    Called from reconcile() AFTER sessions_lock is released — no gh subprocess
    runs under the session lock (liveness requirement, #485 SHOULD_FIX 4).

    Returns the list of ticket IDs auto-completed.
    """
    state = load_state()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=TIMED_OUT_MERGED_LOOKBACK_DAYS)

    # Build a cheap lookup: (client, ticket_id) → task (for PENDING filter
    # before gh call). Scoped by client so a TIMED_OUT session in one
    # client's queue cannot match a same-numbered ticket in another
    # client's queue (GitHub #1385, mirrors the #1054 fix in core.py).
    task_by_ticket: dict[tuple[str, str], TicketTask] = {
        (t.client, t.ticket_id): t for t in load_dev_queue().tasks
    }

    # Phase 1: Cheap filters before any gh subprocess call.
    candidates = _collect_timed_out_merged_candidates(
        state.sessions, task_by_ticket, cutoff
    )
    if not candidates:
        return []

    # Load clients once for branch resolution (resolve_feature_branch, #728).
    clients = load_clients()
    orchestrator_config = load_orchestrator_config()

    # Phase 2: One gh call per surviving candidate (outside any lock).
    to_complete = _filter_merged_candidates(
        candidates,
        clients,
        task_by_ticket,
        orchestrator_config.pr_hydration_interval_seconds,
    )
    if not to_complete:
        return []

    # Phase 3: Acquire only dev_queue_lock for the PENDING→COMPLETED write.
    completed_ids: list[str] = []
    refused: list[tuple[str, Session, str, bool]] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for session, ticket_id in to_complete:
            for task in store.tasks:
                if (
                    task.ticket_id == ticket_id
                    and task.client == session.client
                    and task.status == QueueItemStatus.PENDING
                ):
                    if _is_never_claimed(task):
                        refused.append(
                            (ticket_id, session, task.lane, task.ever_spawned)
                        )
                        break
                    # Why: PR URL is not retrieved here — a second gh call is
                    # disproportionate for this recovery path; disposition alone
                    # is enough for the operator to identify these as shipped.
                    transition_task_status(
                        task, QueueItemStatus.COMPLETED, disposition="shipped"
                    )
                    _stamp_salvage_stage(task)
                    completed_ids.append(ticket_id)
                    changed = True
                    break
        if changed:
            save_dev_queue(store)

    # Phase 4: Emit decision-trace events after the lock releases.
    for session, ticket_id in to_complete:
        if ticket_id in completed_ids:
            record_event(
                OrchestratorEventType.SESSION_COMPLETED,
                {
                    "session_id": session.id,
                    "session_name": session.name,
                    "client": session.client,
                    "ticket_id": ticket_id,
                    "claude_session_id": session.claude_session_id,
                    "crashed": False,
                    "salvaged": True,
                    "reason": _TIMED_OUT_MERGED_REASON,
                },
                correlation_id=ticket_id,
            )
    _emit_never_claimed_refusals(refused)

    return completed_ids
