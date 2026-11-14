"""Dev-queue stale-gate release for reconcile (#2631 package split).

Releases BLOCKED_ON_USER rows parked behind a gate whose PR has since merged
or closed (Variant A and Variant B). See GitHub #1713, #1902, #1927.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cw.auto_dev_result import (
    GATE_TIMEOUT_BLOCKER_REASON,
    STALE_DISPATCH_BLOCKER_REASON,
)
from cw.config import load_clients, load_orchestrator_config
from cw.dev_queue import (
    DEPENDENCY_HOLD_DISPOSITION,
    GATE_TIMEOUT_DISPOSITION,
    STALE_DISPATCH_DISPOSITION,
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
)
from cw.models import (
    ClientConfig,
    DevQueueStore,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    TicketTask,
)
from cw.pr_hydrate import _parse_pr_url
from cw.queue_rows import (
    _AUTOMERGE_NOT_ARMED_REASON,
    _PRIOR_PIPELINE_PR_OPEN_REASON,
)
from cw.reconcile.tasks._client_config import (
    _resolve_task_policy,
)

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


# GitHub #2321: a dependency hold whose PR closed unmerged. Never releases the
# row; emitted once per episode, latched by ``stale_gate_detected_at``.
_PROPOSED_ACTION_DEPENDENCY_PR_CLOSED_UNMERGED = "dependency_hold_pr_closed_unmerged"


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

    Since #2321 a third producer joins: a dependency hold
    (disposition ``DEPENDENCY_HOLD_DISPOSITION``, with a structured
    ``blocked_on_pr``), stamped by ``cw dev-queue hold`` or by routing for a
    ``dependency_unmerged`` park carrying ``depends_on_pr``. Its blocking PR is
    registered as a ``dependency_hold`` WatchedPr, so it has an independent
    state source too. No code retires those watches (#3654).
    """
    if task.status != QueueItemStatus.BLOCKED_ON_USER:
        return False
    if task.blocked_on_pr is None:
        return False
    # GitHub #2321: a dependency hold waits on its blocking PR the same way.
    if task.disposition == DEPENDENCY_HOLD_DISPOSITION:
        return True
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


def _release_variant_b(
    task: TicketTask,
    policy: ReapPolicy,
    *,
    operator_hold: bool = False,
) -> None:
    """Apply the SIGNAL_ONLY/AUTO release for a Variant B gate task.

    ``operator_hold`` (#2321) forces the AUTO release for an operator hold
    whatever the reap policy, because the operator asked for the hold. This
    is one flag on this function, not a copy of it.
    """
    if operator_hold or policy is ReapPolicy.AUTO:
        transition_task_status(task, QueueItemStatus.PENDING)
        task.session_id = None
        # Why: the release fired on MERGED/CLOSED evidence, so the gate cleared
        # and its backoff wait goes too (#2717); evidence-driven, ADR-0014 inv 1.
        clear_gate_block_wait(task)
    else:
        task.stale_gate_detected_at = datetime.now(UTC)


def _scan_variant_b(
    store: DevQueueStore,
    clients: dict[str, ClientConfig],
    orchestrator_config: OrchestratorConfig,
) -> tuple[
    list[str], list[tuple[str, str, str, str, str | None, dict[str, int]]], bool
]:
    """Variant B cross-reference scan; caller holds ``dev_queue_lock`` (#2321).

    Returns (released ids, pending events to emit after the lock, changed).
    Dependency holds take the merged/closed split of their own class; every
    other Variant B row keeps its pre-#2321 behavior.
    """
    released_ids: list[str] = []
    pending: list[tuple[str, str, str, str, str | None, dict[str, int]]] = []
    changed = False
    merged_by_client = _merged_pr_numbers_by_client(store)
    closed_by_client = _closed_pr_numbers_by_client(store)
    for task in store.tasks:
        if not _is_variant_b_gate_task(task):
            continue
        pr = task.blocked_on_pr
        if pr is None:  # _is_variant_b_gate_task already excludes this
            continue
        merged = pr in merged_by_client.get(task.client, set())
        closed = pr in closed_by_client.get(task.client, set())
        if not merged and not closed:
            continue  # unknown or still open: fail closed, release nothing
        orig_session_id = task.session_id
        policy = _resolve_task_policy(
            task.client, task.lane, clients, orchestrator_config
        )
        # GitHub #2321: a dependency hold (the gate admits it only with an id).
        dependency_hold = task.disposition == DEPENDENCY_HOLD_DISPOSITION
        if dependency_hold:
            if not merged:
                # Closed unmerged: nothing to wait for. Latch the event once
                # per episode.
                if task.stale_gate_detected_at is None:
                    task.stale_gate_detected_at = datetime.now(UTC)
                    pending.append(
                        (
                            task.ticket_id,
                            task.client,
                            task.lane,
                            _PROPOSED_ACTION_DEPENDENCY_PR_CLOSED_UNMERGED,
                            orig_session_id,
                            {"blocked_on_pr": pr},
                        )
                    )
                    changed = True
                continue
            # An operator hold (no session) releases under any policy.
            operator_hold = task.session_id is None
            _release_variant_b(task, policy, operator_hold=operator_hold)
        else:
            _release_variant_b(task, policy)
        released_ids.append(task.ticket_id)
        pending.append(
            (
                task.ticket_id,
                task.client,
                task.lane,
                _PROPOSED_ACTION_VARIANT_B,
                orig_session_id,
                {},
            )
        )
        changed = True
    return released_ids, pending, changed


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

    Since #2321 every releasable park registers a client-tagged ``WatchedPr``
    (see ``cw.reconcile.stale_dispatch_watch``), so an orphaned
    ``blocked_on_pr`` whose PR sits on no store row now releases. This
    SUPERSEDES the earlier "residual blind spot" note. The multi-PR regex
    fail-closed and the default ``signal_only`` stamp-only behavior for worker
    parks are unchanged. No code retires those watches yet (#3654), the same
    limit #1927's stale-dispatch watches already carry.

    Dependency holds (#2321) follow their own rule: an operator hold
    (``session_id is None``) releases to PENDING on MERGED under any policy; a
    worker park with an id follows ``reap_policy``. A PR that CLOSED UNMERGED
    never releases a dependency hold: it emits
    ``dependency_hold_pr_closed_unmerged`` once per episode and leaves the row
    held. An unknown PR state fails closed.

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
    pending_events: list[tuple[str, str, str, str, str | None, dict[str, int]]] = []

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
                        {},
                    )
                )
                changed = True
            processed_event_ids.append(event.id)

        # Variant B: dev-queue-wide cross-reference scan (no event stream
        # names these rows directly -- see docstring).
        variant_b_released, variant_b_pending, variant_b_changed = _scan_variant_b(
            store, clients, orchestrator_config
        )
        released_ids.extend(variant_b_released)
        pending_events.extend(variant_b_pending)
        changed = changed or variant_b_changed

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
    for (
        ticket_id,
        client,
        lane,
        proposed_action,
        session_id,
        evidence,
    ) in pending_events:
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
                "evidence": evidence,
            },
            correlation_id=ticket_id,
        )

    return released_ids
