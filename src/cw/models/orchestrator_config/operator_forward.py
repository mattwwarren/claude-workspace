"""Operator-attention forward-set for the cw-operator SSE channel (RFC 0008 W3).

``OperatorChannelForward`` and its default event-type and task-transition
status sets. Depends on ``cw.models.enums`` only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cw.models.enums import LivenessBucket, OrchestratorEventType, QueueItemStatus

# RFC 0008 W3 (#1002) default operator-attention forward-set. task.transition
# is admitted only for the terminal/attention-worthy statuses below (narrowed
# further in OperatorChannelForward._admits by cw.cw_operator_events); the
# other four types are unconditional once present in event_types.
_DEFAULT_OPERATOR_EVENT_TYPES: frozenset[OrchestratorEventType] = frozenset(
    {
        OrchestratorEventType.TASK_TRANSITION,
        OrchestratorEventType.TASK_DELETED,
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        OrchestratorEventType.PR_REGISTERED,
        OrchestratorEventType.PR_CI_FAILED,
        OrchestratorEventType.PR_REVIEW_RECEIVED,
        OrchestratorEventType.PR_MERGEABLE,
        OrchestratorEventType.PR_MERGED,
        OrchestratorEventType.SESSION_LIVENESS_CHANGED,
        # RFC 0008 capstone (#1015, Q3): OPERATOR_ESCALATION is the durable
        # escalation latch's operator-facing signal — forwarded by default.
        # CONCIERGE_RECOVERED is deliberately EXCLUDED here: it is an
        # audit-trail record of a *mechanical* (non-destructive) recovery the
        # operator does not need paged for, recorded via record_event but
        # never added to this forward-set.
        OrchestratorEventType.OPERATOR_ESCALATION,
        # GATE_AUTO_APPROVED is deliberately EXCLUDED (since v1.63.0), like
        # CONCIERGE_RECOVERED above: the gate recipes are on by default and
        # release Large gates whose only "reason" was size, so forwarding each
        # release would page the operator for the very noise the recipes
        # remove. It stays in the event log and the ticket's audit comment.
        # Forwarded alongside TICKET_APPROVED: without this correction, a
        # failed queue write/rollback could leave an approval event standing
        # alone as a false-positive operator signal (#2337).
        OrchestratorEventType.TICKET_APPROVAL_FAILED,
        # A failed act-phase mutation leaves the row parked with its page
        # suppressed (dispatch Rule 1 skips SESSION_NEEDS_ATTENTION for a park
        # a recipe will release), so this correction is how the operator
        # learns a person is needed after all.
        OrchestratorEventType.GATE_AUTO_APPROVE_FAILED,
        # RFC 0011 A3 (#1160): an A3 force hold declining the automatic
        # mutation leaves the row parked for a person, same as a failure.
        # Declined rather than raised, but the operator needs to know either
        # way.
        OrchestratorEventType.GATE_AUTO_APPROVE_HELD,
        # RFC 0010 P2 (#1097): a review recipe dispatching an /address-review
        # action with no human in the loop is operator-attention-worthy —
        # forwarded by default (contrast CONCIERGE_RECOVERED, excluded above as
        # audit-only). PR_ACTION_FAILED forwards alongside so a failed dispatch
        # never leaves PR_ACTION_TAKEN standing alone as an uncorrected signal.
        OrchestratorEventType.PR_ACTION_TAKEN,
        OrchestratorEventType.PR_ACTION_FAILED,
        # GitHub #1437: the ssh_key_gate operator escape hatch suppressing an
        # already-live safety probe is attention-worthy.
        OrchestratorEventType.SSH_KEY_GATE_BYPASSED,
        # GitHub #1887: the disk_pressure_gate operator escape hatch
        # suppressing an already-live safety probe is attention-worthy, same
        # rationale as SSH_KEY_GATE_BYPASSED directly above.
        OrchestratorEventType.DISK_PRESSURE_GATE_BYPASSED,
        # GitHub #1730: a review-stage requeue proceeding with no operator-visible
        # confirmation that the send-back comment actually reached the reviewer is
        # a no-human-in-the-loop decision -- operator-attention-worthy, forwarded
        # by default (contrast CONCIERGE_RECOVERED, excluded as audit-only).
        # No companion "delivery succeeded" event exists to pair this with
        # (see #1730 Decisions item 4) -- this event is self-contained, not a
        # correction to another forwarded signal.
        OrchestratorEventType.REQUEUE_REVIEW_DELIVERY_DEGRADED,
    }
)
_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES: frozenset[QueueItemStatus] = frozenset(
    {
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
        QueueItemStatus.COMPLETED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
    }
)


class OperatorChannelForward(BaseModel):
    """Declarative forward-set for the cw-operator SSE channel (RFC 0008 W3).

    Consumed by ``cw.cw_operator_events``'s filter engine, which additionally
    applies the two sub-condition rules referenced above (task.transition's
    ``new_status`` and session.liveness_changed's ``new_bucket`` are compared
    against ``task_transition_statuses``/``liveness_min_bucket`` respectively;
    every other admitted type in ``event_types`` forwards unconditionally).
    No coercion validator by design -- see the field docstring on
    ``OrchestratorConfig.operator_channel_forward``. See GitHub #1002.
    """

    model_config = ConfigDict(extra="forbid")

    event_types: frozenset[OrchestratorEventType] = Field(
        default_factory=lambda: frozenset(_DEFAULT_OPERATOR_EVENT_TYPES)
    )
    task_transition_statuses: frozenset[QueueItemStatus] = Field(
        default_factory=lambda: frozenset(_DEFAULT_OPERATOR_TASK_TRANSITION_STATUSES)
    )
    liveness_min_bucket: LivenessBucket = LivenessBucket.STALE_30M
