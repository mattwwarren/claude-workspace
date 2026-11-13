"""Candidate screening and the atomic claim transaction.

Everything that decides, under ``dev_queue_lock()``, whether a PENDING row is
claimed: the precomputed worktree-occupancy screen (#2077), spawn-error
backoff, the fix-dispatch hold, the pre-dispatch open-PR gate (#1862), the
pre-dispatch tracker-MCP gate (#2442), the lane-resolved attempt ceiling, and
:func:`_claim_next_pending` itself.
Extracted verbatim from the historical flat ``cw.dispatch.claim`` module by
the package split (#2378).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.dev_queue import (
    ADOPTED_HEAD_DRIFT_DISPOSITION,
    REVIEW_APPROVAL_GATE_DISPOSITION,
    STALE_DISPATCH_GATE_DISPOSITION,
    TRACKER_MCP_GATE_DISPOSITION,
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.dev_queue.adoption import REPIN_ADOPTED_HEAD_FLAG
from cw.dev_queue.dispositions import (
    _PRE_DISPATCH_ADOPTED_HEAD_DRIFT_REASON,
    _PRE_DISPATCH_REVIEW_APPROVAL_REASON,
    _PRE_DISPATCH_STALE_PR_REASON,
    _PRE_DISPATCH_TRACKER_MCP_REASON,
)
from cw.dev_queue.gate_backoff import clear_gate_block_wait, gate_wait_released
from cw.dev_queue.next_commands import dev_queue_command
from cw.dispatch.claim.events import (
    _emit_attempt_cap_attention_event,
    _emit_attempt_cap_blocked_event,
    _emit_review_approval_gate_attention_event,
    _emit_stale_dispatch_attention_event,
    _emit_stale_dispatch_blocked_event,
    _emit_tracker_mcp_gate_attention_event,
    _emit_tracker_mcp_gate_blocked_event,
    _emit_worktree_occupied_skip_event,
    emit_adoption_attention_event,
)
from cw.launch_marker import current_launch_marker
from cw.models import QueueItemStatus, Stage, resolve_feature_branch
from cw.queue_rows import _is_fix_dispatch_held
from cw.reconcile import resolve_attempt_ceiling
from cw.worktree import live_home_reason, worktree_path_for

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cw.dispatch.adopted_head_gate import AdoptedHeadDrift
    from cw.dispatch.review_approval_gate import ReviewApprovalHold
    from cw.dispatch.tracker_mcp_gate import TrackerMcpGateHit
    from cw.models import (
        ClientConfig,
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
    )
    from cw.native_daemon import NativeDaemonClient
    from cw.worktree import UnresolvablePathWarningKey

_log = logging.getLogger("cw.dispatch")


def _park_stale_pr_task(
    task: TicketTask,
    client_name: str,
    lane: str,
    store: DevQueueStore,
    found_pr_url: str | None,
) -> None:
    """Park one gate-hit task BLOCKED_ON_USER and emit both signals (#1862).

    ``unproductive=False``: no session was ever spawned for this claim, so
    there is no RUNNING exit to charge — and charging it would eventually
    re-park the row at ``attempt_cap_blocked``, burying the specific,
    actionable ``stale_dispatch_gate`` signal behind a generic one.

    Records the PR the gate saw (#2740): *found_pr_url* is stamped as the
    row's ``pr_url`` only when the row has none. A pre-existing URL is passed
    through explicitly because the seam assigns ``pr_url`` unconditionally on
    a BLOCKED_ON_USER transition (same technique as ``_park_lost_rows``).
    When a URL is newly stamped, any ``pr_state`` on the row is reset to None:
    requeue clears ``pr_url`` but not ``pr_state``, so a terminal state
    hydrated for an earlier PR would otherwise pair with the new URL (hiding
    it from ``pr_hydrate`` and mislabelling it in the reconcile PR index).
    Clearing it in the shared lifecycle reset is the separately tracked
    follow-up.

    Extracted rather than inlined because both claim loops (priority and plain)
    need the identical five-step sequence; keeping it here also holds
    ``_claim_next_pending`` inside its PLR branch/statement budget.
    """
    stamps_new_url = not task.pr_url and found_pr_url is not None
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=STALE_DISPATCH_GATE_DISPOSITION,
        blocked_reason=_PRE_DISPATCH_STALE_PR_REASON,
        pr_url=task.pr_url or found_pr_url,
        unproductive=False,
    )
    if stamps_new_url:
        task.pr_state = None
    save_dev_queue(store)
    _emit_stale_dispatch_blocked_event(client_name, task.ticket_id)
    _emit_stale_dispatch_attention_event(task, client_name, lane)


def _is_stale_pr_gated(
    task: TicketTask, stale_pr_ticket_ids: Mapping[str, str | None]
) -> bool:
    """True iff *task* is a PLAN/IMPL-stage row the open-PR gate holds (#1862).

    Stage-scoped at the point of use, not only at resolution time: a REVIEW or
    FINALIZE row legitimately has an open PR (that is the artifact under
    review), so the gate must never hold one even if a stale set from an
    earlier stage still names its ticket id.
    """
    return (
        task.stage in (Stage.PLAN, Stage.IMPL) and task.ticket_id in stale_pr_ticket_ids
    )


def _park_tracker_mcp_gated_task(
    task: TicketTask,
    client_name: str,
    lane: str,
    store: DevQueueStore,
    hit: TrackerMcpGateHit,
) -> None:
    """Park one tracker-MCP gate hit BLOCKED_ON_USER and emit both signals (#2442).

    ``unproductive=False`` for the same reason as :func:`_park_stale_pr_task`:
    no session was ever spawned for this claim. *hit* names the branch, the
    file inspected, the expected plugin id or MCP server, and the backend
    (#3055), carried on the
    SESSION_NEEDS_ATTENTION event only (transient, event-only detail).
    """
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=TRACKER_MCP_GATE_DISPOSITION,
        blocked_reason=_PRE_DISPATCH_TRACKER_MCP_REASON,
        unproductive=False,
    )
    save_dev_queue(store)
    _emit_tracker_mcp_gate_blocked_event(client_name, task.ticket_id)
    _emit_tracker_mcp_gate_attention_event(
        task,
        client_name,
        lane,
        branch=hit.branch,
        file_inspected=hit.file_inspected,
        expected=hit.expected,
        backend=hit.backend,
    )


def _is_tracker_mcp_gated(
    task: TicketTask, tracker_mcp_gate_hits: Mapping[str, TrackerMcpGateHit]
) -> bool:
    """True iff *task* is a PLAN/IMPL/FINALIZE row the tracker-MCP gate holds (#2442).

    Stage-scoped at the point of use, like :func:`_is_stale_pr_gated`: a row
    that advanced past FINALIZE since the lock-free snapshot is never held on
    a stale hit. Hits only ever exist for a codex FINALIZE row (RFC 0018 C3).
    """
    return (
        task.stage in (Stage.PLAN, Stage.IMPL, Stage.FINALIZE)
        and task.ticket_id in tracker_mcp_gate_hits
    )


def _live_adopted_head_drift(
    task: TicketTask, adopted_head_drifts: Mapping[str, AdoptedHeadDrift]
) -> AdoptedHeadDrift | None:
    """The drift hit that still applies to *task* under the lock (#2943).

    A hit is ignored when the row's pin advanced since the lock-free snapshot
    (``hit.pinned`` no longer the pin) or the row was flagged for a
    session-end settle since then: the next tick's settle decides it.
    """
    hit = adopted_head_drifts.get(task.ticket_id)
    if hit is None or task.adopted_head_sha != hit.pinned or task.adopted_repin_due:
        return None
    return hit


def _park_adopted_head_drift(
    task: TicketTask,
    client_name: str,
    lane: str,
    store: DevQueueStore,
    hit: AdoptedHeadDrift,
) -> None:
    """Park an adopted row whose origin head moved off its pin (RFC 0016 R5).

    ``unproductive=False`` like the sibling pre-dispatch parks: no session
    ran. The pin is untouched; ``cw dev-queue requeue --repin-adopted-head``
    is the only release, and the attention event prints the range.
    """
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=ADOPTED_HEAD_DRIFT_DISPOSITION,
        blocked_reason=_PRE_DISPATCH_ADOPTED_HEAD_DRIFT_REASON,
        unproductive=False,
    )
    save_dev_queue(store)
    _log.warning(
        "dispatch: adopted-head drift park %s/%s branch=%s pinned=%s observed=%s",
        client_name,
        task.ticket_id,
        hit.branch,
        hit.pinned,
        hit.observed,
    )
    emit_adoption_attention_event(
        task,
        client_name,
        lane,
        paused_status=ADOPTED_HEAD_DRIFT_DISPOSITION,
        branch=hit.branch,
        observed_head_sha=hit.observed,
        commit_range=hit.commit_range,
        details=(
            f"origin/{hit.branch} moved {hit.pinned} -> {hit.observed} outside a"
            " cw session; review the range, then"
            f" `{dev_queue_command('requeue', task, REPIN_ADOPTED_HEAD_FLAG)}`"
        ),
    )


def _live_review_approval_hold(
    task: TicketTask, holds: Mapping[str, ReviewApprovalHold]
) -> ReviewApprovalHold | None:
    """The review-approval hit that still applies to *task* under the lock (#2773).

    A hit is ignored when the row left PENDING FINALIZE or its approval fields
    changed since the lock-free snapshot the hit judged (a fresh approval, a
    cleared one): the next tick's precompute decides it.
    """
    hit = holds.get(task.ticket_id)
    if hit is None or task.stage != Stage.FINALIZE:
        return None
    kind = task.review_approver_kind
    if (
        task.status != QueueItemStatus.PENDING
        or (None if kind is None else str(kind)) != hit.approver_kind
        or task.review_approved_sha != hit.approved_sha
        or (task.review_approved_at is not None) != hit.approved_at_set
    ):
        return None
    return hit


def _park_review_approval_hold(
    task: TicketTask,
    client_name: str,
    lane: str,
    store: DevQueueStore,
    hit: ReviewApprovalHold,
) -> None:
    """Park a large FINALIZE row whose review approval is not valid (#2773).

    ``unproductive=False`` like the sibling pre-dispatch parks: no session ran,
    so there is no RUNNING exit to charge. The release is ``requeue --stage
    review``, which the attention event names.
    """
    transition_task_status(
        task,
        QueueItemStatus.BLOCKED_ON_USER,
        disposition=REVIEW_APPROVAL_GATE_DISPOSITION,
        blocked_reason=_PRE_DISPATCH_REVIEW_APPROVAL_REASON,
        unproductive=False,
    )
    save_dev_queue(store)
    _log.warning(
        "dispatch: review-approval gate park %s/%s branch=%s token=%s: %s",
        client_name,
        task.ticket_id,
        hit.branch,
        hit.token,
        hit.detail,
    )
    _emit_review_approval_gate_attention_event(
        task, client_name, lane, branch=hit.branch, token=hit.token, detail=hit.detail
    )


def _park_if_pre_dispatch_gated(
    task: TicketTask,
    *,
    client_name: str,
    lane: str,
    stale_pr_ticket_ids: Mapping[str, str | None],
    tracker_mcp_gate_hits: Mapping[str, TrackerMcpGateHit],
    adopted_head_drifts: Mapping[str, AdoptedHeadDrift],
    review_approval_holds: Mapping[str, ReviewApprovalHold],
    store: DevQueueStore,
) -> bool:
    """Park *task* under the first pre-dispatch gate that holds it; report it.

    Stale-PR gate (#1862) first, then the tracker-MCP gate (#2442), then the
    adopted-head drift gate (#2943), then the large-tier review-approval gate
    (#2773; a deferral hold returns True with no write, like the settle-flag
    skip above, leaving the row PENDING). Grouped so :func:`_screen_and_claim`
    stays inside its PLR0911 return budget.
    """
    # The session-end settle may have been marked after the lock-free
    # precompute. Re-check the durable marker under the claim lock so a newly
    # flagged adopted row cannot slip through before the next tick settles it.
    if task.adopted_head_sha is not None and task.adopted_repin_due:
        return True
    if _is_stale_pr_gated(task, stale_pr_ticket_ids):
        _park_stale_pr_task(
            task, client_name, lane, store, stale_pr_ticket_ids[task.ticket_id]
        )
        return True
    if _is_tracker_mcp_gated(task, tracker_mcp_gate_hits):
        _park_tracker_mcp_gated_task(
            task, client_name, lane, store, tracker_mcp_gate_hits[task.ticket_id]
        )
        return True
    drift = _live_adopted_head_drift(task, adopted_head_drifts)
    if drift is not None:
        _park_adopted_head_drift(task, client_name, lane, store, drift)
        return True
    approval = _live_review_approval_hold(task, review_approval_holds)
    if approval is not None:
        if not approval.defer:
            _park_review_approval_hold(task, client_name, lane, store, approval)
        return True
    return False


def resolve_occupied_ticket_ids(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    daemon: NativeDaemonClient,
    warned_unresolvable: set[UnresolvablePathWarningKey] | None = None,
) -> dict[str, str]:
    """Ticket ids (with occupancy reason) whose per-ticket worktree a live
    session/worker already holds (#2077).

    Resolved once per client per tick, lock-free, BEFORE any row is
    claimed -- mirrors the #1862 stale-PR-ticket-ids precompute
    (cw.dispatch.pr_gate.resolve_stale_pr_ticket_ids): _claim_next_pending
    runs under dev_queue_lock() and must make no I/O of its own. Unlike
    that sibling this makes no gh call -- only the same local reads (cw
    session state, the daemon roster) create_worktree's reuse-refresh and
    the stale-worktree claim handler already consult via
    live_home_reason. Its fleet-wide feature-flag escape hatch is
    ``OrchestratorConfig.occupancy_gate_enabled`` (#2396), with a staged
    per-client override on ``ClientConfig``; both are resolved in the
    ``cw.dispatch.lanes`` caller exactly as ``pr_gate_enabled`` gates
    ``resolve_stale_pr_ticket_ids`` -- this function's own body and
    signature are unaffected by the toggles.

    Scans every PENDING task for *client* across all lanes and stages
    (occupancy is per-ticket, not per-lane: a ticket's worktree is the
    same path across its whole pipeline). A task with no worktree
    created yet resolves to a nonexistent path that matches no live
    home, so it is never falsely reported occupied.

    Returns the occupancy REASON per ticket (not just membership) so the
    pre-claim screen's warning log can name it, matching the shape the
    post-claim occupancy handlers already log.
    """
    occupied: dict[str, str] = {}
    for task in queue_snapshot.tasks:
        if task.client != client.name or task.status != QueueItemStatus.PENDING:
            continue
        branch = resolve_feature_branch(task.ticket_id, client, task)
        wt_path = worktree_path_for(client, branch)
        reason = live_home_reason(
            wt_path, daemon=daemon, warned_unresolvable=warned_unresolvable
        )
        if reason is not None:
            occupied[task.ticket_id] = reason
    return occupied


# _screen_and_claim outcomes. "skipped" covers every held/parked case the two
# claim loops treat identically (move to the next candidate, no flag raised).
_CLAIM_CLAIMED = "claimed"
_CLAIM_BACKOFF = "backoff"
_CLAIM_SKIPPED = "skipped"


def _screen_and_claim(
    task: TicketTask,
    *,
    client_name: str,
    lane: str,
    client: ClientConfig,
    config: OrchestratorConfig,
    stale_pr_ticket_ids: Mapping[str, str | None],
    occupied_ticket_reasons: dict[str, str],
    tracker_mcp_gate_hits: Mapping[str, TrackerMcpGateHit],
    adopted_head_drifts: Mapping[str, AdoptedHeadDrift],
    review_approval_holds: Mapping[str, ReviewApprovalHold],
    store: DevQueueStore,
    now: datetime,
) -> str:
    """Run one PENDING candidate through the claim gauntlet; report the outcome.

    The identical screen-then-claim sequence both claim loops (priority and
    plain) run per candidate, extracted (#2075) so the fix-dispatch hold could
    be added without pushing ``_claim_next_pending`` past its PLR0912 branch
    budget. Screens in precedence order — worktree occupancy (#2077),
    spawn-error backoff, fix-dispatch hold, stale-PR gate, tracker-MCP gate
    (#2442), attempt ceiling — then claims. Parking paths save the store
    themselves (``_park_if_pre_dispatch_gated`` and the ceiling park below),
    as does the successful claim; a screened-out candidate writes nothing.

    The occupancy screen runs first: it is the most fundamental precondition
    (a second worker must never be spawned into a live worktree) and needs no
    store write -- the row simply stays PENDING, unclaimed and uncharged, for
    the next tick's fresh precompute to reconsider.
    """
    occupied_reason = occupied_ticket_reasons.get(task.ticket_id)
    if occupied_reason is not None:
        branch = resolve_feature_branch(task.ticket_id, client, task)
        wt_path = worktree_path_for(client, branch)
        _emit_worktree_occupied_skip_event(client_name, task.ticket_id)
        _log.warning(
            "dispatch_tick: worktree %s for %s/%s is occupied (%s); leaving"
            " it PENDING for a later tick (not claimed)",
            wt_path,
            client_name,
            task.ticket_id,
            occupied_reason,
        )
        return _CLAIM_SKIPPED
    if task.next_eligible_at is not None and now < task.next_eligible_at:
        # #2717: disabling the rollout releases a gate-owned wait as part of
        # this locked claim transaction, so a later non-gate route cannot
        # retain an orphaned claim clock.
        if gate_wait_released(task, backoff_enabled=client.gate_block_backoff_enabled):
            clear_gate_block_wait(task)
            save_dev_queue(store)
        else:
            return _CLAIM_BACKOFF
    if _is_fix_dispatch_held(task):
        return _CLAIM_SKIPPED
    if _park_if_pre_dispatch_gated(
        task,
        client_name=client_name,
        lane=lane,
        stale_pr_ticket_ids=stale_pr_ticket_ids,
        tracker_mcp_gate_hits=tracker_mcp_gate_hits,
        adopted_head_drifts=adopted_head_drifts,
        review_approval_holds=review_approval_holds,
        store=store,
    ):
        return _CLAIM_SKIPPED
    ceiling = resolve_attempt_ceiling(client, task, config)
    if ceiling is not None and task.unproductive_attempts >= ceiling:
        transition_task_status(
            task,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition="attempt_cap_blocked",
        )
        save_dev_queue(store)
        _emit_attempt_cap_blocked_event(client_name, task.ticket_id, ceiling)
        _emit_attempt_cap_attention_event(task, client_name, lane, ceiling)
        return _CLAIM_SKIPPED
    transition_task_status(task, QueueItemStatus.RUNNING)
    task.attempts += 1
    # #2591: a fresh clock, not *now* (read once at the start of the claim
    # loop): reconcile's adoption skips a session that started before this
    # instant, so an early stamp would let a stale one through.
    task.claimed_at = datetime.now(UTC)
    # #2615: after the transition, whose clear block drops any old marker.
    # A local /proc read via psutil, not a subprocess (ADR-0019); None when
    # this process's start time is unreadable, which stores no marker.
    task.launch_marker = current_launch_marker()
    save_dev_queue(store)
    return _CLAIM_CLAIMED


def _claim_next_pending(
    client_name: str,
    *,
    lane: str,
    client: ClientConfig,
    config: OrchestratorConfig,
    priority_ticket_ids: list[str] | None = None,
    usage_limited_until: datetime | None = None,
    stale_pr_ticket_ids: Mapping[str, str | None] | None = None,
    occupied_ticket_reasons: dict[str, str] | None = None,
    tracker_mcp_gate_hits: Mapping[str, TrackerMcpGateHit] | None = None,
    adopted_head_drifts: Mapping[str, AdoptedHeadDrift] | None = None,
    review_approval_holds: Mapping[str, ReviewApprovalHold] | None = None,
) -> tuple[TicketTask | None, bool]:
    """Atomically claim the next PENDING task for a client in a specific lane.

    Acquires the dev-queue file lock, loads the queue, marks the first
    PENDING task for *client_name* in *lane* as RUNNING, saves, and returns it.
    Returns (None, spawn_backoff_skipped) if no pending task exists or all
    eligible tasks are in spawn_error backoff.

    If *priority_ticket_ids* is provided, prefer claiming PENDING tasks in
    that order (only those whose ticket_id appears in the list).  Tasks not
    referenced by the list are skipped at this stage; they will be claimed
    by subsequent ticks once the prioritised tasks are exhausted (the
    parameter is intentionally a *preference*, not a filter — see the
    fallback after the priority loop).

    Attempt ceiling: if task.unproductive_attempts >= the ceiling resolved for
    the task's lane, the task is parked BLOCKED_ON_USER instead of claimed. A
    dispatch.tick event with skip_reason=ATTEMPT_CAP_BLOCKED is emitted per
    parked task for observability. See GitHub #786.

    The ceiling is resolved per-lane by
    :func:`cw.reconcile.resolve_attempt_ceiling` (#1751), which takes *client*
    for that lookup and falls back to ``config.global_attempt_ceiling``.
    A lane that sets ``attempt_ceiling: false`` resolves to ``None``, meaning
    no ceiling at all — a supervised lane's operator answers every park and is
    itself the rate limiter, so the automated bound buys nothing there. The
    concierge's recovery recipes call the same resolver, so neither layer can
    refuse work the other would have allowed.

    The ceiling reads ``unproductive_attempts``, NOT raw ``attempts`` (GitHub
    #1750): every claim still increments ``attempts`` below, but only claims
    that exited RUNNING without evidence of progress are charged against the
    ceiling. A ticket doing real work across many stages (#1727) therefore no
    longer parks itself, while a crashloop that produces nothing (#1653) still
    reaches the cap at exactly the same rate as before.

    *usage_limited_until*: when set and still in the future, returns
    ``(None, False)`` immediately without claiming anything (#1346
    defense-in-depth — see the gate below for why this is a parameter, not a
    fresh read).

    *stale_pr_ticket_ids* (GitHub #1862): ticket id -> open-PR URL (or None)
    for every ticket whose feature branch already carries an open, unmerged
    PR. A PLAN/IMPL-stage task named here is parked BLOCKED_ON_USER with
    ``disposition=stale_dispatch_gate`` instead of claimed, so a dispatch whose
    PR landed but whose queue row was never advanced is not silently
    re-implemented by a second worker; the park records the URL as the row's
    ``pr_url`` when the row has none (#2740). Resolved once per client per
    tick by ``cw.dispatch.pr_gate.resolve_stale_pr_ticket_ids`` and passed in
    precomputed for the same reason *usage_limited_until* is: this function
    runs under ``dev_queue_lock()`` and must make no network call. ``None``
    (the default) screens nothing, so any caller that has not resolved the
    gate simply keeps today's behaviour.

    *occupied_ticket_reasons* (GitHub #2077): ticket id -> occupancy reason for
    every ticket whose per-ticket worktree a live session or daemon worker
    already holds. A task named here is left PENDING -- never claimed, no
    attempt charged, no backoff stamped -- instead of being claimed and then
    released when ``create_worktree`` or the hook-context write discovers the
    conflict. Resolved once per client per tick by
    :func:`resolve_occupied_ticket_ids` and passed in precomputed for the same
    no-I/O-under-the-lock reason as *stale_pr_ticket_ids*. ``None`` (the
    default) screens nothing, so any caller that has not resolved it keeps
    the post-claim occupancy handling as its only guard.

    *tracker_mcp_gate_hits* (GitHub #2442): ticket id -> gate hit for every
    PLAN/IMPL-stage ticket whose branch's settings file verifiably lacks the
    client's configured tracker MCP plugin. A task named here is parked
    BLOCKED_ON_USER with ``disposition=tracker_mcp_gate`` instead of claimed.
    Resolved once per client per tick by
    ``cw.dispatch.tracker_mcp_gate.resolve_tracker_mcp_gate_hits`` and passed
    in precomputed for the same no-I/O-under-the-lock reason. ``None`` (the
    default) screens nothing.

    *adopted_head_drifts* (GitHub #2943, RFC 0016): ticket id -> drift hit for
    every PENDING adopted row whose origin head moved off its pin, resolved by
    ``cw.dispatch.adopted_head_gate``. A row named here is parked
    BLOCKED_ON_USER with ``disposition=adopted_head_drift`` unless its pin
    advanced or it was flagged for a settle since the snapshot. ``None`` (the
    default) screens nothing.

    *review_approval_holds* (GitHub #2773): ticket id -> hold for every
    PENDING FINALIZE row whose tier is ``large`` on the row and whose review
    approval is not valid for its branch head, resolved by
    ``cw.dispatch.review_approval_gate`` with the predicate finalize Step 4.0
    applies. A non-deferral hit parks the row BLOCKED_ON_USER with
    ``disposition=review_approval_gate``; a deferral hit (a git fault or the
    per-tick probe cap) leaves the row PENDING and writes nothing. A hit is
    ignored when the row's approval fields changed since the snapshot.
    ``None`` (the default) screens nothing.

    Returns a tuple (task, spawn_backoff_skipped) where spawn_backoff_skipped
    is True when at least one PENDING task was skipped due to active
    spawn_error backoff (next_eligible_at in the future). See GitHub #868.
    """
    now = datetime.now(UTC)
    # Defense-in-depth (#1346): the caller (dispatch_tick, via
    # _dispatch_client_lanes) already gates the whole tick on this same
    # value at tick.py's top-of-tick early return -- this second check
    # protects any OTHER caller of this claim primitive (this function is
    # re-exported as part of cw.dispatch's private test surface and is not
    # guaranteed to always be reached only through dispatch_tick's gate).
    # Deliberately takes the value as a parameter rather than calling
    # load_usage_limited_until() here: this function is invoked per-client
    # per-tick (an in-function read would be N reads/tick instead of one),
    # and claim.py is pure state-transition logic under dev_queue_lock with
    # no I/O -- reading the file here would break that invariant.
    if usage_limited_until is not None and now < usage_limited_until:
        return None, False
    stale_prs = stale_pr_ticket_ids if stale_pr_ticket_ids is not None else {}
    reasons = occupied_ticket_reasons if occupied_ticket_reasons is not None else {}
    mcp_hits = tracker_mcp_gate_hits if tracker_mcp_gate_hits is not None else {}
    drifts = adopted_head_drifts if adopted_head_drifts is not None else {}
    approval_holds = review_approval_holds if review_approval_holds is not None else {}
    with dev_queue_lock():
        store = load_dev_queue()
        spawn_backoff_skipped = False
        if priority_ticket_ids:
            for ticket_id in priority_ticket_ids:
                for task in store.tasks:
                    if (
                        task.client == client_name
                        and task.ticket_id == ticket_id
                        and task.lane == lane
                        and task.status == QueueItemStatus.PENDING
                    ):
                        outcome = _screen_and_claim(
                            task,
                            client_name=client_name,
                            lane=lane,
                            client=client,
                            config=config,
                            stale_pr_ticket_ids=stale_prs,
                            occupied_ticket_reasons=reasons,
                            tracker_mcp_gate_hits=mcp_hits,
                            adopted_head_drifts=drifts,
                            review_approval_holds=approval_holds,
                            store=store,
                            now=now,
                        )
                        if outcome == _CLAIM_CLAIMED:
                            return task, spawn_backoff_skipped
                        if outcome == _CLAIM_BACKOFF:
                            spawn_backoff_skipped = True
                        break
        pending = sorted(
            [
                t
                for t in store.tasks
                if t.client == client_name
                and t.lane == lane
                and t.status == QueueItemStatus.PENDING
            ],
            key=lambda t: (-t.priority, t.created_at),
        )
        for task in pending:
            outcome = _screen_and_claim(
                task,
                client_name=client_name,
                lane=lane,
                client=client,
                config=config,
                stale_pr_ticket_ids=stale_prs,
                occupied_ticket_reasons=reasons,
                tracker_mcp_gate_hits=mcp_hits,
                adopted_head_drifts=drifts,
                review_approval_holds=approval_holds,
                store=store,
                now=now,
            )
            if outcome == _CLAIM_CLAIMED:
                return task, spawn_backoff_skipped
            if outcome == _CLAIM_BACKOFF:
                spawn_backoff_skipped = True
    return None, spawn_backoff_skipped
