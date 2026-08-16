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


# Statuses a Session never leaves once reached. Single source of truth for
# "is this session done" checks scattered across spawn.py, reconcile, cli,
# and doctor (GitHub #1674 — was independently duplicated as an inline tuple
# or a locally-defined frozenset in several places before this constant
# existed; consolidated at four of those call sites here, alongside
# SessionStatus itself, following the WORKER_PURPOSES derived-constant
# precedent above — reconcile/salvage.py:231 carries one remaining
# unconsolidated copy, tracked separately, not folded into this ticket).
TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.TIMED_OUT}
)


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


class LastResultSource(StrEnum):
    """RFC 0012 S2 — provenance of ``Session.last_result``.

    Records which mechanism wrote the session's terminal sentinel, so the
    ``emit_result_locked`` door (``cw.result``) can arbitrate first-writer-
    wins and log a collision naming both sources. ``None`` on ``Session``
    means pre-migration (no writer has stamped a source yet). See #1456.
    """

    EMIT_CLI = "emit_cli"
    STOP_HOOK_HARVEST = "stop_hook_harvest"
    EXECUTOR_DIRECT = "executor_direct"
    GIT_SYNTHESIS = "git_synthesis"
    SALVAGE_TRANSCRIPT = "salvage_transcript"


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
    # ``cw focus set`` / ``cw focus clear`` audit trail (#1644). ``cw focus
    # show`` is read-only and deliberately emits nothing.
    FOCUS_SET = "focus.set"
    FOCUS_CLEARED = "focus.cleared"
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
    # GitHub #1674 — concierge recipe 1 (false_park_requeue) hook-context
    # conflict refusal. Emitted from _act_on_false_park_candidates when the
    # row's currently-resolved session is the exact session that already made
    # a spawn attempt raise HookContextConflictError, and that session is
    # still non-terminal. Distinct from CONCIERGE_RECOVERY_BACKOFF_ARMED
    # above: that one still requeues and only defers the *next* detection
    # cycle, whereas this one SKIPS the requeue outright (no
    # CONCIERGE_RECOVERED, no mutation) because retrying is proven futile
    # until an operator closes the session (`cw spawn close --confirmed-dead
    # <id>`). Deliberately not latched — it re-fires every reconcile pass the
    # row stays parked against the same session.
    CONCIERGE_HOOK_CONTEXT_CONFLICT_REFUSED = "concierge.hook_context_conflict_refused"
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
    # RFC 0011 A3 (#1160) — second companion to GATE_AUTO_APPROVED. Emitted when
    # the act-phase mutation *declines* to approve, after GATE_AUTO_APPROVED was
    # already recorded, because the row carries an armed proactive finalize hold
    # (``--hold-finalize`` / ``finalize_gate: manual``). Distinct from
    # GATE_AUTO_APPROVE_FAILED: nothing raised and nothing is broken — the gate
    # deliberately held. Same correction rationale, so it is forwarded by
    # default alongside GATE_AUTO_APPROVED for the same reason.
    GATE_AUTO_APPROVE_HELD = "gate.auto_approve_held"
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
    # GitHub #1406 -- catch-all-unparseable-sentinel liveness veto. Emitted by
    # _route_blocked_result_to_task instead of landing a RUNNING task terminal
    # FAILED/abandoned when a malformed/unrecognized BlockedResult (the
    # unrecognized-reason catch-all) arrives but the owning session's
    # transcript is still advancing within TRANSCRIPT_LIVENESS_WINDOW_SECONDS.
    # Sibling closure to #1281's SESSION_SENTINEL_STAGE_MISMATCH_VETOED above,
    # but no persisted veto counter: this decision clears the task's
    # session_id and re-queues to PENDING (unlike the other two vetoes, which
    # leave the task RUNNING against the same session for re-evaluation next
    # tick), so a fresh session is dispatched on retry -- there is no
    # same-session repeat-veto risk to bound.
    SESSION_SENTINEL_LIVENESS_VETOED = "session.sentinel_liveness_vetoed"
    # GitHub #1437 — ssh_key_gate operator escape hatch. Emitted by
    # _emit_ssh_key_bypass when the SSH-agent-key preflight probe (#927)
    # reports unavailable but the operator has set
    # OrchestratorConfig.ssh_key_gate_enabled=False, so the would-be skip is
    # suppressed and the client dispatches anyway. Like GATE_AUTO_APPROVED, an
    # automated safety decision suppressing a gate with no human in the loop
    # is operator-attention-worthy — forwarded by default. No paired failure
    # event: emitting this has no mutation of its own that can fail.
    SSH_KEY_GATE_BYPASSED = "gate.ssh_key_bypassed"
    # GitHub #1617 -- scope_hint routing-decision audit trail. Emitted at every
    # scope-gate-relevant routing decision (Rule 1, Rule 3, the stage-walk's
    # REVIEW rung, and the _approve_ticket_locked gate-release site) recording
    # the sentinel's own scope.tier, task.scope_hint, the resolved tier, which
    # rule fired, and the resulting disposition -- so a bypass (a gate that
    # should have fired but didn't) is diagnosable after the fact instead of
    # requiring a forensic sweep. Deliberately NOT added to
    # _DEFAULT_OPERATOR_EVENT_TYPES (orchestrator_config.py): this is an
    # audit/diagnostic trail, not an operator alert, and it fires on
    # effectively every stage transition for every ticket -- far higher volume
    # than any currently-forwarded member.
    SCOPE_ROUTING_DECISION = "dispatch.scope_routing_decision"
    # GitHub #1730 -- a REVIEW-stage requeue whose resolved executor backend
    # cannot deliver the operator's tracker comments to the reviewer. Namespaced
    # by its owning module (dev_queue/requeue.py), same convention as
    # SCOPE_ROUTING_DECISION above. This DEGRADES rather than blocking:
    # requeue.py:182-183 codifies the asymmetry that impl hard-exits on a
    # missing plan while review/finalize degrade, and a hard-fail guard here
    # would invert it (#1730/#1717 comment 6 rejected exactly that). The event
    # is therefore the ONLY signal that the operator's send-back never reached
    # the reviewer, which is why -- unlike SCOPE_ROUTING_DECISION -- it IS in
    # _DEFAULT_OPERATOR_EVENT_TYPES (orchestrator_config.py).
    REQUEUE_REVIEW_DELIVERY_DEGRADED = "requeue.review_delivery_degraded"
    # GitHub #1814 -- one re-derived review finding was suppressed because it
    # matched a VoidedFinding the operator had already settled. Namespaced by
    # its owning module (review_adjudication.py), same convention as
    # SCOPE_ROUTING_DECISION / REQUEUE_REVIEW_DELIVERY_DEGRADED above.
    # Mandatory, not optional: suppression is the one act in the review
    # pipeline that makes a finding disappear without any reviewer or
    # coordinating session deciding so in that pass, and this event is the
    # only durable record that it happened. `apply_voided_suppression` emits
    # it inline for exactly that reason -- suppressing and recording the
    # suppression are not separable steps (see ADR-0015).
    REVIEW_FINDING_VOIDED = "review.finding_voided"
    # GitHub #1837 -- a fix-loop re-review raised a MUST_FIX on code the latest
    # fix cycle never touched, with no substantiated causal link, so the
    # admission gate refused it and diverted it into the debt ledger instead
    # of letting it restart the loop. Same namespacing and same rationale as
    # REVIEW_FINDING_VOIDED above: the refusal is a mechanical decision nobody
    # asked to see, and this event is the only durable record it happened.
    REVIEW_TREADMILL_DETECTED = "review.treadmill_detected"
    # GitHub #1838 -- one re-derived review finding was suppressed because a
    # prior round's operator adjudication had already REJECTED it. Namespaced
    # by its owning module (review_finding_dispositions.py), same convention as
    # REVIEW_FINDING_VOIDED / REVIEW_TREADMILL_DETECTED above.
    # Mandatory, not optional, and for the identical reason REVIEW_FINDING_VOIDED
    # is: this is the second mechanism by which a finding stops blocking with
    # nothing in the current pass deciding so, and the event is its only durable
    # local record. `suppress_adjudicated_findings` emits it inline -- suppressing
    # and recording the suppression are not separable steps (see ADR-0015).
    # Distinct from REVIEW_FINDING_VOIDED rather than a reuse of it: the two
    # suppressions have different identities (evidence-anchored vs.
    # fingerprint_v1), different lifetimes (lapses on code motion vs. does not),
    # and different payloads, so one event type would make an audit trail that
    # cannot say which mechanism fired.
    REVIEW_FINDING_DISPOSITION_SUPPRESSED = "review.finding_disposition_suppressed"


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
