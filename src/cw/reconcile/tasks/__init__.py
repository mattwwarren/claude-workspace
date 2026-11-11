"""Dev-queue task-revert backstops for reconcile.

Recovers RUNNING TicketTasks whose owning session is already terminal
(TIMED_OUT or DAEMON-COMPLETED), and auto-completes PENDING tasks whose
TIMED_OUT session's PR merged. See GitHub #421, #488, #637.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    GATE_TIMEOUT_BLOCKER_REASON,
    STALE_DISPATCH_BLOCKER_REASON,
)
from cw.config import load_clients, load_orchestrator_config, load_state
from cw.dev_queue import (
    GATE_TIMEOUT_DISPOSITION,
    STALE_DISPATCH_DISPOSITION,
    _stamp_salvage_stage,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dev_queue.gate_backoff import clear_gate_block_wait
from cw.events import advance_cursor, read_events, record_event
from cw.gh import (
    _GH_PR_STATE_CLOSED,
    _GH_PR_STATE_MERGED,
    TIMED_OUT_MERGED_LOOKBACK_DAYS,
    resolve_merged_via_pr_state,
)
from cw.models import (
    DevQueueStore,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionStatus,
    TicketTask,
    resolve_feature_branch,
)
from cw.pr_hydrate import _parse_pr_url
from cw.queue_rows import (
    _AUTOMERGE_NOT_ARMED_REASON,
    _PRIOR_PIPELINE_PR_OPEN_REASON,
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
    _resolve_task_policy,
)
from cw.reconcile.tasks.backstops import (
    _COMPLETION_ROUTING_GRACE_SECONDS,
    _revert_running_tasks_for_sessions,
    _revert_terminal_backstop,
    _sessions_past_completion_grace,
    backstop_targets,
    capture_backstop_dirty_checks,
    revert_completed_silent_tasks,
    revert_timed_out_tasks,
)
from cw.reconcile.tasks.terminal_siblings import (
    _TERMINAL_SIBLING_STATUSES,
    _latest_terminal_at_index,
    park_terminal_sibling_tasks,
)

if TYPE_CHECKING:
    from cw.models import ClientConfig, Session

__all__ = [
    "_COMPLETION_ROUTING_GRACE_SECONDS",
    "_PROPOSED_ACTION_VARIANT_A",
    "_PROPOSED_ACTION_VARIANT_B",
    "_STALE_GATE_CONSUMER",
    "_TERMINAL_SIBLING_STATUSES",
    "_VARIANT_A_BLOCKED_DISPOSITION",
    "_VARIANT_A_MERGE_PENDING_DISPOSITION",
    "_VARIANT_B_DISPOSITION",
    "_client_cwd",
    "_closed_pr_numbers_by_client",
    "_collect_timed_out_merged_candidates",
    "_emit_never_claimed_refusals",
    "_filter_merged_candidates",
    "_index_variant_a_candidates",
    "_is_dangling_client",
    "_is_never_claimed",
    "_is_variant_a_gate_task",
    "_is_variant_b_gate_task",
    "_latest_terminal_at_index",
    "_merged_pr_numbers_by_client",
    "_pr_numbers_by_client_in_state",
    "_release_variant_a",
    "_release_variant_b",
    "_resolve_task_policy",
    "_revert_running_tasks_for_sessions",
    "_revert_terminal_backstop",
    "_sessions_past_completion_grace",
    "backstop_targets",
    "capture_backstop_dirty_checks",
    "complete_timed_out_merged_tasks",
    "park_terminal_sibling_tasks",
    "release_stale_gated_tasks",
    "revert_completed_silent_tasks",
    "revert_timed_out_tasks",
]


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


# ---------------------------------------------------------------------------
# release_stale_gated_tasks (GitHub #1713)
# ---------------------------------------------------------------------------

# Consumer cursor name for the Variant A (own-PR-merged) event stream.
# Independent of orchestrate.py's _RETIREMENT_CONSUMER ("orchestrate_retire")
# -- that consumer retires Session/PRDispatchRecord state for the manual
# ``cw orchestrate retire`` command and never touches a TicketTask; this one
# is a distinct consumer over the SAME PR_MERGED event stream, read from the
# automatic per-tick dispatch loop (GitHub #1713 root-cause chain).
_STALE_GATE_CONSUMER = "dev_queue_stale_gate"

# Variant A gate-class markers: a BLOCKED_ON_USER row parked behind its OWN
# PR's merge/CI gate. "merge_pending" is the disposition Rule 3b stamps
# verbatim (dispatch/routing.py); "blocked"/"automerge_not_armed" is Rule 5's
# stamp for a failed auto-merge arm on an already-created PR (both parks
# populate task.pr_url -- merge_pending directly, automerge_not_armed via the
# pr_info fallback threaded in dispatch/routing.py). #3372 adds a third pair,
# GATE_TIMEOUT_DISPOSITION/GATE_TIMEOUT_BLOCKER_REASON: a reuse-path gate the
# foreground timeout cut off parks with auto-merge left as found and pr_url
# stamped from pr_info, so a PR that merges on its own releases the row.
#
# The "automerge_not_armed" / "prior_pipeline_pr_open" reason literals
# themselves are NOT re-declared here -- _is_variant_a_gate_task and
# _is_variant_b_gate_task below import them from cw.queue_rows, the same
# objects cw.dispatch.routing (the sole producer that stamps
# task.blocked_reason with these values) imports, so a locally re-declared
# copy can never drift from the producer with no compiler signal. The leaf
# imports nothing from cw.dispatch, so the import is module-scope (#2613).
_VARIANT_A_MERGE_PENDING_DISPOSITION = "merge_pending"
_VARIANT_A_BLOCKED_DISPOSITION = "blocked"

# Variant B gate-class markers: a BLOCKED_ON_USER row with no PR of its own,
# parked behind a DIFFERENT ticket's open PR (dispatch/routing.py Rule 5,
# merge_gate_blocked/prior_pipeline_pr_open) -- see _is_variant_b_gate_task's
# docstring for the full two-producer contract (GitHub #1902 widens this to
# a second disposition/reason pair, made production-reachable by #1927).
_VARIANT_B_DISPOSITION = "merge_gate_blocked"

_PROPOSED_ACTION_VARIANT_A = "release_stale_gate_variant_a"
_PROPOSED_ACTION_VARIANT_B = "release_stale_gate_variant_b"


def _is_variant_a_gate_task(task: TicketTask) -> bool:
    """True iff *task* is a BLOCKED_ON_USER row parked behind its own PR gate."""
    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        return False
    if task.disposition == _VARIANT_A_MERGE_PENDING_DISPOSITION:
        return True
    if (
        task.disposition == GATE_TIMEOUT_DISPOSITION
        and task.blocked_reason == GATE_TIMEOUT_BLOCKER_REASON
    ):
        return True
    return (
        task.disposition == _VARIANT_A_BLOCKED_DISPOSITION
        and task.blocked_reason == _AUTOMERGE_NOT_ARMED_REASON
    )


def _is_variant_b_gate_task(task: TicketTask) -> bool:
    """True iff *task* is a BLOCKED_ON_USER row parked behind ANOTHER
    ticket's PR -- either dispatch's own Rule 5 merge gate
    (disposition="merge_gate_blocked", GitHub #1713) or an agent-reported
    stale_dispatch sentinel (disposition=STALE_DISPATCH_DISPOSITION,
    GitHub #1862/#1902 fast-follow). Both stamp task.blocked_on_pr via the
    identical _extract_blocked_on_pr regex over blocker.details.

    The two producers reach their blocking PR's state by different routes.
    prior_pipeline_pr_open's PR belongs to a DIFFERENT ticket, whose own
    store row carries it as a pr_url for the task scan in the merged/closed
    cross-reference (_merged_pr_numbers_by_client /
    _closed_pr_numbers_by_client, GitHub #1920) to poll. stale_dispatch's PR
    is this ticket's OWN earlier dispatch, found by a live `gh pr list --head`
    query
    that writes no pr_url anywhere -- so #1927 gives it an independent
    source instead: cw.reconcile.stale_dispatch_watch registers it as a
    client-tagged WatchedPr, which hydrates on the same serve tick and folds
    into the same per-client index. (Before #1927 this branch was
    unit-test-reachable but production-unreachable -- see #1902 R3.)
    """
    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        return False
    if task.blocked_on_pr is None:
        return False
    if task.disposition == _VARIANT_B_DISPOSITION:
        return task.blocked_reason == _PRIOR_PIPELINE_PR_OPEN_REASON
    return (
        task.disposition == STALE_DISPATCH_DISPOSITION
        and task.blocked_reason == STALE_DISPATCH_BLOCKER_REASON
    )


def _index_variant_a_candidates(
    store: DevQueueStore,
) -> dict[tuple[str, str], TicketTask]:
    """(client, ticket_id) -> task for every current Variant A gate-class row."""
    return {
        (t.client, t.ticket_id): t for t in store.tasks if _is_variant_a_gate_task(t)
    }


def _pr_numbers_by_client_in_state(
    store: DevQueueStore, pr_state_value: str
) -> dict[str, set[int]]:
    """client -> set of PR numbers this store has observed in *pr_state_value*.

    Two independent sources, unioned per client:

    * every task's hydrated ``pr_state`` (populated by cw.pr_hydrate's
      per-tick poll, independent of the PR_MERGED event stream Variant A
      consumes) -- reaches a blocking PR that some OTHER ticket's row owns;
    * every client-tagged ``WatchedPr`` (GitHub #1927), hydrated on the same
      tick by ``_hydrate_watched_prs`` -- reaches a blocking PR that NO task
      row owns, which is exactly the ``stale_dispatch`` park's own earlier
      self-PR (see ``cw.reconcile.stale_dispatch_watch``, the producer).

    Both are scoped per-client, matching the Self-Verified Premise that a
    dev-queue client is bound to exactly one repo, so a bare PR number is
    unambiguous within one client. A ``client is None`` watch (the webhook/cli
    operator-review producers) carries no client context and is therefore
    excluded rather than attributed to a guessed client (#1269).
    """
    by_client: dict[str, set[int]] = {}
    for t in store.tasks:
        if t.pr_state is None or t.pr_state.state != pr_state_value:
            continue
        if not t.pr_url:
            continue
        parsed = _parse_pr_url(t.pr_url)
        if parsed is None:
            continue
        _, pr_number = parsed
        by_client.setdefault(t.client, set()).add(pr_number)
    for w in store.watched_prs:
        if w.client is None:
            continue
        if w.pr_state is None or w.pr_state.state != pr_state_value:
            continue
        by_client.setdefault(w.client, set()).add(w.pr_number)
    return by_client


def _merged_pr_numbers_by_client(store: DevQueueStore) -> dict[str, set[int]]:
    """client -> set of PR numbers this store has observed MERGED (via pr_state).

    See ``_pr_numbers_by_client_in_state`` for the two-source contract.
    """
    return _pr_numbers_by_client_in_state(store, _GH_PR_STATE_MERGED)


def _closed_pr_numbers_by_client(store: DevQueueStore) -> dict[str, set[int]]:
    """client -> set of PR numbers this store has observed CLOSED unmerged
    (via pr_state). GitHub #1920: a Variant B park's blocking PR that is
    CLOSED without merging must release the park identically to a MERGED
    one -- see ``_pr_numbers_by_client_in_state`` for the shared two-source
    contract.
    """
    return _pr_numbers_by_client_in_state(store, _GH_PR_STATE_CLOSED)


def _release_variant_a(task: TicketTask, policy: ReapPolicy) -> None:
    """Apply the SIGNAL_ONLY/AUTO release for a Variant A gate task."""
    if policy is ReapPolicy.AUTO:
        # Why: this row's PR is confirmed MERGED (own-PR event) or already
        # created (automerge_not_armed, gate_timeout #3372) -- mirrors
        # complete_timed_out_merged_tasks's identical disposition="shipped"
        # for the same "PR merged, no further gh call needed" shape.
        transition_task_status(task, QueueItemStatus.COMPLETED, disposition="shipped")
        task.session_id = None
    else:
        task.stale_gate_detected_at = datetime.now(UTC)


def _release_variant_b(task: TicketTask, policy: ReapPolicy) -> None:
    """Apply the SIGNAL_ONLY/AUTO release for a Variant B gate task."""
    if policy is ReapPolicy.AUTO:
        transition_task_status(task, QueueItemStatus.PENDING)
        task.session_id = None
        # Why: the release fired on MERGED/CLOSED evidence, so the gate cleared
        # and its backoff wait goes too (#2717); evidence-driven, ADR-0014 inv 1.
        clear_gate_block_wait(task)
    else:
        task.stale_gate_detected_at = datetime.now(UTC)


def release_stale_gated_tasks() -> list[str]:
    """Re-validate BLOCKED_ON_USER dev-queue rows against a cleared PR gate.

    GitHub #1713: dev-queue rows parked behind a merge/CI gate never observe
    that gate clearing -- ``hydrate_pr_states`` emits ``pr.merged`` events
    every tick, but nothing before this function consumed them against
    dev-queue task state. Two independent variants (see the ticket's
    root-cause chain):

    * Variant A -- the row is blocked behind ITS OWN PR (disposition
      "merge_pending", "blocked"/blocked_reason="automerge_not_armed", or
      "gate_timeout"/blocked_reason="gate_timeout", #3372).
      Detected by consuming ``PR_MERGED`` events off a dedicated consumer
      cursor (``_STALE_GATE_CONSUMER``) and matching each event's
      ``(client, ticket_id)`` against the current Variant A candidate set.
    * Variant B -- the row is blocked behind a DIFFERENT ticket's PR
      (disposition "merge_gate_blocked"/blocked_reason=
      "prior_pipeline_pr_open", ``blocked_on_pr`` carrying the blocking PR's
      bare number) *or* behind its own earlier, un-harvested-sentinel PR
      (disposition ``STALE_DISPATCH_DISPOSITION``/blocked_reason=
      "pr_already_open", GitHub #1902 fast-follow to #1862). No event stream
      names this row directly, so it is found by a dev-queue-wide
      cross-reference scan against every OTHER task's hydrated ``pr_state``
      within the same client -- plus, since #1927, against every
      client-tagged ``WatchedPr``, which is what makes the stale_dispatch
      producer reachable at all (its self-PR lives on no task row; see
      ``cw.reconcile.stale_dispatch_watch``). Since GitHub #1920, the
      blocking PR's state matches on either MERGED or CLOSED-unmerged --
      both leave the specific condition Variant B parks on (the OTHER PR
      still being OPEN) equally false, so both release identically to
      PENDING.

    Per ``park_terminal_sibling_tasks``'s exact precedent (ADR-0006):
    ``SESSION_REAP_PROPOSED`` fires on EVERY detection regardless of
    ``reap_policy`` -- only the status mutation differs.
    ``ReapPolicy.SIGNAL_ONLY`` (default): stamp ``stale_gate_detected_at``,
    leave ``task.status`` untouched. ``ReapPolicy.AUTO``: perform the
    variant-specific release (Variant A -> COMPLETED/"shipped", Variant B ->
    PENDING requeue) via ``transition_task_status``, whose unconditional
    latch-clear then clears ``stale_gate_detected_at`` in the same call.

    Residual blind spot (binding operator resolution, not closed here):
    Variant B is cross-reference-only -- a blocking PR that merged or closed
    and then left the queue entirely (its task row removed/never tracked) has
    no ``pr_state`` for this scan to match against, so that row stays parked
    until the orchestrator requeues or closes it. No ``gh pr view`` fallback is
    attempted.

    Does NOT require ``sessions_lock`` -- operates on the dev queue and the
    events inbox only. Returns the list of ticket_ids released or stamped.
    """
    clients = load_clients()
    orchestrator_config = load_orchestrator_config()
    events = read_events(
        consumer=_STALE_GATE_CONSUMER,
        event_types=[OrchestratorEventType.PR_MERGED],
    )

    released_ids: list[str] = []
    # Snapshot fields needed for event emission after the lock (emitting
    # after release is a choice ADR-0019 permits). session_id is
    # captured BEFORE _release_variant_a/_release_variant_b run (both clear
    # task.session_id under the AUTO branch) -- mirrors
    # park_terminal_sibling_tasks's orig_session_id precedent exactly.
    pending_events: list[tuple[str, str, str, str, str | None]] = []

    # PR_MERGED event IDs processed by the Variant A loop below, deferred
    # until AFTER save_dev_queue succeeds (see the advance_cursor loop below
    # this `with` block for why).
    processed_event_ids: list[str] = []

    with dev_queue_lock():
        store = load_dev_queue()
        changed = False

        # Variant A: consume PR_MERGED events off the dedicated cursor.
        variant_a_index = _index_variant_a_candidates(store)
        for event in events:
            payload = event.payload
            client = str(payload.get("client", ""))
            ticket_id = str(payload.get("ticket_id", ""))
            task = variant_a_index.get((client, ticket_id))
            # Re-check gate-class membership at process time (not just index
            # membership): defends against a task mutated earlier in this
            # same batch (e.g. two events resolving to the same row) so a
            # stale index entry can never double-release.
            if task is not None and _is_variant_a_gate_task(task):
                orig_session_id = task.session_id
                policy = _resolve_task_policy(
                    task.client, task.lane, clients, orchestrator_config
                )
                _release_variant_a(task, policy)
                released_ids.append(task.ticket_id)
                pending_events.append(
                    (
                        task.ticket_id,
                        task.client,
                        task.lane,
                        _PROPOSED_ACTION_VARIANT_A,
                        orig_session_id,
                    )
                )
                changed = True
            processed_event_ids.append(event.id)

        # Variant B: dev-queue-wide cross-reference scan (no event stream
        # names these rows directly -- see docstring).
        merged_by_client = _merged_pr_numbers_by_client(store)
        closed_by_client = _closed_pr_numbers_by_client(store)
        for task in store.tasks:
            if not _is_variant_b_gate_task(task):
                continue
            if task.blocked_on_pr not in merged_by_client.get(
                task.client, set()
            ) and task.blocked_on_pr not in closed_by_client.get(task.client, set()):
                continue
            orig_session_id = task.session_id
            policy = _resolve_task_policy(
                task.client, task.lane, clients, orchestrator_config
            )
            _release_variant_b(task, policy)
            released_ids.append(task.ticket_id)
            pending_events.append(
                (
                    task.ticket_id,
                    task.client,
                    task.lane,
                    _PROPOSED_ACTION_VARIANT_B,
                    orig_session_id,
                )
            )
            changed = True

        if changed:
            save_dev_queue(store)

        # Advance cursors only AFTER the mutation batch above is durably
        # persisted (crash-safety fix, GitHub #1713 review). Advancing the
        # cursor inside the Variant A loop (the pre-fix shape) permanently
        # consumed a PR_MERGED event before its corresponding release was
        # written -- a crash, or a save_dev_queue failure (disk full, lock
        # I/O), between the two left the task stuck BLOCKED_ON_USER forever
        # with no future trigger, reproducing inside this very mechanism the
        # exact silent-staleness failure class #1713 exists to close.
        # PR_MERGED fires once per PR (first-observation dedup), so there is
        # no second chance.
        #
        # If save_dev_queue raises above, this loop never runs: the
        # exception propagates out of this `with dev_queue_lock():` block
        # (releasing the lock), up through release_stale_gated_tasks(), to
        # _run_stale_gate_release_guarded's broad-catch wrapper in
        # dispatch/loop.py, which logs and retries next tick. The cursor was
        # never advanced, so the retry re-reads the same events and
        # re-derives the same mutation -- safe to repeat because the on-disk
        # dev-queue state is unchanged when save_dev_queue itself is what
        # failed (no partial write reaches disk; see atomic_write_text).
        for event_id in processed_event_ids:
            advance_cursor(_STALE_GATE_CONSUMER, event_id)

    # Emit events after dev_queue_lock releases (ADR-0019 permits either).
    for ticket_id, client, lane, proposed_action, session_id in pending_events:
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            {
                "session_id": session_id,
                "session_name": None,
                "client": client,
                "ticket_id": ticket_id,
                "lane": lane,
                "proposed_action": proposed_action,
                "reason": ReapReason.STALE_GATE.value,
                "evidence": {},
            },
            correlation_id=ticket_id,
        )

    return released_ids
