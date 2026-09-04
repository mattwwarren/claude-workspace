"""Dev-queue CRUD: enqueue, remove, cancel, move, clear, prune, resolve, find.

Extracted from the flat ``cw.dev_queue`` module (#1318, part 2). Owns the
operator-facing queue mutations (``add_ticket``, ``register_watched_pr``,
``remove_ticket``, ``cancel_ticket``, ``cancel_task_for_session``,
``move_ticket``, ``clear_tickets``, ``prune_tickets`` and its read-only
preview ``select_prunable_tickets``), the read/resolution helpers
(``resolve_client``, ``list_tickets``, ``_newest_by_created_at``,
``_find_ticket``), the shared prune age-basis (``_prune_age_basis``), and
the ``task.deleted`` event chokepoint (``_emit_task_deleted``).

Layering: imports ``lifecycle.transition_task_status`` at module level for the
cancel paths. ``lifecycle.wait_for_terminal`` reaches back into ``_find_ticket``
here via a deferred import to keep this edge one-way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from cw.config import get_client
from cw.dev_queue.lifecycle import _raise_stage_high_water, transition_task_status
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import CwError, LaneMoveError, LaneNotFoundError, RequeueStageError
from cw.models import (
    DEFAULT_STAGE,
    OCCUPIED_LANE_STATUSES,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
)

if TYPE_CHECKING:
    from cw.models import (
        DevQueueStore,
        OrchestratorConfig,
        TicketTask,
        WatchedPr,
    )

_UNMOVABLE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ]
)

# Statuses eligible for approve_ticket (an existing BLOCKED_ON_USER approval
# gate, or a parked operator-signoff gate to clear). See GitHub #990.
_APPROVABLE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [QueueItemStatus.BLOCKED_ON_USER, QueueItemStatus.AWAITING_OPERATOR_SIGNOFF]
)

# Default age cutoff for `cw dev-queue prune` (#382). A module-level constant,
# deliberately NOT an OrchestratorConfig field -- the operator names the window
# per invocation via --older-than, and a config knob would only add a second
# place for it to drift.
DEFAULT_PRUNE_OLDER_THAN_DAYS: int = 90

# Why no `_PRUNABLE_STATUSES` allow-list here: QueueItemStatus is exhausted by
# OCCUPIED_LANE_STATUSES (never prunable at any age, per #382's binding
# resolution) + PENDING (the stricter-gated carve-out) + COMPLETED/FAILED/
# CANCELLED. A private allow-list of those last three would be the same
# partition read from the other side, and OCCUPIED_LANE_STATUSES
# (cw.models.enums) is already the codebase's single source of truth for it --
# so _select_prune_candidates gates on that constant directly rather than
# minting an equivalent one that could drift from it.


def _validate_stage_in_pipeline(
    stage: Stage, stages: list[Stage], *, client: str
) -> None:
    """Raise ``RequeueStageError`` iff ``stage`` is not a member of ``stages``.

    Shared by ``add_ticket`` (below) and ``_apply_requeue_stage``
    (``cw.dev_queue.requeue``) — the "is this stage actually in *this
    client's* configured pipeline" check, extracted so the two call sites
    can never drift apart. See GitHub #1682.
    """
    if stage not in stages:
        msg = f"Stage '{stage.value}' is not in the pipeline for client '{client}'."
        raise RequeueStageError(msg)


def add_ticket(task: TicketTask) -> bool:
    """Enqueue a TicketTask, acquiring the file lock atomically.

    Returns True if the task was inserted, False if a task with the same
    (client, ticket_id) is already PENDING or RUNNING, already parked on an
    operator gate (BLOCKED_ON_USER / AWAITING_OPERATOR_SIGNOFF — the parked
    row already owns the ticket; re-adding would mint a sibling row that
    later surfaces as ``terminal_sibling`` reconcile noise, #1653 — release
    the park via ``requeue``/``approve`` instead), or if the same
    (client, ticket_id, stage) is already COMPLETED or CANCELLED
    (deduplication guard — terminal check is stage-scoped to allow normal
    multi-stage progression, e.g. COMPLETED PLAN does not block IMPL, #876).

    Raises:
        LaneNotFoundError: if task.lane is not declared for the client.
        RequeueStageError: if task.stage is not in the client's configured
            pipeline (GitHub #1682).
    """
    _active = {QueueItemStatus.PENDING, QueueItemStatus.RUNNING}
    _parked = {
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    }
    _terminal = {QueueItemStatus.COMPLETED, QueueItemStatus.CANCELLED}
    with _lock():
        try:
            client_cfg = get_client(task.client)
        except CwError:
            pass  # Unknown client — lane/stage validation deferred to dispatch
        else:
            declared_lane_names = [ln.name for ln in client_cfg.effective_lanes]
            if task.lane not in declared_lane_names:
                msg = (
                    f"Lane '{task.lane}' is not declared for client '{task.client}'."
                    f" Declared lanes: {', '.join(declared_lane_names)}."
                    f" Run: cw lane add {task.client} {task.lane}"
                )
                raise LaneNotFoundError(msg)
            stages = client_cfg.pipeline.stages
            _validate_stage_in_pipeline(task.stage, stages, client=task.client)
            if task.stage != DEFAULT_STAGE:
                _raise_stage_high_water(task, stages, task.stage)
        store = load_dev_queue()
        for existing in store.tasks:
            if existing.client != task.client or existing.ticket_id != task.ticket_id:
                continue
            if existing.status in _active or existing.status in _parked:
                return False
            if existing.status in _terminal and existing.stage == task.stage:
                return False
        store.tasks.append(task)
        save_dev_queue(store)
    return True


def register_watched_pr(watched: WatchedPr) -> bool:
    """Insert a WatchedPr into the store's watched_prs list, atomically.

    Returns True if inserted, False if an ``active`` watched PR with the same
    ``(repo, pr_number)`` already exists (idempotency dedup, RFC 0011 S2 R7 —
    mirrors ``add_ticket``'s ``with _lock(): load -> scan -> append/return
    False -> save`` shape). The guard is scoped to ``status == "active"`` so a
    future ``dismissed`` transition can re-open registration (adopted #5).
    """
    with _lock():
        store = load_dev_queue()
        for existing in store.watched_prs:
            if (
                existing.repo == watched.repo
                and existing.pr_number == watched.pr_number
                and existing.status == "active"
            ):
                return False
        store.watched_prs.append(watched)
        save_dev_queue(store)
    return True


def register_or_adopt_watched_pr(
    watched: WatchedPr,
) -> Literal["inserted", "adopted", "already_active", "collision"]:
    """Insert *watched*, adopting a pre-existing client-less watch in place.

    GitHub #1927. For ``register_watched_pr``'s ``(repo, pr_number, active)``
    dedup, a bare ``False`` return is indistinguishable from "already
    handled" — but for a producer that needs the row itself to carry
    ``watched.client`` (the ``stale_dispatch_park`` producer, whose park can
    only self-release via a *client*-tagged watch — see
    ``cw.reconcile.tasks._merged_pr_numbers_by_client``), an existing
    client-less watch silently shadowing it is a correctness bug, not a
    no-op: the park would hold its lane slot indefinitely with no signal.

    Returns:

    * ``"inserted"`` — no active watch for ``(repo, pr_number)`` existed;
      *watched* was appended (same case ``register_watched_pr`` returns True
      for).
    * ``"adopted"`` — an active, client-less watch already existed (the
      common case: a pre-existing webhook/cli watch, RFC 0011 S2). It is
      tagged with ``watched.client`` in place rather than shadowed. Safe
      because no consumer reads ``WatchedPr.client`` except the per-client
      merge index this exists to feed — the entry's outbound-consent role
      (RFC 0011 B2, ``resolve_outbound_consent_allowed``) reads only
      ``pr_url``/``status``.
    * ``"already_active"`` — an active watch already exists for this exact
      client (idempotent no-op; unreachable through the current
      ``stale_dispatch_watch`` caller, whose own pre-filter already excludes
      this case, but a correct outcome for any other caller of this
      general-purpose primitive).
    * ``"collision"`` — an active watch exists for a DIFFERENT, non-``None``
      client. Nothing is inserted or mutated; a ``watched_pr.collision``
      event records the park's client, the PR, and the colliding watch's
      client/source, so the outcome is observable rather than silent.
    """
    with _lock():
        store = load_dev_queue()
        for existing in store.watched_prs:
            if not (
                existing.repo == watched.repo
                and existing.pr_number == watched.pr_number
                and existing.status == "active"
            ):
                continue
            if existing.client is None:
                existing.client = watched.client
                save_dev_queue(store)
                return "adopted"
            if existing.client == watched.client:
                return "already_active"
            # Why: emit inline under dev_queue_lock, mirroring
            # _emit_task_deleted's identical precedent below — record_event
            # nests _inbox_lock INSIDE dev_queue_lock, never the reverse, so
            # this is deadlock-safe.
            record_event(
                OrchestratorEventType.WATCHED_PR_COLLISION,
                {
                    "client": watched.client,
                    "repo": watched.repo,
                    "pr_number": watched.pr_number,
                    "colliding_client": existing.client,
                    "colliding_source": existing.source,
                },
                correlation_id=watched.client,
            )
            return "collision"
        store.watched_prs.append(watched)
        save_dev_queue(store)
    return "inserted"


def _emit_task_deleted(
    removed: TicketTask,
    reason: Literal["operator_remove", "operator_clear", "operator_prune"],
) -> None:
    """Emit a ``task.deleted`` event for a single removed row.

    Shared chokepoint for every row-removal site (RFC 0008 W1, closes #978):
    called from ``remove_ticket`` (``operator_remove``), ``clear_tickets``
    (``operator_clear``), and ``prune_tickets`` (``operator_prune``, #382),
    once per removed row.
    """
    # Why: emit inline under dev_queue_lock — one task.deleted per removed
    # row (not per API call). record_event nests _inbox_lock INSIDE
    # dev_queue_lock; the reverse never happens, so this is deadlock-safe.
    record_event(
        OrchestratorEventType.TASK_DELETED,
        {
            "ticket_id": removed.ticket_id,
            "client": removed.client,
            "stage": removed.stage,
            "status_at_deletion": removed.status,
            "reason": reason,
        },
        correlation_id=removed.ticket_id,
    )


def _remove_ticket_selector_clause(
    status: QueueItemStatus | None, disposition: str | None
) -> str:
    """Render remove_ticket's optional status/disposition filter for messages.

    e.g. ``" (status=blocked_on_user, disposition=terminal_sibling)"``. Empty
    string when neither selector is given, so the unfiltered message is
    byte-identical to the pre-#2100 text.
    """
    parts = []
    if status is not None:
        parts.append(f"status={status.value}")
    if disposition is not None:
        parts.append(f"disposition={disposition!r}")
    if not parts:
        return ""
    return f" ({', '.join(parts)})"


def remove_ticket(
    ticket_id: str,
    client: str,
    *,
    remove_all: bool = False,
    status: QueueItemStatus | None = None,
    disposition: str | None = None,
) -> None:
    """Remove one (or all) matching TicketTask(s) from the dev queue.

    *status*/*disposition* (GitHub #2100), when given, narrow the
    (ticket_id, client) match set before the multi-match gate below is
    evaluated -- both are optional and AND'ed together when both are given.
    This lets an operator remove a single stuck duplicate row (e.g. a
    ``BLOCKED_ON_USER`` row parked ``disposition=terminal_sibling`` --
    see ``cw.reconcile.tasks.park_terminal_sibling_tasks`` -- that
    ``cw doctor --reap`` will not touch, since there is nothing to revert it
    *to*) without either guessing which of several same-ticket rows
    ``--all`` would keep, or deleting the ticket's live/legitimate row along
    with it.

    Raises CwError when no task matches (after the status/disposition
    filter, if given).  Raises CwError when multiple tasks match and
    *remove_all* is False -- the message then suggests --status/--disposition
    when neither was already given as a narrowing selector.
    """
    with _lock():
        store = load_dev_queue()
        matches = [
            t
            for t in store.tasks
            if t.ticket_id == ticket_id
            and t.client == client
            and (status is None or t.status == status)
            and (disposition is None or t.disposition == disposition)
        ]
        n = len(matches)
        selector = _remove_ticket_selector_clause(status, disposition)
        if n == 0:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client}'{selector}."
            )
            raise CwError(msg)
        if n > 1 and not remove_all:
            suggestion = (
                " pass --status/--disposition to narrow the match, or --all"
                " to remove all."
                if not selector
                else " pass --all to remove all matching rows."
            )
            msg = (
                f"Multiple dev-queue tasks ({n}) match ticket '{ticket_id}' in"
                f" client '{client}'{selector};{suggestion}"
            )
            raise CwError(msg)
        match_set = {id(m) for m in matches}
        store.tasks = [t for t in store.tasks if id(t) not in match_set]
        save_dev_queue(store)
        for removed in matches:
            _emit_task_deleted(removed, "operator_remove")


def cancel_ticket(ticket_id: str, client: str) -> list[str | None]:
    """Mark a TicketTask as CANCELLED, clearing its session_id.

    Returns the list of session_ids that were cleared (one per cancelled task).
    Raises CwError when no task matches. Idempotent for already-CANCELLED tasks.
    """
    with _lock():
        store = load_dev_queue()
        matches = [
            t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
        ]
        if not matches:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client}'."
            )
            raise CwError(msg)
        cleared: list[str | None] = []
        changed = False
        for task in matches:
            if task.status == QueueItemStatus.CANCELLED:
                continue
            cleared.append(task.session_id)
            transition_task_status(task, QueueItemStatus.CANCELLED)
            task.session_id = None
            changed = True
        if changed:
            save_dev_queue(store)
    return cleared


def cancel_task_for_session(session_id: str) -> bool:
    """Mark the RUNNING TicketTask that owns *session_id* as CANCELLED.

    Returns True if a task was cancelled, False if none matched.
    Used by _spawn_close_impl to atomically preempt the dispatcher before
    the session is marked COMPLETED. See GitHub issue #317.
    """
    with _lock():
        store = load_dev_queue()
        for task in store.tasks:
            if task.session_id == session_id and task.status == QueueItemStatus.RUNNING:
                transition_task_status(task, QueueItemStatus.CANCELLED)
                task.session_id = None
                save_dev_queue(store)
                return True
    return False


def move_ticket(ticket_id: str, client_name: str, to_lane: str) -> str:
    """Move a pending ticket to a different lane.

    Returns the previous lane name (from_lane) for event emission by the caller.

    Raises:
        CwError: if no matching task is found for (ticket_id, client_name).
        LaneNotFoundError: if to_lane is not declared for the client.
        LaneMoveError: if the task status is RUNNING, BLOCKED_ON_USER, or
            AWAITING_OPERATOR_SIGNOFF.

    Note: record_event is NOT called here — the CLI layer fires TICKET_MOVED.
    """
    with _lock():
        store = load_dev_queue()
        task = next(
            (
                t
                for t in store.tasks
                if t.ticket_id == ticket_id and t.client == client_name
            ),
            None,
        )
        if task is None:
            msg = (
                f"No dev-queue task found for ticket '{ticket_id}'"
                f" in client '{client_name}'."
            )
            raise CwError(msg)

        client = get_client(client_name)
        declared_lane_names = [ln.name for ln in client.effective_lanes]
        if to_lane not in declared_lane_names:
            msg = (
                f"Lane '{to_lane}' is not declared for client '{client_name}'."
                f" Declared lanes: {', '.join(declared_lane_names)}."
                f" Run: cw lane add {client_name} {to_lane}"
            )
            raise LaneNotFoundError(msg)

        if task.status in _UNMOVABLE_STATUSES:
            msg = (
                f"Cannot move ticket '{ticket_id}': task is {task.status.value}."
                " Only PENDING tasks can be moved between lanes."
            )
            raise LaneMoveError(msg)

        from_lane = task.lane
        task.lane = to_lane
        save_dev_queue(store)
    return from_lane


def _select_clear_candidates(
    store: DevQueueStore,
    client: str,
    status: QueueItemStatus | None,
) -> list[TicketTask]:
    """Shared filter for ``clear_tickets``/``select_clearable_tickets`` (#2003).

    Deliberately NOT ``_select_prune_candidates`` with a different call
    shape -- the two commands have different eligibility rules and sharing
    one function would force one to grow branches for the other's gates.
    ``prune`` refuses ``OCCUPIED_LANE_STATUSES`` outright, at any age,
    because it targets stale terminal rows; ``clear`` must remain able to
    force-clear a stuck RUNNING or parked BLOCKED_ON_USER/
    AWAITING_OPERATOR_SIGNOFF row when an operator names it explicitly via
    ``--status`` -- there is no age cutoff or terminal-status restriction
    here at all. When *status* is ``None``, ``OCCUPIED_LANE_STATUSES`` rows
    are excluded from the default sweep; naming one of them via *status* is
    itself the gate -- no further restriction applies.
    """
    if status is None:
        return [
            t
            for t in store.tasks
            if t.client == client and t.status not in OCCUPIED_LANE_STATUSES
        ]
    return [t for t in store.tasks if t.client == client and t.status == status]


def select_clearable_tickets(
    client: str, status: QueueItemStatus | None = None
) -> list[TicketTask]:
    """Read-only snapshot of the tasks ``clear_tickets`` would remove (#2003).

    Unlocked (mirrors ``select_prunable_tickets``). Used only by the CLI's
    ``--dry-run`` and default-preview paths, which never mutate. NOT used by
    the ``--confirm`` path -- see ``clear_tickets`` for why the two must not
    share a call in a single ``--confirm`` invocation.
    """
    store = load_dev_queue()
    return _select_clear_candidates(store, client, status)


def clear_tickets(
    client: str, status: QueueItemStatus | None = None
) -> list[TicketTask]:
    """Remove TicketTasks for *client*, optionally filtered by *status* (#2003).

    With no *status*, ``OCCUPIED_LANE_STATUSES`` (RUNNING, BLOCKED_ON_USER,
    AWAITING_OPERATOR_SIGNOFF) rows are excluded from the sweep -- naming one
    of them explicitly via *status* targets it for deletion instead of
    skipping it.

    Mirrors ``prune_tickets``'s locking/emission shape. Computes the
    candidate set exactly ONCE, inside a single ``_lock()`` acquisition --
    one ``load_dev_queue()`` call, one ``_select_clear_candidates()`` call,
    one ``save_dev_queue()`` call -- and deletes exactly that computed set.
    A design that instead called ``select_clearable_tickets()`` first
    (unlocked, for a preview) and then ``clear_tickets()`` second (which
    re-derives fresh under lock) would reopen a TOCTOU window between the
    two calls, in which a concurrent dispatch tick or another prune/clear
    invocation could grow the deleted set beyond whatever was rendered to
    the operator. The CLI must call this function alone for the
    ``--confirm`` path -- never ``select_clearable_tickets()`` first.

    Returns the removed tasks (not a bare count, unlike the pre-#2003
    signature -- the CLI's summary table needs per-row detail, mirroring
    ``prune_tickets``).
    """
    with _lock():
        store = load_dev_queue()
        removed_tasks = _select_clear_candidates(store, client, status)
        removed_ids = {id(t) for t in removed_tasks}
        store.tasks = [t for t in store.tasks if id(t) not in removed_ids]
        save_dev_queue(store)
        for task in removed_tasks:
            _emit_task_deleted(task, "operator_clear")
    return removed_tasks


def _prune_age_basis(task: TicketTask) -> datetime:
    """Single source for prune's ``completed_at or created_at`` age rule (#382).

    A CANCELLED row always has ``completed_at is None``
    (``lifecycle._RESET_DISPOSITION_STATUSES`` clears it on every CANCELLED
    transition), so keying on ``completed_at`` alone would make every
    CANCELLED row permanently unprunable -- ``created_at`` is the fallback.

    Both ``_select_prune_candidates`` (what gets deleted) and the CLI's
    ``_print_task_summary`` (the AGE_DAYS the operator reads before passing
    ``--confirm``) call this instead of re-deriving the rule, so the two can
    never diverge on what "age" means for a destructive command.
    """
    return task.completed_at or task.created_at


def _select_prune_candidates(
    store: DevQueueStore,
    statuses: frozenset[QueueItemStatus],
    older_than_days: int,
    client: str | None,
    *,
    all_clients: bool,
) -> list[TicketTask]:
    """Shared filter: statuses/client/age-cutoff (#382).

    Used by both the read-only preview path and the real delete (from a
    single call each -- see ``prune_tickets``) so the two can never disagree
    on selection.

    Validation order (each raises ``CwError``):

    1. Exactly one of *client*/*all_clients* must be given -- a bare
       ``client=None, all_clients=False`` call is refused outright; this is
       a defense-in-depth mirror of the CLI's own required-client gate for
       any other library caller.
    2. Any status in ``OCCUPIED_LANE_STATUSES`` (RUNNING, BLOCKED_ON_USER,
       AWAITING_OPERATOR_SIGNOFF) is refused outright, regardless of age,
       client, or all_clients -- never prunable.
    3. PENDING is a carve-out: prunable only when named explicitly in
       *statuses* together with a single *client* (never with
       *all_clients* -- refused as an incompatible combination).
    4. *older_than_days* must be >= 0.

    Age basis: see ``_prune_age_basis``.
    """
    if client is None and not all_clients:
        msg = "Must specify a client, or pass all_clients=True."
        raise CwError(msg)
    if client is not None and all_clients:
        msg = "client and all_clients are mutually exclusive."
        raise CwError(msg)
    disallowed = statuses & OCCUPIED_LANE_STATUSES
    if disallowed:
        names = ", ".join(sorted(s.value for s in disallowed))
        msg = (
            f"Cannot prune status(es) {names}: RUNNING, BLOCKED_ON_USER, and"
            " AWAITING_OPERATOR_SIGNOFF rows are never pruned, at any age --"
            " they are live or operator-parked work (#382)."
        )
        raise CwError(msg)
    if QueueItemStatus.PENDING in statuses and all_clients:
        msg = (
            "--all-clients is incompatible with --status pending: crossing"
            " the tenant boundary and pruning non-terminal PENDING rows"
            " together has no legitimate use (#382)."
        )
        raise CwError(msg)
    if older_than_days < 0:
        msg = f"--older-than must be >= 0 (got {older_than_days})."
        raise CwError(msg)
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    return [
        t
        for t in store.tasks
        if t.status in statuses
        and (all_clients or t.client == client)
        and _prune_age_basis(t) < cutoff
    ]


def select_prunable_tickets(
    statuses: frozenset[QueueItemStatus],
    older_than_days: int = DEFAULT_PRUNE_OLDER_THAN_DAYS,
    client: str | None = None,
    *,
    all_clients: bool = False,
) -> list[TicketTask]:
    """Read-only snapshot of the tasks ``prune_tickets`` would remove (#382).

    Unlocked (mirrors ``drain.select_held_tickets`` -- ADR-0005's read-only
    carve-out). Used only by the CLI's ``--dry-run`` and default-preview
    paths, which never mutate. NOT used by the ``--confirm`` path -- see
    ``prune_tickets`` for why the two must not share a call in a single
    ``--confirm`` invocation.
    """
    store = load_dev_queue()
    return _select_prune_candidates(
        store, statuses, older_than_days, client, all_clients=all_clients
    )


def prune_tickets(
    statuses: frozenset[QueueItemStatus],
    older_than_days: int = DEFAULT_PRUNE_OLDER_THAN_DAYS,
    client: str | None = None,
    *,
    all_clients: bool = False,
) -> list[TicketTask]:
    """Remove TicketTasks matching *statuses* past *older_than_days* (#382).

    Mirrors ``clear_tickets``'s locking/emission shape. Computes the
    candidate set exactly ONCE, inside a single ``_lock()`` acquisition --
    one ``load_dev_queue()`` call, one ``_select_prune_candidates()`` call,
    one ``save_dev_queue()`` call -- and deletes exactly that computed set.
    This is deliberate: a design that instead called
    ``select_prunable_tickets()`` first (unlocked, for a preview) and then
    ``prune_tickets()`` second (which re-derives fresh under lock) reopens a
    TOCTOU window between the two calls, in which a concurrent dispatch tick
    or another prune/clear invocation could grow the deleted set beyond
    whatever was rendered to the operator. The CLI must call this function
    alone for the ``--confirm`` path -- never ``select_prunable_tickets()``
    first.

    Returns the removed tasks (not a bare count, unlike ``clear_tickets`` --
    the CLI's summary table needs per-row detail). Raises ``CwError`` per
    ``_select_prune_candidates``.
    """
    with _lock():
        store = load_dev_queue()
        removed_tasks = _select_prune_candidates(
            store, statuses, older_than_days, client, all_clients=all_clients
        )
        removed_ids = {id(t) for t in removed_tasks}
        store.tasks = [t for t in store.tasks if id(t) not in removed_ids]
        save_dev_queue(store)
        for task in removed_tasks:
            _emit_task_deleted(task, "operator_prune")
    return removed_tasks


def resolve_client(
    ticket_id: str,
    config: OrchestratorConfig,
    client_override: str | None,
) -> str:
    """Resolve the target client for a ticket.

    Resolution order:
    1. ``client_override`` (--client flag) if provided
    2. Prefix map: ``GEN-100`` -> prefix ``GEN`` -> client name
    3. Raise ``CwError`` if neither resolves
    """
    if client_override is not None:
        return client_override

    # Extract prefix: everything before the first '-'
    if "-" in ticket_id:
        prefix = ticket_id.split("-", maxsplit=1)[0]
        if prefix in config.linear_prefix_map:
            return config.linear_prefix_map[prefix]

    msg = (
        f"Cannot resolve client for ticket '{ticket_id}'."
        " Use --client to specify, or add the prefix to linear_prefix_map in"
        " ~/.claude-workspace/orchestrator.yaml."
    )
    raise CwError(msg)


def list_tickets(client: str | None = None) -> list[TicketTask]:
    """Return tickets from the dev queue, optionally filtered by client."""
    store = load_dev_queue()
    if client is None:
        return list(store.tasks)
    return [t for t in store.tasks if t.client == client]


def _newest_by_created_at(tasks: list[TicketTask]) -> TicketTask:
    """Return the task with the newest created_at (duplicate-row tie-break).

    Callers must pass a non-empty list (raises via max() on empty by design;
    every existing call site already guards emptiness).
    """
    return max(tasks, key=lambda t: t.created_at)


def _find_ticket(store: DevQueueStore, ticket_id: str, client: str) -> TicketTask:
    """Return the TicketTask matching (ticket_id, client) or raise CwError.

    Selection priority: PENDING/RUNNING (live, newest created_at) →
    BLOCKED_ON_USER (newest created_at) → terminal (newest created_at).
    Re-resolved on every call — callers that poll (e.g. dev_queue_wait)
    pick up re-enqueued tasks on the next tick automatically.

    Emits a one-time stderr warning when multiple live PENDING/RUNNING tasks
    exist for the same (ticket_id, client).
    """
    # Why: add-after-terminal creates duplicate (client, ticket_id) rows.
    # Returning the oldest terminal record would cause wait to resolve
    # immediately on a stale status while a fresh RUNNING task is live.
    import click

    matches = [
        t for t in store.tasks if t.ticket_id == ticket_id and t.client == client
    ]
    if not matches:
        msg = f"No dev-queue task found for ticket '{ticket_id}' in client '{client}'."
        raise CwError(msg)

    live = [
        t
        for t in matches
        if t.status in {QueueItemStatus.PENDING, QueueItemStatus.RUNNING}
    ]
    if live:
        if len(live) > 1:
            click.echo(
                f"Warning: {len(live)} live tasks for ticket '{ticket_id}' "
                f"in client '{client}'; binding to newest.",
                err=True,
            )
        return _newest_by_created_at(live)

    blocked = [t for t in matches if t.status in _APPROVABLE_STATUSES]
    if blocked:
        return _newest_by_created_at(blocked)

    return _newest_by_created_at(matches)
