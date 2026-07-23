"""Enums and enum-derived constants for cw's persisted models (DAG root).

Zero dependencies on any other ``cw.models`` submodule — every other submodule
in the package imports from here. See ``cw.models.__init__`` for the split
rationale and DAG order.
"""

from __future__ import annotations

from enum import StrEnum


class SessionPurpose(StrEnum):
    IMPL = "impl"
    IDEA = "idea"
    DEBT = "debt"
    ORCHESTRATE = "orchestrate"


# Purposes a worker session can be dispatched/created with. ORCHESTRATE is
# excluded: an ORCHESTRATE session is created only via `cw orchestrate start`
# (#595 / Phase 4b), never selected as a worker --purpose.
WORKER_PURPOSES: tuple[SessionPurpose, ...] = (
    SessionPurpose.IMPL,
    SessionPurpose.IDEA,
    SessionPurpose.DEBT,
)


class SessionStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    BACKGROUNDED = "backgrounded"
    COMPLETED = "completed"
    # Headless daemon session exceeded wall-clock budget without emitting a
    # sentinel. Terminal-ish but retry-eligible: reconciler reverts the
    # owning TicketTask to PENDING so the dispatch loop can retry.
    # See GitHub issue #176 Layer 1.
    TIMED_OUT = "timed_out"


class CompletionReason(StrEnum):
    USER = "user"
    HANDOFF = "handoff"
    CRASHED = "crashed"
    NORMAL = "normal"
    TIMED_OUT = "timed_out"


class SessionOrigin(StrEnum):
    USER = "user"
    DAEMON = "daemon"


class QueueItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_ON_USER = "blocked_on_user"
    # RFC 0007 Phase 3 (W3): a ticket parked for an explicit operator signoff
    # before it ships (occupies its lane slot the same as BLOCKED_ON_USER).
    # See GitHub #990.
    AWAITING_OPERATOR_SIGNOFF = "awaiting_operator_signoff"


# Statuses that occupy a lane's concurrency slot (RUNNING/BLOCKED_ON_USER
# already did per ADR-0006; AWAITING_OPERATOR_SIGNOFF joins them in #990 --
# a signoff-parked ticket is not eligible for re-dispatch, so it must not be
# double-counted as free capacity). Single source of truth for the 4+
# occupancy-membership tests duplicated across dispatch.py/board.py/
# config_cmds.py prior to this ticket.
OCCUPIED_LANE_STATUSES: frozenset[QueueItemStatus] = frozenset(
    [
        QueueItemStatus.RUNNING,
        QueueItemStatus.BLOCKED_ON_USER,
        QueueItemStatus.AWAITING_OPERATOR_SIGNOFF,
    ]
)


class ReapReason(StrEnum):
    """Reason taxonomy for queue.session_reaped bus events.

    Each value maps to exactly one reconcile disposition path. See the
    reap-site decision table in GitHub issue #380.
    """

    PHANTOM_SURFACE = "phantom_surface"
    IDLE_STALL = "idle_stall"
    USAGE_LIMIT_CUTOFF = "usage_limit_cutoff"
    RETRY_CAP_PARKED = "retry_cap_parked"
    STALLED_RETRY_CAP_PARKED = "stalled_retry_cap_parked"
    WALL_CLOCK_BUDGET = "wall_clock_budget"
    COMPLETED_BACKSTOP = "completed_backstop"
    SALVAGE_COMPLETED = "salvage_completed"
    SALVAGE_PARKED = "salvage_parked"
    FINALIZE_BLOCKED = "finalize_blocked"
    TERMINAL_SIBLING = "terminal_sibling"


class Stage(StrEnum):
    """RFC 0005 pipeline stage. Mutated live by dispatch via
    ``_stage_advance_unchecked`` as a task moves through the pipeline."""

    HARDEN = "harden"
    PLAN = "plan"
    IMPL = "impl"
    REVIEW = "review"
    FINALIZE = "finalize"


class LivenessBucket(StrEnum):
    """RFC 0008 W2 transcript-staleness bucket, latched onto ``Session``.

    Closed 4-value set: per-stage overrides
    (``OrchestratorConfig.liveness_first_bucket_by_stage``) move the
    entry-point threshold for a stage, but never rename or add labels.
    Downstream consumers of ``session.liveness_changed`` match these
    literals directly without reading config. See GitHub #1001.
    """

    LIVE = "live"
    STALE_15M = "stale_15m"
    STALE_30M = "stale_30m"
    STALE_45M = "stale_45m"


