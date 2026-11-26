"""The requeue entry points: ``precheck_requeue`` and ``requeue_ticket`` (#3739)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.config import get_client
from cw.dev_queue._requeue_messages import (
    _regress_needs_stage_message,
    _requeue_result,
    _reset_unproductive_attempts,
)
from cw.dev_queue.adoption import (
    AdoptedPrSnapshot,
    adopted_requeue_refusal,
    apply_adopted_requeue,
)
from cw.dev_queue.crud import _find_ticket
from cw.dev_queue.gate_backoff import clear_gate_block_wait
from cw.dev_queue.lifecycle import _reset_for_same_stage_requeue
from cw.dev_queue.requeue.live_session import _refuse_if_live_session
from cw.dev_queue.requeue.review_delivery import _review_reentry_deliverable
from cw.dev_queue.requeue.stage_gate import (
    _apply_requeue_stage,
    _classify_requeue_move,
    _forward_gate_applied,
    is_pending_requeue_noop,
)
from cw.dev_queue.storage import _lock, load_dev_queue, save_dev_queue
from cw.events import record_event
from cw.exceptions import RequeueStageError, RequeueStateError
from cw.models import OrchestratorEventType, Stage
from cw.native_daemon import NativeDaemonClient, get_native_daemon_client

if TYPE_CHECKING:
    from cw.models.review_feedback import ReviewFeedback


def precheck_requeue(
    ticket_id: str,
    client_name: str,
    stage_override: str | None = None,
    *,
    allow_regress: bool = False,
    from_cancelled: bool = False,
    from_failed: bool = False,
    from_completed: bool = False,
    native_daemon: NativeDaemonClient,
    ignore_session_ids: frozenset[str] = frozenset(),
    idempotent_pending: bool = False,
    infer_regress: bool = False,
    repin_adopted_head: bool = False,
    adopt_recheck: AdoptedPrSnapshot | None = None,
) -> None:
    """Raise what :func:`requeue_ticket` would refuse with, writing nothing.

    Lock-free and read-only (no write, no event, no network, no git I/O),
    in ``requeue_ticket``'s order: the live-session guard (with
    *ignore_session_ids*), the row and client lookup, the
    ``allow_regress``-needs-a-stage refusal, the stage gate
    (:func:`_classify_requeue_move`) and, for a forward move, the status gate
    (:func:`_forward_gate_applied`) -- the very functions the real call runs,
    so the prediction cannot drift. It exists so ``cw dev-queue requeue`` stops
    nothing the requeue is going to refuse (#2517).

    Deliberately NOT predicted: the impl-bypass plan availability (a
    filesystem and tracker read), the FINALIZE ``merge_in_progress`` probe
    (git I/O), the bounded-lock timeout, and any concurrent change between
    this check and the call.

    With *idempotent_pending* (#2692) the row lookup comes first and an
    idempotent PENDING no-op (:func:`is_pending_requeue_noop`) returns before
    the live-session guard; *infer_regress* is passed to the stage gate.
    The adoption keywords feed only the pure ``adopted_requeue_refusal`` (#2943).
    """
    if idempotent_pending and is_pending_requeue_noop(
        _find_ticket(load_dev_queue(), ticket_id, client_name),
        stage_override,
        allow_regress,
    ):
        return
    _refuse_if_live_session(
        ticket_id,
        client_name,
        native_daemon=native_daemon,
        ignore_session_ids=ignore_session_ids,
    )
    # Deferred for the same dev_queue -> executor cycle as requeue_ticket.
    from cw.executor import resolve_pipeline_stages

    task = _find_ticket(load_dev_queue(), ticket_id, client_name)
    refusal = adopted_requeue_refusal(
        task, stage_override, repin=repin_adopted_head, recheck=adopt_recheck
    )
    if refusal is not None:
        raise RequeueStateError(refusal)
    stages = resolve_pipeline_stages(task, get_client(client_name))
    if allow_regress and stage_override is None:
        raise RequeueStageError(_regress_needs_stage_message(task, stages))
    if stage_override is not None and (
        _classify_requeue_move(
            task,
            stages,
            Stage(stage_override),
            allow_regress=allow_regress,
            infer_regress=infer_regress,
        )
        == "regress"
    ):
        return
    _forward_gate_applied(
        task,
        ticket_id,
        from_cancelled=from_cancelled,
        from_failed=from_failed,
        from_completed=from_completed,
        stage_override=stage_override,
    )


def requeue_ticket(
    ticket_id: str,
    client_name: str,
    stage_override: str | None = None,
    *,
    allow_regress: bool = False,
    from_cancelled: bool = False,
    from_failed: bool = False,
    from_completed: bool = False,
    native_daemon: NativeDaemonClient | None = None,
    ignore_session_ids: frozenset[str] = frozenset(),
    bounded: bool = False,
    idempotent_pending: bool = False,
    infer_regress: bool = False,
    reset_attempts: bool = False,
    repin_adopted_head: bool = False,
    adopt_recheck: AdoptedPrSnapshot | None = None,
    review_feedback: ReviewFeedback | None = None,
) -> dict[str, str | bool | int]:
    """Requeue a BLOCKED_ON_USER or AWAITING_OPERATOR_SIGNOFF ticket, optionally
    at a specific stage.

    Returns dict with from_stage, to_stage, ticket_id, client, regressed,
    regress_attempts, from_cancelled_applied, from_failed_applied,
    from_completed_applied, already_pending and regress_inferred for event
    emission. ``already_pending`` is True only for the *idempotent_pending*
    no-op (#2692: nothing written, ``from_stage == to_stage``);
    ``regress_inferred`` is True when a backward *stage_override* regressed
    the row through *infer_regress* rather than ``allow_regress``.
    ``regressed`` is True only on a genuine backward regress
    (``allow_regress`` or *infer_regress* + backward ``stage_override`` +
    blocked task); ``regress_attempts`` is the
    post-mutation attempt count (0 on the forward/same-stage path).
    ``from_cancelled_applied`` is True only when the CANCELLED-acceptance
    branch actually admitted the row — i.e. ``from_cancelled=True`` AND the
    row's status was CANCELLED — not merely when the caller passed
    ``from_cancelled=True``. ``from_failed_applied``/``from_completed_applied``
    are the analogous fields for the FAILED- and COMPLETED-acceptance
    branches, respectively. Callers must key any "recovered from
    CANCELLED"/"recovered from FAILED"/"recovered from COMPLETED" provenance
    signal (e.g. an event ``reason``) off these fields, not off the raw
    ``from_cancelled``/``from_failed``/``from_completed`` arguments, since any
    of the flags is harmlessly additive on an already-approvable row.

    Args:
        from_cancelled: when True, the forward/same-stage path also accepts a
            CANCELLED row (CLI: --from-cancelled). Recovers a ticket stranded
            by ``cw spawn close <sid> --confirmed-dead`` on a RUNNING row
            (#1018). Does NOT widen the backward-regress gate: a CANCELLED
            row combined with a backward stage_override still raises
            RequeueStageError, since _apply_requeue_stage's regress check is
            unchanged.
        from_failed: when True, the forward/same-stage path also accepts a
            FAILED row (CLI: --from-failed). Recovers a ticket whose row was
            marked FAILED even though the underlying session may have
            actually completed clean (#1190). Does NOT widen the
            backward-regress gate: a FAILED row combined with a backward
            stage_override still raises RequeueStageError, since
            _apply_requeue_stage's regress check is unchanged.
        from_completed: when True, the forward/same-stage path also accepts a
            COMPLETED row (CLI: --from-completed). Recovers a ticket whose
            shipped PR later went conflicting because a sibling ticket in the
            same wave merged first (#2023). Does NOT widen the
            backward-regress gate: a COMPLETED row combined with a backward
            stage_override still raises RequeueStageError, since
            _apply_requeue_stage's regress check is unchanged.
        native_daemon: daemon client whose roster the live-session guard
            reads; ``None`` resolves :func:`get_native_daemon_client`.
        ignore_session_ids: ``Session.id`` values excluded from the
            live-session guard (e.g. the session ``cw spawn close --requeue``
            just closed, whose roster entry may lag the close).
        idempotent_pending: when True (CLI only), a row already PENDING with
            no other stage asked for returns the ``already_pending`` no-op
            under the lock instead of raising RequeueStateError. The
            live-session guard still runs first.
        infer_regress: when True (CLI only), a backward *stage_override* on a
            parked row regresses without ``allow_regress``.
        reset_attempts: when True (CLI: --reset-attempts, #2695), zero
            ``unproductive_attempts`` (never raw ``attempts``) before any stage
            or transition call, so the ``task.transition`` event reports 0.
            The result's ``attempts_reset``, ``prior_unproductive_attempts``
            and ``attempts_reset_was_capped`` (the row was
            ``attempt_cap_blocked``) report it; the PENDING no-op returns
            their defaults (False, 0, False) because nothing is written.
        repin_adopted_head / adopt_recheck: CLI-only (#2943, R10); release an
            ``adopted_head_drift`` / ``adopt_pr_*`` park through
            :func:`cw.dev_queue.adoption.apply_adopted_requeue`. ``adopted_*``
            result keys appear only on a re-pin.
        review_feedback: CLI-only; attach a validated review report to the
            existing row while requeueing it at IMPL.

    Raises:
        RequeueLiveSessionError: if a daemon-live session exists for the
            ticket (other than one in ``ignore_session_ids``); raised before
            any dev-queue mutation (#2275). Its subclass
            ``RequeueRosterUnreadableError`` when the roster is unreadable or
            malformed, so a live session cannot be ruled out (fail closed).
        RequeueStateError: if ticket is not BLOCKED_ON_USER or
            AWAITING_OPERATOR_SIGNOFF (forward path), unless from_cancelled
            is True and the ticket is CANCELLED, from_failed is True and
            the ticket is FAILED, or from_completed is True and the ticket
            is COMPLETED.
        RequeueStageError: if stage_override would regress without allow_regress,
            is not in the client's or lane's resolved pipeline, regresses a
            non-blocked task, if
            allow_regress is set with no backward stage_override, or if a
            forward bypass targets stage 'impl' with no approved plan
            available locally or on the tracker (#1681).
        CwError: if no matching task is found.
    """
    _refuse_if_live_session(
        ticket_id,
        client_name,
        native_daemon=native_daemon or get_native_daemon_client(),
        ignore_session_ids=ignore_session_ids,
    )
    # Deferred import of ``resolve_pipeline_stages`` to break the
    # ``dev_queue -> executor -> codex_background -> dev_queue`` cycle (same
    # precedent as ``_review_reentry_deliverable``'s deferred ``cw.executor``
    # import above, see module docstring).
    from cw.executor import resolve_pipeline_stages

    with _lock(bounded=bounded):
        store = load_dev_queue()
        task = _find_ticket(store, ticket_id, client_name)
        if review_feedback is not None:
            task.review_feedback = review_feedback
        # Before any transition (its event reads the counter, and the move to
        # PENDING clears the disposition). A raise before save discards it.
        prior, was_capped = (
            _reset_unproductive_attempts(task) if reset_attempts else (0, False)
        )
        # Why: an operator requeue bypasses the gate's own backoff wait (#2717).
        clear_gate_block_wait(task)

        client_cfg = get_client(client_name)
        stages = resolve_pipeline_stages(task, client_cfg)

        from_stage = task.stage

        if idempotent_pending and is_pending_requeue_noop(
            task, stage_override, allow_regress
        ):
            return _requeue_result(task, from_stage, already_pending=True)

        # #2943: adoption gate + re-pin (I/O under this lock, scope-drift style).
        adopted = apply_adopted_requeue(
            task,
            client_cfg,
            stage_override,
            repin=repin_adopted_head,
            recheck=adopt_recheck,
        )

        if allow_regress and stage_override is None:
            raise RequeueStageError(_regress_needs_stage_message(task, stages))

        # The forward gate's refusal describes the row as found, before the
        # stage pointer moves (#2692).
        row_as_found = task.model_copy()
        regressed = _apply_requeue_stage(
            task,
            stages,
            stage_override=stage_override,
            client_cfg=client_cfg,
            allow_regress=allow_regress,
            infer_regress=infer_regress,
        )

        from_cancelled_applied = False
        from_failed_applied = False
        from_completed_applied = False
        if not regressed:
            # Forward/same-stage path (including inert allow_regress forward
            # targets) requires the ticket to be at a BLOCKED_ON_USER or
            # AWAITING_OPERATOR_SIGNOFF gate, OR (opt-in)
            # CANCELLED/FAILED/COMPLETED.
            from_cancelled_applied, from_failed_applied, from_completed_applied = (
                _forward_gate_applied(
                    row_as_found,
                    ticket_id,
                    from_cancelled=from_cancelled,
                    from_failed=from_failed,
                    from_completed=from_completed,
                    stage_override=stage_override,
                )
            )
            _reset_for_same_stage_requeue(task)
            task.regress_attempts = 0
            # #1794: a forward/same-stage bypass resolves any pending regress
            # in the same lock, so the per-arrival marker must not survive to
            # a later, unrelated stage entry.
            task.regressed_into_stage = None

        to_stage = task.stage

        # #1730: every successful requeue landing at REVIEW is, by construction,
        # a review-stage re-entry of a previously-parked ticket -- i.e. the
        # moment an operator send-back comment is supposed to reach the
        # reviewer. If the resolved backend cannot deliver it, degrade LOUDLY
        # and proceed: a hard-fail guard here would invert the asymmetry
        # _apply_requeue_stage already codifies (impl hard-exits on a missing
        # plan; review/finalize degrade). Emitted inline while dev_queue_lock is
        # still held, mirroring _emit_stage_change's chokepoint convention --
        # the inbox lock nests inside dev_queue_lock as a leaf (ADR-0019,
        # RFC 0008 W1).
        # Inline rather than per-caller because requeue_ticket's other
        # production caller (dev_queue/drain.py) builds no events of its own.
        if to_stage == Stage.REVIEW:
            deliverable, reason, backend, tracker = _review_reentry_deliverable(
                task, client_cfg
            )
            if not deliverable:
                record_event(
                    OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
                    {
                        "ticket_id": ticket_id,
                        "client": client_name,
                        "reason": reason,
                        "backend": backend,
                        "tracker": tracker,
                    },
                    correlation_id=ticket_id,
                )

        save_dev_queue(store)

    return (
        _requeue_result(
            task,
            from_stage,
            regressed=regressed,
            regress_inferred=regressed and not allow_regress,
            applied=(
                from_cancelled_applied,
                from_failed_applied,
                from_completed_applied,
            ),
            attempts_reset=reset_attempts,
            prior_unproductive_attempts=prior,
            attempts_reset_was_capped=was_capped,
        )
        | adopted
    )
