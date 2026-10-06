"""Shared reason, key, template and status constants for ``cw.reconcile._shared``.

The paused-status / disposition reason strings, the ``session.last_result``
dict keys, the salvage and rescue PR templates, the harness queue-operation
record literals, and the window / grace / live-status constants that more
than one reconcile cluster reads -- plus :data:`_LOGGER_NAME`, the pinned
logger name every submodule of this package logs under. Imports nothing from
its siblings: the root of the package's layering. Split out of the flat
``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

from cw.models import ReapReason, SessionStatus

# Pinned logger name for every record this package emits. Deliberately a
# literal, not ``__name__``: the pre-split ``reconcile/_shared.py`` logged under
# this fixed name, and anything filtering/configuring logging by exact logger
# name must keep seeing it after the package split (#2214).
_LOGGER_NAME = "cw.reconcile._shared"


# Session-name prefix for DAEMON sessions spawned by the dispatch loop. The
# full name is ``<client>/<AUTO_DEV_LABEL_PREFIX><ticket_id>``; reconciliation
# uses it to recover the ticket id when reverting phantom tickets. Defined
# here (not in ``cw.dispatch``) to avoid a circular import — ``cw.dispatch``
# imports :func:`reconcile` from this module.
AUTO_DEV_LABEL_PREFIX = "auto-dev/"

# How recently a session's transcript must have been modified to be considered
# actively making progress. If the newest .jsonl under the session's project
# dir was written within this window, the watchdog skips the session (GitHub
# #340). Conservative default: 2 min = well below the 15-min budget.
# 5 min — widened from 2 min (#384): covers short inter-turn gaps; subagent
# gaps are handled separately by the liveness sweep's agent_spawn_stamp check
# (_read_unresolved_subagent_spawn below, #1969).
TRANSCRIPT_LIVENESS_WINDOW_SECONDS = 300

# Recency bound for treating a detected usage-limit message as the *current*
# reason a session stalled or was reaped (GitHub #1345). A limit message far
# behind the transcript's own tail is stale backstory (an early rate-limit the
# worker recovered from), not a live cutoff. The gate is anchored to the
# transcript's last content-bearing record, NOT wall-clock now, so a long-
# quiescent transcript isn't judged against real elapsed time. Reuses the same
# 300s "how recent counts as now" horizon the liveness watchdog uses above.
USAGE_LIMIT_BACKOFF_WINDOW_SECONDS = TRANSCRIPT_LIVENESS_WINDOW_SECONDS
# Tighter recency bound for the salvage low-path (#1345). Salvage stamps a
# terminal USAGE_LIMIT_CUTOFF disposition and (per #1336) preserves the
# worktree, so a false-positive mislabels an ordinary crash as a rate-limit
# cutoff and suppresses auto-retry. 60s admits only a limit message essentially
# at the transcript tail; this site also fails CLOSED (fail_open=False).
USAGE_LIMIT_SALVAGE_WINDOW_SECONDS = 60

# Paused-status value written to SESSION_NEEDS_ATTENTION events for sessions
# the watchdog flags (no sentinel ever emitted, daemon surface still live).
_SILENTLY_IDLE_REASON = "silently_idle"
# Paused-status written to SESSION_NEEDS_ATTENTION events by the liveness
# sweep's operator distress signal: a live DAEMON session crossed the top
# staleness bucket while still in the daemon roster, with no sentinel emitted
# and no pending subagent at the transcript tail. Signal-only — the session
# is left running (no daemon stop, no revert, no park). Replaces the class of
# signal the removed process-kill timeouts used to provide as a side effect.
_SESSION_UNRESPONSIVE_REASON = "session_unresponsive"
# Paused-status written to SESSION_NEEDS_ATTENTION events by the same liveness
# distress path when the quietness IS explained by an outstanding subagent
# spawn -- but that spawn has been outstanding longer than
# OrchestratorConfig.fix_loop_await_deadline_minutes (#2012). Discriminated
# from _SESSION_UNRESPONSIVE_REASON on purpose: the operator's next move is
# different (a dispatch that never produced a subagent, vs. a session that
# simply went quiet), and R3's bar for this deadline was that it "knows what it
# is waiting for and can name what failed". Signal-only like its sibling: no
# disposition, no queue mutation, no worktree touch (ADR-0014).
_FIX_LOOP_AWAIT_DEADLINE_EXCEEDED_REASON = "fix_loop_await_deadline_exceeded"
# Paused-status written to SESSION_NEEDS_ATTENTION events by the same liveness
# distress path when the quietness IS explained by an unresolved, non-subagent
# tool_use at the transcript tail (e.g. Bash) with no matching tool_result --
# most commonly an interactive permission prompt no headless session can
# answer (GitHub #1482; forensic incident: a permission prompt dangled 82
# minutes with only the generic session_unresponsive signal to go on).
# Signal-only exactly as its siblings are, per ADR-0014: nothing is disposed.
_DANGLING_TOOL_USE_REASON = "dangling_tool_use"
# Paused-status written to SESSION_NEEDS_ATTENTION events by the same liveness
# distress path when the transcript's last record is a queue-operation enqueue
# notification (e.g. a backgrounded Bash command's completion) that no later
# turn ever consumed (GitHub #2251; forensic incident: /prep-pr backgrounded a
# quality gate in a headless session, the completion was enqueued, and nothing
# resumed the turn). Takes priority over _DANGLING_TOOL_USE_REASON. Signal-only
# exactly as its siblings are, per ADR-0014: nothing is disposed.
_UNCONSUMED_QUEUE_NOTIFICATION_REASON = "unconsumed_queue_notification"

# Wire-format identifiers for a harness queue-operation transcript record
# (#2251 review round 1) -- shared by _iter_notification_records and
# _detect_unconsumed_queue_notification so the two consumers of this record
# shape never drift on the literal strings they match against.
_QUEUE_OPERATION_RECORD_TYPE = "queue-operation"
_QUEUE_OPERATION_ENQUEUE = "enqueue"

_SALVAGE_SKIP_REASON = "park_marker_blocks_salvage"
# TicketTask.advisory_note written by _stamp_session_id_mismatch_advisories
# (#1762) when a RUNNING row's session_id no longer resolves to a live session.
# The leading "?" is part of the token, matching `cw dev-queue tasks`'s existing
# unregistered-blocker-reason convention (#2097): the flag is a *prefix* so it
# survives the REASON column's 20-char truncation. _reason_cell renders the
# value verbatim and does not add the marker itself.
_SESSION_ID_MISMATCH_ADVISORY_NOTE = "?session_mismatch"
# Paused-status written to SESSION_NEEDS_ATTENTION events when an
# `external`-counterparty session (reviewing a teammate's PR) reaches the
# confirmed-idle threshold. Escalated rather than reaped/parked. RFC 0011 B1
# (#1158).
_EXTERNAL_COUNTERPARTY_IDLE_REASON = "external_counterparty_idle"
# paused_status written to SESSION_NEEDS_ATTENTION when a client's
# consecutive freshness-gate-block latch trips (RFC 0007 §W2).
_FRESHNESS_BLOCK_ESCALATED_REASON = "freshness_gate_blocked"
# paused_status written to SESSION_NEEDS_ATTENTION by the dispatch loop's
# proactive staleness watchdog: a client's last DISPATCH_TICK is older than
# TICK_STALE_SECONDS while it still has pending work and carries no live
# executor-blocked marker (#1875). Client-scoped like
# _FRESHNESS_BLOCK_ESCALATED_REASON above, but recurring on a fixed interval
# rather than a one-shot latch -- the condition it reports (this client's
# dispatch loop is not ticking) does not clear itself.
_DISPATCH_LOOP_STALE_REASON = "dispatch_loop_stale"
# paused_status written to SESSION_NEEDS_ATTENTION when a session's
# consecutive salvage-skip latch trips (closes #974).
_SALVAGE_SKIP_ESCALATED_REASON = "salvage_skip_escalated"
# Reason tag written to SESSION_COMPLETED events when a TIMED_OUT session's PR
# was found MERGED via issue-linkage (timed_out-merged auto-complete, #488).
_TIMED_OUT_MERGED_REASON = "timed_out_merged"
# Paused-status written to SESSION_NEEDS_ATTENTION events when a session's
# worktree has unsaved work and the task is routed to BLOCKED_ON_USER instead
# of being retried automatically (GitHub issue #421).
_DIRTY_WORKTREE_REASON = "dirty_worktree"
# Disposition stamped when the phantom sweep reroutes a dead DAEMON session
# whose worktree still carries an unresolved subagent-spawn stamp (GitHub
# #1646). Distinct from ReapReason.PHANTOM_SURFACE on purpose: that reason says
# only "the surface is gone", while this one says "the surface is gone AND it
# died with a sub-agent spawn in flight", i.e. committed work may sit behind a
# verification tail that never ran. The operator's next move differs -- check
# the worktree/branch before requeueing rather than simply retrying.
#
# This is the live successor to the intent _NEEDS_SALVAGE_REASON no longer
# serves: that constant's producer was deleted outright by ADR-0014 and it is
# marked historical in docs/session-disposition.md. Do not wire new detection
# into it.
#
# Deliberately NOT added to _REAP_ELIGIBLE_DISPOSITIONS_BASE below. That
# frozenset feeds concierge's false-park requeue, and auto-requeuing this class
# would silently re-run a session over possibly-committed work -- the exact
# retry #1646 exists to stop. It IS escalation-eligible, joined as its own
# union term in escalation.py (same split #1702/#1714/#1823 already use), so
# splitting it off phantom_surface does not cost it its operator page.
_UNRESOLVED_SUBAGENT_SPAWN_REASON = "unresolved_subagent_spawn"
# disposition/paused_status stamped by fix_dispatch._park_for_unresolved_ref
# when no candidate in dispatch_fix_agent's reported/upstream/templated ladder
# has a tip matching the worktree's HEAD (GitHub #2209).
#
# Same treatment as _UNRESOLVED_SUBAGENT_SPAWN_REASON above and for the same
# reason: escalation-eligible via its own union term in escalation.py, but
# deliberately NOT in _REAP_ELIGIBLE_DISPOSITIONS_BASE below. A branch cw
# cannot locate is not a false park to auto-requeue -- the requeue would just
# re-run the same failing resolution. It is not a hold or drain disposition
# either. Uniquely among the park dispositions, the row keeps its
# ``pending_fix_dispatch`` -- as evidence for the operator, not as a resume
# point: a requeue sets the row PENDING, the retained handoff is dropped by the
# #2142 stale-handoff sweep, and the ticket is claimed into a fresh REVIEW
# session (#2265 decides whether requeue should resume it instead).
_FIX_DISPATCH_REF_UNRESOLVED_REASON = "fix_dispatch_ref_unresolved"
# paused_status written to SESSION_NEEDS_ATTENTION events when
# complete_timed_out_merged_tasks refuses a COMPLETED transition for a
# PENDING row with no claim history (attempts == spawn_error_count,
# session_id is None -- every attempt died on the spawn-error path) --
# a reconciler false-match rather than a genuine completion (GitHub #1385,
# #1387 belt-and-braces guard, widened by #1623 to also cover
# attempts > 0 spawn-error-only histories).
_NEVER_CLAIMED_COMPLETION_REASON = "never_claimed_completion_refused"
# Reason tag written to SESSION_COMPLETED events when a phantom/stalled/idle
# session's PR was found MERGED before its task was reverted to PENDING.
# Prevents re-dispatch of already-shipped tickets (GitHub issue #637).
_PHANTOM_REAP_MERGED_REASON = "phantom_reap_merged"
# Paused-status written to SESSION_NEEDS_ATTENTION events when the gh
# availability or PR-merged check returns an inconclusive result and the
# task is routed to BLOCKED_ON_USER rather than being reverted to PENDING
# (fail-closed on ambiguous world state; GitHub issue #637).
_GH_CHECK_BLOCKED_REASON = "gh_check_blocked"
# Paused-status written to SESSION_NEEDS_ATTENTION events when the stalled
# watchdog parks a session after exhausting its wall-clock retry cap (GitHub #756).
_STALLED_CAP_PARKED_REASON = "stalled_retry_cap_parked"
# Disposition stamped (and paused_status written to the SESSION_NEEDS_ATTENTION
# event) when the Stop hook observes an abandoned exit: the worker recorded a
# ``park_comment_marker`` for this session, ticket and row stage via ``cw
# signal-park`` after posting its park comment, the Stop fired with no pending
# background tasks and no sentinel, and no sentinel framing text -- not even a
# truncated frame -- appears in the transcript after the marker (GitHub #2135).
#
# Evidence-driven, never a timer: it fires on an observed conjunction of facts,
# and mutates only the dev-queue row (never session status, daemon roster, or
# worktree), so a late sentinel still rescues the row through #918.
#
# Deliberately NOT added to _REAP_ELIGIBLE_DISPOSITIONS_BASE below: that
# frozenset feeds concierge's false-park requeue, and auto-requeuing this class
# would silently re-run a stage the operator was just asked to look at.
#
# Not _NEEDS_SALVAGE_REASON: that constant is historical (its producer was
# deleted by ADR-0014) and new detection must not be wired into it.
_STOPPED_WITHOUT_SENTINEL_REASON = "stopped_without_sentinel"
# Disposition stamped (and paused_status written to the SESSION_NEEDS_ATTENTION
# event) when a still-roster-present worker's transcript tail is a usage-limit
# message with no sentinel (GitHub #2324). Stamped on the BLOCKED_ON_USER park
# a non-auto reap_policy routes to; the auto branch reverts to PENDING instead.
_USAGE_LIMITED_MID_TURN_REASON = "usage_limited_mid_turn"
# The 6-member reap-eligible disposition base shared verbatim by
# concierge.py's _FALSE_PARK_ELIGIBLE_DISPOSITIONS (recipe 1: false-park
# requeue) and escalation.py's _ELIGIBLE_DISPOSITIONS (BLOCKED_ON_USER
# branch) -- GitHub #1571 (#1535 drift-class instance 1). Both modules
# previously hand-typed this same 6-member frozenset independently, synced
# only by a comment telling the reader to update both sites together. See
# concierge.py's module comment for the per-member reasoning (#976
# dispositions, pre-#976 None-disposition legacy rows) -- that reasoning is
# recipe-1-specific and stays with that consumer.
_REAP_ELIGIBLE_DISPOSITIONS_BASE: frozenset[str | None] = frozenset(
    {
        _STALLED_CAP_PARKED_REASON,
        _SILENTLY_IDLE_REASON,
        ReapReason.IDLE_STALL.value,
        ReapReason.WALL_CLOCK_BUDGET.value,
        ReapReason.PHANTOM_SURFACE.value,
        None,
    }
)
# Paused-status written to SESSION_NEEDS_ATTENTION events when a FINALIZE-stage
# session times out with commits pushed but no PR (GitHub #812). The worktree is
# preserved; rescue_finalize_blocked_sessions opens the PR on the next tick.
_FINALIZE_BLOCKED_REASON = "finalize_blocked"
# Paused-status written to SESSION_NEEDS_ATTENTION events by the main_drift sweep
# when a live worktree worker's OWN worktree is elsewhere but the operator main
# checkout is dirty or ahead/diverged from origin — the #925/#940 isolation
# breach (a worker escaped its worktree and committed on the main checkout).
_MAIN_CHECKOUT_DRIFT_REASON = "main_checkout_drift"
# paused_status written to session.last_result when a ROUTE_EMITTED_SENTINEL
# candidate is refused by the shared staged-advance guard (an earlier-stage
# stage-advance-claim replay, or an unresolvable position, #1019; narrowed to
# advance-claims only by GitHub #1676 -- an earlier-stage non-advance-claim
# sentinel now routes instead of refusing). Flips the "last_result is None"
# unrouted-check gate false so the doomed candidate stops re-firing every tick
# (GitHub #1149). Carries no "status" key, so _has_terminal_sentinel stays
# False and the session is not mistaken for genuinely terminal.
_SENTINEL_STAGE_MISMATCH_REFUSED_REASON = "sentinel_stage_mismatch_refused"
# Dict key the paused_status markers above are stored under in idle.py's and
# phantom.py's session.last_result refusal-stamp sites (GitHub #1149). Shared
# so the producer (stamp) and consumer (read-back) sides can't drift
# independently. stalled.py's and salvage.py's own "paused_status" writers
# predate this ticket and are unrelated reasons (_NEEDS_SALVAGE_REASON,
# _FINALIZE_BLOCKED_REASON, etc.) -- out of this ticket's scope, not converted.
_PAUSED_STATUS_KEY = "paused_status"
# Merged-in (never overwriting) flag stamped alongside a pre-existing
# session.last_result dict when a ROUTE_EMITTED_SENTINEL refusal must not
# clobber that dict's own paused_status marker (e.g. idle.py's park marker on
# a session that later becomes a phantom candidate, GitHub #1149 review
# finding). _detect_phantom_candidates' already_refused check reads this in
# addition to _PAUSED_STATUS_KEY so the refusal still latches (stops
# re-offering the doomed candidate) even when the marker itself can't be
# written without destroying pre-existing content.
_SENTINEL_ADVANCE_REFUSED_KEY = "sentinel_advance_refused"
# Merged-in (never overwriting) flag stamped alongside session.last_result's
# already-terminal-shaped payload once a #2458 complete_session=False partial
# route (cw.cli.stop_hook._resolve_and_complete_headless_session) has accepted
# the route (fix cycle 4, Action 1). Unlike _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
# -- a _PAUSED_STATUS_KEY-only stamp that clears the "status" key so a refused
# candidate drops out of _has_terminal_sentinel -- this flag is merged in
# ALONGSIDE the existing terminal dict: the payload is genuinely routed, not
# refused, so a later Stop hook (once background_tasks drains) still needs
# _has_terminal_sentinel True to recognize the session's own terminal result
# and complete it, without re-deriving and re-routing the already-consumed
# sentinel a second time. holds_staged_emit_result reads this flag to answer
# False once consumed, which closes both re-routing paths at their one shared
# predicate: the Stop hook's own emit-precedence routing guard below, and the
# idle sweep's ROUTE_EMITTED_SENTINEL candidacy check (idle/_detect.py).
_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY = "sentinel_partial_route_consumed"
# Git-state salvage constants (GitHub issue #497).
_NEEDS_SALVAGE_REASON = "needs_salvage"
_SALVAGE_KIND_GIT_STATE = "git_state_salvage"
_STAGE_REVIEW_COMPLETE = "s3_review_complete"
_SALVAGE_PR_TITLE_TEMPLATE = "chore: salvage auto-dev branch for #{ticket_id}"
_SALVAGE_PR_BODY_TEMPLATE = (
    "Auto-salvaged by reconcile after the session was reaped post-review.\n\n"
    "The worker reached Stage 3 review (clean) and was reaped before opening a PR. "
    "Review this branch and merge when satisfied.\n\n"
    "Ticket: #{ticket_id}"
)
_RESCUE_PR_BODY_TEMPLATE = (
    "Auto-rescued by reconcile after finalize was blocked.\n\n"
    "The worker completed impl+review and pushed the branch but could not open"
    " the PR (permission classifier / usage limit / transient gh failure)."
    " Ticket: #{ticket_id}"
)
# Appended to the rescue PR body only when ticket_id is a real numeric GitHub
# issue id (mirrors the `ship-it.md` numeric-guard convention) -- feeds
# closedByPullRequestsReferences so the auto-rescued PR auto-closes its ticket
# on merge (GitHub #1293).
_RESCUE_PR_CLOSES_TRAILER_TEMPLATE = "\n\nCloses #{ticket_id}"

# Cause tags for SESSION_TIMED_OUT events emitted by the idle watchdog (#486).
# idle_stall_recovered — watchdog fired but no usage-limit message found.
# usage_limit_cutoff   — transcript contains a Claude session/usage-limit message.
# USAGE_LIMIT_RE is imported from cw.exceptions (centralized there for reuse).
_CAUSE_IDLE_STALL = "idle_stall_recovered"
_CAUSE_USAGE_LIMIT = "usage_limit_cutoff"

# Grace window for a newly-spawned session to register with the daemon
# (`claude agents --json`). `claude --bg` spawn → daemon roster registration
# is async; reconciliation that runs in the same dispatch tick as the spawn
# would otherwise see the session as a phantom and reap it within 1 second.
# 30 seconds is comfortably above observed registration latency (~0.3-1.5s
# in dogfooding 2026-05-26) while still bounding how long a genuinely dead
# session can hide. See GitHub issue #271.
SPAWN_GRACE_SECONDS = 30


# Only these two statuses imply "the daemon should have a live session".
# BACKGROUNDED sessions intentionally have no surface (that's the whole point);
# COMPLETED is terminal. Both are ignored by reconciliation.
_LIVE_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.ACTIVE,
        SessionStatus.IDLE,
    }
)