class OrchestratorEventType(StrEnum):
    """Event types for the orchestrator-level event bus.

    Covers PR lifecycle, ticket queue, and cross-session coordination.
    """

    TICKET_ENQUEUED = "ticket.enqueued"
    SESSION_SPAWNED = "session.spawned"
    SESSION_COMPLETED = "session.completed"
    SESSION_TIMED_OUT = "session.timed_out"
    SESSION_NEEDS_ATTENTION = "session.needs_attention"
    TICKET_NEEDS_SYNC = "ticket.needs_sync"
    STAGE_ENTERED = "stage.entered"
    STAGE_ERRORED = "stage.errored"
    PR_REGISTERED = "pr.registered"
    PR_CI_FAILED = "pr.ci_failed"
    PR_REVIEW_RECEIVED = "pr.review_received"
    PR_MERGEABLE = "pr.mergeable"
    PR_MERGED = "pr.merged"
    DISPATCH_TICK = "dispatch.tick"
    DISPATCH_LOOP_EXITED = "dispatch.loop_exited"
    # #1343 -- fires when the dispatch loop's usage-limit back-off window
    # (usage_limited_until) transitions from armed to lapsed. See
    # docs/events.md for the payload shape and the --once / single-loop-
    # invariant caveats.
    USAGE_LIMIT_CLEARED = "dispatch.usage_limit_cleared"
    SESSION_PHANTOM_REVERTED = "session.phantom_reverted"
    SESSION_SALVAGE_SKIPPED = "session.salvage_skipped"
    SESSION_REAP_PROPOSED = "session.reap_proposed"
    SESSION_REAP_AUTHORIZED = "session.reap_authorized"
    SESSION_SPAWN_UNREGISTERED = "session.spawn_unregistered"
    LANE_CREATED = "lane.created"
    LANE_PAUSED = "lane.paused"
    LANE_RESUMED = "lane.resumed"
    TICKET_MOVED = "ticket.moved"
    TICKET_APPROVED = "ticket.approved"
    TICKET_REQUEUED = "ticket.requeued"
    TICKET_UNBLOCKED = "ticket.unblocked"
    SESSION_STAGE_TIMED_OUT_RETRIED = "session.stage_timed_out_retried"
    WAVE_COLLISION = "wave.collision"
    # RFC 0008 W1 orchestrator push-channel producers (closes #978). Emitted
    # from cw.dev_queue at the status-authority / stage-mutation / row-removal
    # chokepoints; correlation_id is the ticket_id. See docs/events.md.
    TASK_TRANSITION = "task.transition"
    TASK_STAGE_CHANGED = "task.stage_changed"
    TASK_DELETED = "task.deleted"
    # RFC 0008 W2 liveness producer (#1001): latched transcript-staleness
    # bucket crossings from the reconcile idle-watchdog pass.
    SESSION_LIVENESS_CHANGED = "session.liveness_changed"
    # RFC 0008 capstone (#1015) — daemon-side gate concierge. CONCIERGE_RECOVERED
    # is the mechanical-recovery-reactor's audit trail (emitted before every
    # recipe's mutation); OPERATOR_ESCALATION is the durable-escalation-latch's
    # one-shot fire when a gate has sat parked past ESCALATION_PARK_MINUTES.
    CONCIERGE_RECOVERED = "concierge.recovered"
    OPERATOR_ESCALATION = "operator.escalation"
    # GitHub #1019 — sentinel/task stage-mismatch guard. Emitted by
    # _route_staged_decision when a late/replayed sentinel's stage_reached
    # does not match task.stage; the routing table refuses to advance the
    # row in that case (true no-op — see cw.dispatch._STAGE_REACHED_TO_STAGE).
    SENTINEL_STAGE_MISMATCH = "sentinel.stage_mismatch"
    # #976 — wall-clock-budget liveness veto. Emitted by the stalled sweep
    # instead of proceeding with a REVERT_TASK/park when the session's
    # freshly-classified liveness bucket is still LIVE despite the wall-clock
    # budget having expired. Side-effect-only: no queue or session mutation.
    SESSION_PARK_VETOED = "session.park_vetoed"
    # GitHub #1030 — concierge recipe 1 (false_park_requeue) churn backoff.
    # Emitted from _act_on_false_park_candidates when a candidate's session
    # shows the dead-on-arrival signature (died within seconds of spawn,
    # never producing real output) — the requeue to PENDING always proceeds
    # regardless; this event additionally records that
    # false_park_recovery_count / false_park_recovery_next_eligible_at were
    # stamped, deferring the *next* false-park detection cycle for this
    # ticket. NOT a veto (contrast SESSION_PARK_VETOED above, which
    # accompanies zero mutation) — no queue/session mutation is suppressed.
    CONCIERGE_RECOVERY_BACKOFF_ARMED = "concierge.recovery_backoff_armed"
    # RFC 0009 P1+P2 (#1065) — gate-recipe automation. Emitted by
    # cw.reconcile.gate_recipes._act_auto_approve_review before the
    # auto-approve mutation when a review met the fixed clean-review predicate
    # (no MUST_FIX, no deferred, recommendation=PROCEED, no forbidden-area
    # touch) and was approved with no human review. Unlike CONCIERGE_RECOVERED
    # (audit-only), this IS forwarded to the operator channel by default — an
    # auto-approve bypassing human review is attention-worthy.
    GATE_AUTO_APPROVED = "gate.auto_approved"
    # RFC 0009 P1+P2 (#1065) — companion to GATE_AUTO_APPROVED. Emitted when
    # the act-phase mutation raises after GATE_AUTO_APPROVED was already
    # recorded (e.g. a duplicate row, or the client's pipeline config changed
    # between detect and act) — so the durable event stream carries a
    # correction, not just a non-durable log line, for what would otherwise
    # be a false "approved" signal on the operator channel. Forwarded by
    # default alongside GATE_AUTO_APPROVED for the same reason.
    GATE_AUTO_APPROVE_FAILED = "gate.auto_approve_failed"
    # RFC 0010 P2 (#1097) — review-recipe act phase. Emitted by
    # cw.reconcile.review_recipes._act_address_review BEFORE dispatching an
    # /address-review session in response to a PR whose review came back
    # changes_requested. Like GATE_AUTO_APPROVED (contrast CONCIERGE_RECOVERED,
    # audit-only), this IS forwarded to the operator channel by default — an
    # automated PR action with no human in the loop is attention-worthy. Reused
    # by RFC 0010 P4's other review recipes with no new event types.
    PR_ACTION_TAKEN = "pr.action_taken"
    # RFC 0010 P2 (#1097) — companion to PR_ACTION_TAKEN. Emitted when the
    # dispatch (spawn_create_impl) raises after PR_ACTION_TAKEN was already
    # recorded, or when a precondition anomaly (unparseable PR url, unresolvable
    # client, missing worktree) blocks the action — so the durable event stream
    # carries a correction, not just a non-durable log line. Forwarded by
    # default alongside PR_ACTION_TAKEN for the same reason.
    PR_ACTION_FAILED = "pr.action_failed"
    # GitHub #1281 -- stage-mismatch-refusal liveness veto. Emitted by the
    # phantom sweep instead of proceeding with a CRASH_COMPLETE when a session
    # already latched `already_refused` (a prior tick's #1149 stage-mismatch
    # refusal) still has a transcript actively advancing within
    # TRANSCRIPT_LIVENESS_WINDOW_SECONDS. Side-effect-only, mirrors
    # SESSION_PARK_VETOED above: no queue or session mutation accompanies it.
    SESSION_SENTINEL_STAGE_MISMATCH_VETOED = "session.sentinel_stage_mismatch_vetoed"


