"""Sentinel-to-dev-queue routing for the reconcile package.

Routes an emitted or salvaged ``AUTO_DEV_RESULT`` / ``BlockedResult`` onto
its dev-queue row (``_apply_sentinel_to_task`` and its audited, blocked-result
and stopped-without-sentinel siblings), the row lookup they share, and the
queue mutations reconcile applies. Imports ``_constants``; defers its
``cw.dispatch`` import to function level (#698). Split out of the flat
``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from cw.auto_dev_result import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
    BLOCKER_REASON_VALIDATION_FAILED,
    SALVAGE_TERMINAL_STATUSES,
    AutoDevResult,
    BlockedResult,
)
from cw.config import get_client
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError
from cw.models import (
    OCCUPIED_LANE_STATUSES,
    TERMINAL_QUEUE_STATUSES,
    DevQueueStore,
    LastResultSource,
    OrchestratorEventType,
    QueueItemStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import _deps
from cw.reconcile._shared._constants import (
    _LOGGER_NAME,
    _STOPPED_WITHOUT_SENTINEL_REASON,
)
from cw.result import EmitOutcome, _record_result_emitted_audit, emit_result_on_audited

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import Session

_log = logging.getLogger(_LOGGER_NAME)


# Constants for _apply_sentinel_to_task (moved from cli.py; shared by both
# signal_stop and the ROUTE_EMITTED_SENTINEL reconcile path). See GitHub #578.
# Deliberately compared against raw ``target.attempts``, NOT the newer
# ``unproductive_attempts`` counter (GitHub #1750). This is a per-stage cap on
# repeated *sentinel validation failures*: a worker that keeps emitting an
# unparseable sentinel may well have committed real work each time, so it
# would read as productive and never trip a ceiling keyed on productivity.
# #1750 moved only the GLOBAL ceiling (dispatch/claim.py, concierge.py) to the
# new counter; this cap stays on the total claim count by design. See #756.
# GitHub #2401: also gates the deterministic-parse branch below
# (_DETERMINISTIC_PARSE_FAILURES) since the #2077 incident (session
# f1dd7a30) showed a schema_version_unsupported BlockedResult landing FAILED
# on the first occurrence can kill a worker that is, in fact, still working
# -- the same "may have committed real work each time" rationale applies
# identically to a deterministic parse failure as to validation_failed, so a
# same-occurrence-count cap replaces the prior immediate-abandon there too.
_VALIDATION_FAILED_MAX_ATTEMPTS = 3
_DETERMINISTIC_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED}
)
_TRANSIENT_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_NO_RESULT_EMITTED}
)
_TERMINAL_NO_RETRY_STATUSES: frozenset[str] = SALVAGE_TERMINAL_STATUSES


# #1692: genuinely terminal QueueItemStatus values -- distinct from
# "outside OCCUPIED_LANE_STATUSES", which also includes PENDING (redispatch-
# eligible, not terminal). Used by _apply_sentinel_to_task's lookup-miss
# branch to classify task_already_terminal precisely. Review round 2 (#1692):
# this was an independently-defined literal identical to
# cw.reconcile.review_recipes.auto_fix_ci._REQUEUE_ELIGIBLE_STATUSES --
# both now alias the single cw.models.TERMINAL_QUEUE_STATUSES definition.
_GENUINELY_TERMINAL_QUEUE_STATUSES: frozenset[QueueItemStatus] = TERMINAL_QUEUE_STATUSES


class SentinelRouteOutcome(NamedTuple):
    """Result of routing a sentinel through ``_apply_sentinel_to_task`` (#1019).

    ``rescued`` is True iff a parked (non-RUNNING) task was rescued via the
    #918 AutoDevResult arm. ``routed`` is False iff (a) the shared
    staged-advance guard refused the sentinel's stage position, (b)
    ``_route_blocked_result_to_task`` just landed the task terminal-FAILED via
    a BlockedResult, or (c) the lookup matched a same-ticket/session task
    outside ``OCCUPIED_LANE_STATUSES`` (raced to terminal by a concurrent
    caller). A truly-absent task is the only remaining ``routed=True`` miss
    shape (#1189).

    ``landed_terminal`` (#1273) is True only for cause (b) above -- this very
    call just wrote the task terminal-FAILED via
    ``_route_blocked_result_to_task``. It is False for causes (a) and (c) and
    for every ``routed=True`` case. The distinction matters because (a) and
    (c) both leave a still-legitimately-running (or already-handled) worker
    alone, while (b) means the worker backing this task is now leaked --
    ``routed=False`` alone can't tell them apart. Callers (``signal_stop``)
    use ``landed_terminal`` to `daemon.stop()` the leaked worker in the (b)
    case without touching a worker refused by the #986 stage-mismatch guard.

    GitHub #1406: a catch-all BlockedResult vetoed by the transcript-liveness
    guard re-queues to PENDING, so it reports ``routed=True`` and (derived from
    it) ``landed_terminal=False`` -- exactly the signal that keeps the still-
    advancing worker alive rather than stopping it as leaked.

    ``task_already_terminal`` (GitHub #1692) is True only for the raced-to-
    terminal lookup-miss branch of cause (c) above: a same-ticket/session task
    was found but had already been landed in a genuinely terminal status --
    ``COMPLETED``, ``FAILED``, or ``CANCELLED`` (see ``_GENUINELY_TERMINAL_
    QUEUE_STATUSES``) -- by a concurrent caller before this call's own lookup
    ran. A match outside ``OCCUPIED_LANE_STATUSES`` but not in that terminal
    set (i.e. ``PENDING``) is not terminal -- it is redispatch-eligible, so
    ``task_already_terminal`` is False for it. ``routed`` is still False in
    that case too, same as the terminal sub-cause -- both are a
    ``matched_excluded`` miss, and ``routed`` is ``not matched_excluded``
    regardless of which excluded status was matched; only a true "no such
    task anywhere" miss (R3b) reports ``routed=True``. It is mutually
    exclusive with ``landed_terminal``
    by construction -- ``landed_terminal`` is set only in the ``target is not
    None`` arm, ``task_already_terminal`` only in the ``target is None`` arm.
    Once a task is landed genuinely terminal under this exact session_id, no
    dispatch path will ever give this session another leg for this ticket, so
    a caller (``signal_stop``) can safely complete the now-leaked session on
    this sub-cause -- unlike a stage-mismatch refusal, where a still-advancing
    worker may legitimately produce a later, matching-stage sentinel.

    ``stage_refused`` (GitHub #2490) is True only when cause (a) above fired:
    the shared staged-advance guard refused the sentinel's stage position
    (``sentinel.stage_mismatch`` was emitted). It lets a caller whose session
    is provably dead tell that permanent refusal apart from the other
    ``routed=False`` causes, which are not stage mismatches. ``refused_stage``
    is the row's stage as read under ``dev_queue_lock`` at the moment of that
    refusal (None for every other outcome), so a page can name the row's live
    stage rather than a stale per-pass snapshot of it.
    """

    rescued: bool
    routed: bool
    landed_terminal: bool
    task_already_terminal: bool = False
    stage_refused: bool = False
    refused_stage: Stage | None = None


class _TaskLookupResult(NamedTuple):
    """Result of scanning ``store.tasks`` for a same-ticket/session match.

    Round-3 (#1692): excluded (non-``OCCUPIED_LANE_STATUSES``) matches are
    aggregated across the *entire* scan rather than last-seen-wins -- a
    terminal excluded row and a non-terminal (e.g. ``PENDING``) excluded row
    sharing the same ticket_id/session_id can both exist, and which one a
    naive last-seen implementation reports depends on scan order alone.
    ``seen_terminal_excluded``/``seen_nonterminal_excluded`` let the caller
    make the live/redispatch-eligible interpretation win whenever both are
    present, regardless of order.
    """

    target: TicketTask | None
    target_status: QueueItemStatus | None
    matched_excluded: bool
    seen_terminal_excluded: bool
    seen_nonterminal_excluded: bool
    terminal_excluded_status: QueueItemStatus | None
    terminal_excluded_client: str | None


class AuditedSentinelRouteOutcome(NamedTuple):
    """Result of routing a sentinel through the audited write-ahead seam."""

    route: SentinelRouteOutcome | None
    emit: EmitOutcome | None


def _lookup_matching_task(
    store: DevQueueStore, ticket_id: str, cw_session_id: str
) -> _TaskLookupResult:
    """Scan *store* for the task matching *ticket_id*/*cw_session_id*.

    Returns on the first occupied-status match (the ordinary live-completion
    case); otherwise keeps scanning to the end so a later occupied row is not
    missed behind an earlier excluded-status row (post-review amendment A2),
    aggregating every excluded match encountered along the way. See
    ``_TaskLookupResult`` for why the excluded-match fields are aggregated
    rather than last-seen.
    """
    target: TicketTask | None = None
    target_status: QueueItemStatus | None = None
    matched_excluded = False
    seen_terminal_excluded = False
    seen_nonterminal_excluded = False
    terminal_excluded_status: QueueItemStatus | None = None
    terminal_excluded_client: str | None = None
    for task in store.tasks:
        if task.ticket_id == ticket_id and task.session_id == cw_session_id:
            if task.status in OCCUPIED_LANE_STATUSES:
                target = task
                target_status = task.status
                break
            matched_excluded = True
            if task.status in _GENUINELY_TERMINAL_QUEUE_STATUSES:
                if not seen_terminal_excluded:
                    # First terminal match seen -- used for the
                    # SENTINEL_RACE_MISS payload.
                    terminal_excluded_status = task.status
                    terminal_excluded_client = task.client
                seen_terminal_excluded = True
            else:
                seen_nonterminal_excluded = True
    return _TaskLookupResult(
        target=target,
        target_status=target_status,
        matched_excluded=matched_excluded,
        seen_terminal_excluded=seen_terminal_excluded,
        seen_nonterminal_excluded=seen_nonterminal_excluded,
        terminal_excluded_status=terminal_excluded_status,
        terminal_excluded_client=terminal_excluded_client,
    )


# Everything ``load_dev_queue`` can raise for a dev_queue.json an operator
# (or a torn write) can actually leave on disk, verified against the real
# loader: ``OSError`` for an unreadable file; ``ValueError`` for a malformed
# one (``json.JSONDecodeError``, pydantic's ``ValidationError`` and
# ``UnicodeDecodeError`` are all subclasses); ``AttributeError`` for a payload
# whose top level is not an object, which reaches ``raw.get`` in
# ``migrate_dev_queue``. A queue we cannot read is treated as "no row"
# (fail-closed): the Stop hook must never raise out of claude exiting, and
# "defer" is the safe answer to every unknown here.
_DEV_QUEUE_LOAD_ERRORS = (OSError, ValueError, AttributeError)


def _load_dev_queue_or_none(purpose: str, ticket_id: str) -> DevQueueStore | None:
    """Load the dev queue, or ``None`` with one WARNING (#2135).

    The ``load_plan`` idiom (``dev_queue/storage.py``), applied at the two
    abandoned-exit park call sites only -- ``load_dev_queue`` itself keeps
    raising, because everywhere else a corrupt queue SHOULD be loud.

    *purpose* names the caller in the log so an operator can tell the cheap
    precondition read apart from the park's authoritative reload. The class
    name alone is logged, never a traceback: this fires on a hook that runs at
    every turn boundary.
    """
    try:
        return load_dev_queue()
    except _DEV_QUEUE_LOAD_ERRORS as exc:
        _log.warning(
            "%s skipped for ticket %s: dev queue unreadable (%s)",
            purpose,
            ticket_id,
            type(exc).__name__,
        )
        return None


def find_running_task_for_session(
    ticket_id: str, cw_session_id: str
) -> TicketTask | None:
    """Return the RUNNING dev-queue row *cw_session_id* owns, if any (#2135).

    A lock-free read (``load_dev_queue`` takes no lock) of the same lookup
    :func:`_route_stopped_without_sentinel` repeats authoritatively under
    ``dev_queue_lock`` before it mutates. This is the read-only precondition
    form: the Stop hook needs the row to resolve the abandoned-exit park's
    per-lane enablement, and a row that is absent or not RUNNING means there
    is nothing to park, so the marker read and transcript guard are skipped
    outright.

    An unreadable queue is "no row, defer" rather than an exception out of the
    Stop hook -- see :func:`_load_dev_queue_or_none`.
    """
    store = _load_dev_queue_or_none("find_running_task_for_session", ticket_id)
    if store is None:
        return None
    lookup = _lookup_matching_task(store, ticket_id, cw_session_id)
    if lookup.target is None or lookup.target_status is not QueueItemStatus.RUNNING:
        return None
    return lookup.target


def _route_stopped_without_sentinel(ticket_id: str, session: Session) -> None:
    """Park a headless task BLOCKED_ON_USER after an abandoned exit (GitHub #2135).

    The caller has already established the evidence: the Stop fired with empty
    ``background_tasks``, no sentinel was parsed, AND the worker recorded a
    ``park_comment_marker`` covering this session, ticket and row stage, with
    no sentinel framing text in the transcript after it.

    ``session.status`` is never touched -- a late sentinel still routes through
    the #918 rescue in :func:`_apply_sentinel_to_task`, which re-finds the row
    by the ``session_id`` this park deliberately leaves set.

    Two events follow, in this order: the ``task.transition``
    ``transition_task_status`` emits inline while ``dev_queue_lock`` is held,
    then ``session.needs_attention`` once that lock is released. Only a RUNNING
    row transitions -- ``AWAITING_OPERATOR_SIGNOFF`` and an already-parked row
    return before either event, so an operator-armed hold is never clobbered
    and a repeat Stop is idempotent.

    No ``fire_push_notification``: it backgrounds into a daemon thread
    (``cw/notify.py``) that a Stop-hook process exiting immediately afterwards
    would drop. The event bus already delivers ``session.needs_attention``.
    """
    with dev_queue_lock():
        store = _load_dev_queue_or_none("_route_stopped_without_sentinel", ticket_id)
        if store is None:
            return
        lookup = _lookup_matching_task(store, ticket_id, session.id)
        target = lookup.target
        if target is None or lookup.target_status != QueueItemStatus.RUNNING:
            return
        # The park post is positive evidence the stage did its work, so this
        # RUNNING exit is not charged against unproductive_attempts (#1750).
        transition_task_status(
            target,
            QueueItemStatus.BLOCKED_ON_USER,
            disposition=_STOPPED_WITHOUT_SENTINEL_REASON,
            unproductive=False,
        )
        save_dev_queue(store)
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "paused_status": _STOPPED_WITHOUT_SENTINEL_REASON,
            "breadcrumbs": (
                "Stop hook fired with no sentinel after the worker recorded "
                "its park comment"
            ),
            "crashed": False,
            "lane": session.lane,
        },
        correlation_id=ticket_id,
    )


def _apply_sentinel_to_task(
    ticket_id: str,
    session: Session,
    sentinel: AutoDevResult | BlockedResult,
    *,
    before_persist: Callable[[], bool | None] | None = None,
) -> SentinelRouteOutcome:
    """Update the matching dev-queue task based on the sentinel result.

    Shared by signal_stop (cli.py) and the ROUTE_EMITTED_SENTINEL reconcile
    path so both use the same sentinel→QueueItemStatus mapping.  Called before
    marking the session COMPLETED so the task is in its terminal state when
    revert_completed_silent_tasks runs.  See GitHub issues #251, #578.

    The lookup matches a RUNNING task (live completion) or a BLOCKED_ON_USER /
    AWAITING_OPERATOR_SIGNOFF task that still carries this session_id (an
    idle-parked or signoff-parked session whose late Stop-hook sentinel
    finally arrived, #918, #990). A parked task retains its session_id (the
    idle watchdog does not clear it), so the rescue can re-find it. Returns a
    ``SentinelRouteOutcome`` -- see its docstring for ``rescued``/``routed``.

    Takes the owning ``session`` (not just its id) because the BlockedResult
    arm records ``session.id`` on its ``SENTINEL_BLOCKED_RESULT_REQUEUED``
    audit event; the task lookup itself keys off ``session.id`` alone.

    GitHub #1692/#2140: the returned outcome's ``task_already_terminal`` flag
    is now consumed at all four call sites -- the Stop-hook (``cw.cli.
    stop_hook``, #1692's original fix) and the three reconcile-driven callers
    (``cw.reconcile.idle._mutations``, ``cw.reconcile.phantom._mutations``,
    and the LOCAL-DAEMON git-harvest reaper ``cw.reconcile.local``, #2140) --
    each completing the now-leaked session on this sub-cause instead of
    orphaning it.

    ``before_persist`` is an optional reconcile-only write-ahead hook. When a
    route is accepted, it runs after the in-memory queue mutation but before
    ``save_dev_queue``; a failure therefore cannot leave the persisted queue
    ahead of the caller's session mutation. Returning ``False`` abandons the
    in-memory queue mutation without persisting it.
    """
    cw_session_id = session.id
    with dev_queue_lock():
        store = load_dev_queue()
        # #1189: distinguishes "raced to terminal by a concurrent caller"
        # (R3a) from "no such task anywhere" (R3b). See _TaskLookupResult /
        # _lookup_matching_task for the round-3 (#1692) excluded-match
        # aggregation this depends on.
        lookup = _lookup_matching_task(store, ticket_id, cw_session_id)
        target = lookup.target
        target_status = lookup.target_status
        if target is None:
            if lookup.matched_excluded:
                # #1189: surface the race so an operator can tell "raced to
                # terminal by a concurrent caller" apart from "no such task
                # ever existed" -- both silently returned routed=True before
                # this fix, with no signal a race had occurred at all. WARNING
                # (not INFO) to match this module's convention for anomalous-
                # but-non-fatal conditions (see worktree_cleanup_skip_dirty).
                _log.warning(
                    "sentinel_race_miss_detected: ticket=%s session=%s",
                    ticket_id,
                    cw_session_id,
                )
            # Round-2 (#1692): SENTINEL_RACE_MISS fires only for a genuinely
            # terminal excluded row -- the same condition as
            # task_already_terminal below. A non-terminal excluded row (e.g.
            # PENDING) is redispatch-eligible, not a race, so it emits
            # nothing. Round-3: any non-terminal excluded match seen anywhere
            # in the scan vetoes the terminal classification, even if a
            # terminal match was also seen.
            already_terminal = (
                lookup.seen_terminal_excluded and not lookup.seen_nonterminal_excluded
            )
            if already_terminal:
                # #1692: durable trace alongside the log line -- record_event
                # nests the leaf inbox lock inside dev_queue_lock (ADR-0019), as
                # this module's SENTINEL_BLOCKED_RESULT_REQUEUED call (below)
                # does. No queue/session mutation
                # precedes this in this branch, so there is nothing for a
                # failed write to leave half-applied.
                record_event(
                    OrchestratorEventType.SENTINEL_RACE_MISS,
                    {
                        "ticket_id": ticket_id,
                        "client": lookup.terminal_excluded_client,
                        "session_id": cw_session_id,
                        "excluded_status": lookup.terminal_excluded_status,
                    },
                    correlation_id=ticket_id,
                )
            routed = not lookup.matched_excluded
            if routed and before_persist is not None and before_persist() is False:
                routed = False
            return SentinelRouteOutcome(
                rescued=False,
                routed=routed,
                landed_terminal=False,
                task_already_terminal=already_terminal,
            )

        rescued = False
        routed = True
        landed_terminal = False
        stage_refused = False
        mutated = True
        if isinstance(sentinel, AutoDevResult):
            # Delegate to the shared B2 staged advance decision so both the
            # consume path (_apply_events_to_store) and the reconcile
            # ROUTE_EMITTED_SENTINEL path use the same routing table (#698).
            # Why function-level import: cw.dispatch imports reconcile at
            # module level (see reconcile.py module docstring); a module-level
            # import here would create a circular dependency.
            from cw.dispatch import _route_staged_decision, apply_staged_decision

            clients = _deps.load_effective_clients()
            last_result = sentinel.model_dump(mode="json")
            if target_status == QueueItemStatus.RUNNING:
                routed = apply_staged_decision(
                    target, sentinel.status, last_result, clients
                )
            else:
                # #918: rescue an idle-parked (BLOCKED_ON_USER) task through the
                # same assert-free routing core so it lands in exactly the state
                # its RUNNING counterpart would.
                routed = _route_staged_decision(
                    target, sentinel.status, last_result, clients
                )
                rescued = routed
            # #1019: a stage-mismatch refusal is a true no-op -- the routing
            # core already left `target` untouched, but skip the write too.
            mutated = routed
            stage_refused = not routed
        elif target_status == QueueItemStatus.RUNNING:
            # BlockedResult on a live RUNNING task. A parked task falls through
            # to an implicit no-op — a BlockedResult carries no success signal,
            # so leave it parked (never a false FAILED/COMPLETED on a rescue
            # miss, #918/Comment 9).
            # #1189: `routed` reflects whether this call landed the task
            # terminal-FAILED (False) or re-queued it PENDING (True) --
            # callers must not complete/rescue the session on a FAILED
            # landing. Do NOT set `mutated = routed` here (unlike the
            # AutoDevResult arm above): _route_blocked_result_to_task ALWAYS
            # writes a real transition (FAILED or PENDING) that must be
            # persisted below even when routed=False -- routed=False means
            # "don't also complete the session," not "don't write the task."
            routed = _route_blocked_result_to_task(target, session, sentinel)
            landed_terminal = not routed
        else:
            # True no-op: a late BlockedResult against an already-parked task
            # carries no success signal and must not write (#918).
            mutated = False

        if routed and before_persist is not None and before_persist() is False:
            routed = False
            mutated = False
        if mutated:
            save_dev_queue(store)
        return SentinelRouteOutcome(
            rescued=rescued,
            routed=routed,
            landed_terminal=landed_terminal,
            stage_refused=stage_refused,
            refused_stage=target.stage if stage_refused else None,
        )


def _apply_sentinel_to_task_audited(
    ticket_id: str | None,
    session: Session,
    sentinel: AutoDevResult | BlockedResult,
    *,
    source: LastResultSource,
    audit_existing_result: bool = False,
) -> AuditedSentinelRouteOutcome:
    """Apply a reconcile sentinel with one audited write-ahead operation.

    For a fresh sentinel, first-writer arbitration and the audit event happen
    before the queue is persisted. Existing-result callers (the stalled sweep)
    only record the already-present sentinel and leave its session mutation to
    their caller. Keeping both forms here prevents the idle, phantom, stalled,
    and local reconcile paths from drifting apart on ordering or refusal.
    """
    payload = sentinel.model_dump(mode="json")
    emit_outcome: EmitOutcome | None = None

    def before_persist() -> bool:
        nonlocal emit_outcome
        if audit_existing_result:
            _record_result_emitted_audit(
                session, payload, source=source, status=sentinel.status
            )
            return True
        emit_outcome = emit_result_on_audited(session, payload, source=source)
        return not emit_outcome.refused

    if ticket_id is None:
        if audit_existing_result:
            _record_result_emitted_audit(
                session, payload, source=source, status=sentinel.status
            )
        else:
            emit_outcome = emit_result_on_audited(session, payload, source=source)
        return AuditedSentinelRouteOutcome(None, emit_outcome)

    route = _apply_sentinel_to_task(
        ticket_id,
        session,
        sentinel,
        before_persist=before_persist,
    )
    if emit_outcome is not None and emit_outcome.refused:
        # The queue was deliberately not persisted by before_persist(). Make
        # the refusal shape explicit to callers that otherwise complete on a
        # task-already-terminal route.
        route = route._replace(routed=False, task_already_terminal=True)
    elif audit_existing_result and route.task_already_terminal:
        # No callback runs on the lookup-miss/raced-terminal branch, but the
        # existing session sentinel is still a successful completion audit.
        _record_result_emitted_audit(
            session, payload, source=source, status=sentinel.status
        )
    elif not audit_existing_result and route.task_already_terminal:
        # There is no queue mutation to persist in this branch, so finish the
        # accepted emit directly after the lookup result is known.
        emit_outcome = emit_result_on_audited(session, payload, source=source)
    return AuditedSentinelRouteOutcome(route, emit_outcome)


def _requeue_blocked_result_under_cap(
    target: TicketTask, session: Session, sentinel: BlockedResult
) -> bool:
    """Land FAILED at the attempt cap, else re-queue to PENDING (GitHub #2401).

    Shared by the deterministic-parse, validation_failed, and unrecognized-
    reason catch-all branches of :func:`_route_blocked_result_to_task`, which
    became identical once all were put under
    ``_VALIDATION_FAILED_MAX_ATTEMPTS`` (#2401, #2405): a worker that keeps
    emitting the same rejected sentinel may well have committed real work
    each time, so the cap is evidence-based (repeated identical rejection),
    never a transcript-age/clock comparison (ADR-0014).

    At the cap: persists the rejected sentinel to ``last_blocked_result``
    (#1266) and lands terminal FAILED/abandoned, returning False. Under the
    cap: re-queues to PENDING, clears ``target.session_id``, and emits
    ``SENTINEL_BLOCKED_RESULT_REQUEUED`` -- a re-queue rejects nothing, so
    there is no rejected sentinel for the field, but the event still leaves
    an operator-visible trace of what happened -- returning True.
    """
    if target.attempts >= _VALIDATION_FAILED_MAX_ATTEMPTS:
        target.last_blocked_result = sentinel.model_dump(mode="json")
        transition_task_status(target, QueueItemStatus.FAILED, disposition="abandoned")
        return False
    transition_task_status(target, QueueItemStatus.PENDING)
    target.session_id = None
    record_event(
        OrchestratorEventType.SENTINEL_BLOCKED_RESULT_REQUEUED,
        {
            "ticket_id": target.ticket_id,
            "client": target.client,
            "session_id": session.id,
            "blocker_reason": sentinel.blocker.reason,
            "attempts": target.attempts,
            "attempt_cap": _VALIDATION_FAILED_MAX_ATTEMPTS,
        },
        correlation_id=target.ticket_id,
    )
    return True


def _land_blocked_result_failed(target: TicketTask, sentinel: BlockedResult) -> bool:
    """Persist a rejected sentinel and land the task terminal FAILED."""
    target.last_blocked_result = sentinel.model_dump(mode="json")
    transition_task_status(target, QueueItemStatus.FAILED, disposition="abandoned")
    return False


def _blocked_result_requeue_enabled(
    target: TicketTask, sentinel: BlockedResult
) -> bool:
    """Return the client's #2401 deterministic-parse rollout setting."""
    try:
        enabled = get_client(target.client).blocked_result_requeue_enabled
    except CwError:
        # Fail closed when a legacy/local queue row has no usable clients.yaml
        # entry; rollout must be explicitly enabled for the client.
        enabled = False
    if not enabled:
        _log.warning(
            "sentinel.blocked_result_requeue_shadowed: client=%s ticket=%s; "
            "reason=%s attempts=%s attempt_cap=%s cap_only_would_requeue=%s; "
            "blocked_result_requeue_enabled is false",
            target.client,
            target.ticket_id,
            sentinel.blocker.reason,
            target.attempts,
            _VALIDATION_FAILED_MAX_ATTEMPTS,
            target.attempts < _VALIDATION_FAILED_MAX_ATTEMPTS,
        )
    return enabled


def _route_blocked_result_to_task(
    target: TicketTask,
    session: Session,
    sentinel: BlockedResult,
) -> bool:
    """Route a malformed/unparseable BlockedResult to a RUNNING task's status.

    A BlockedResult means the sentinel failed to parse or was malformed.
    Deterministic parse failures re-queue to PENDING (clearing session_id)
    under a rollout gate; the unrecognized-reason catch-all re-queues under
    the shared evidence-based attempt cap unconditionally, landing FAILED only
    once the cap is reached (GitHub #2401, #2405). Transient failures re-queue
    unconditionally.
    Extracted from _apply_sentinel_to_task to keep that function under the
    branch cap (#918).

    Returns False when this call just landed the task terminal-FAILED (the
    caller must not also complete the owning session on that outcome), True
    when it re-queued to PENDING instead (#1189).

    GitHub #2405 (ADR-0014 audit): the catch-all's former #1406 transcript-
    liveness veto is gone -- no transcript-age comparison decides FAILED vs.
    PENDING. A still-advancing
    worker whose sentinel merely failed to *parse* is protected by the attempt
    cap instead: it is re-queued until it has been rejected
    ``_VALIDATION_FAILED_MAX_ATTEMPTS`` times, and only then landed terminal
    (and, via #1273's ``landed_terminal``, has its daemon stopped).
    """
    # GitHub #2401: the branches below share _requeue_blocked_result_under_
    # cap's attempt-cap gate, closing the #1266 last_blocked_result gap -- a
    # FAILED/abandoned landing from any of them records why, and a sub-cap
    # re-queue leaves a SENTINEL_BLOCKED_RESULT_REQUEUED audit event since
    # there is no rejected sentinel to store on a non-terminal landing.
    if sentinel.blocker.reason in _DETERMINISTIC_PARSE_FAILURES:
        if not _blocked_result_requeue_enabled(target, sentinel):
            return _land_blocked_result_failed(target, sentinel)
        return _requeue_blocked_result_under_cap(target, session, sentinel)
    if sentinel.blocker.reason == BLOCKER_REASON_VALIDATION_FAILED:
        return _requeue_blocked_result_under_cap(target, session, sentinel)
    if sentinel.blocker.reason in _TRANSIENT_PARSE_FAILURES:
        transition_task_status(target, QueueItemStatus.PENDING)
        target.session_id = None
        return True
    # An unparseable/unknown-status sentinel (status_unknown,
    # multiple_result_blocks, any unrecognized reason) carries NO success
    # signal. Never mark it COMPLETED — that silently retires unshipped work
    # as "shipped" (#750, the #728 loss). GitHub #2405 (ADR-0014 audit): it
    # shares the same evidence-based attempt cap as the branches above, so a
    # FAILED landing is decided by repeated rejection, never transcript age.
    return _requeue_blocked_result_under_cap(target, session, sentinel)


def _apply_queue_mutations(
    mutations: dict[str, QueueItemStatus],
    clear_session_id: set[str],
    disposition: str | None = None,
) -> list[str]:
    """Apply ticket-status mutations to the dev queue under dev_queue_lock.

    *mutations* maps ticket_id → target QueueItemStatus for RUNNING tasks.
    *clear_session_id* is the subset of ticket_ids whose session_id should be
    set to None (only PENDING-routed tasks; BLOCKED_ON_USER tasks keep their
    session_id for operator traceability).
    *disposition* is stamped on every mutated task via
    ``transition_task_status`` — each call site passes a single reason string
    appropriate to its own sweep (safe because each call's *mutations* dict is
    built from that sweep's own homogeneous candidate population in one tick).
    See GitHub #976.

    Returns the list of ticket_ids that were mutated.  Skips tasks that are
    not RUNNING (natural idempotency — a second call is a no-op).
    """
    if not mutations:
        return []
    mutated: list[str] = []
    with dev_queue_lock():
        store = load_dev_queue()
        changed = False
        for task in store.tasks:
            if task.status != QueueItemStatus.RUNNING:
                continue
            if task.ticket_id not in mutations:
                continue
            transition_task_status(
                task, mutations[task.ticket_id], disposition=disposition
            )
            if task.ticket_id in clear_session_id:
                task.session_id = None
            mutated.append(task.ticket_id)
            changed = True
        if changed:
            save_dev_queue(store)
    return mutated