class DispatchSkipReason(StrEnum):
    """First-match skip_reason values emitted in dispatch.tick events.

    Precedence (highest first):
    AVAILABILITY_GATE > SSH_KEY_GATE > FRESHNESS_GATE > USAGE_LIMITED
    > HOST_CAPACITY_GATED > CAP_FULL > LANE_CAP_BLOCKED > SPAWN_ERROR
    > LANE_CIRCUIT_PAUSED > SPAWN_ERROR_BACKOFF > NO_PENDING > NONE.
    AVAILABILITY_GATE ranks first: it is the fleet-wide gh-availability
    preflight probe (RFC 0011 A5), checked before the per-client freshness
    gate so a real GitHub outage short-circuits every client before any pays
    the freshness git-fetch cost.
    SSH_KEY_GATE ranks second (#927): a per-client `ssh-add -l` preflight
    probe checked right after the gh-availability gate but before the
    freshness gate, since a session spawned without an unlocked SSH key
    cannot push and would burn a slot for a guaranteed-failing session.
    ATTEMPT_CAP_BLOCKED is emitted per-task when the global attempt ceiling
    parks a task; it is not part of the per-client-tick precedence chain.
    HOST_CAPACITY_GATED ranks just above CAP_FULL (#1444): a fleet-wide
    ``OrchestratorConfig.host_session_budget`` ceiling on concurrently-running
    DAEMON sessions across the whole host, folded into the per-client
    ``available_client_slots`` admission math ahead of the per-client cap
    check so an operator can distinguish "this client's own cap is full"
    from "the whole host is out of budget".
    """

    AVAILABILITY_GATE = "availability_gate"
    SSH_KEY_GATE = "ssh_key_gate"
    FRESHNESS_GATE = "freshness_gate"
    USAGE_LIMITED = "usage_limited"
    HOST_CAPACITY_GATED = "host_capacity_gated"
    CAP_FULL = "cap_full"
    LANE_CAP_BLOCKED = "lane_cap_blocked"
    ATTEMPT_CAP_BLOCKED = "attempt_cap_blocked"
    SPAWN_ERROR = "spawn_error"
    LANE_CIRCUIT_PAUSED = "lane_circuit_paused"
    SPAWN_ERROR_BACKOFF = "spawn_error_backoff"
    NO_PENDING = "no_pending"
    NONE = "none"


class ReapPolicy(StrEnum):
    """Policy controlling whether the reconciler destroys a stalled session.

    Under ``SIGNAL_ONLY`` (default): route the owning task to BLOCKED_ON_USER,
    leave session/worktree/daemon surface intact.  Requires operator action to clear.
    Under ``AUTO``: self-healing — stop daemon, revert task to PENDING, clean worktree.
    """

    SIGNAL_ONLY = "signal_only"
    AUTO = "auto"
