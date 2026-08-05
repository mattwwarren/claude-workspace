"""Shared constants, dataclasses, and leaf helpers for the reconcile package.

This module holds the cross-cutting pieces used by more than one reconcile
cluster (idle, stalled, phantom, salvage, tasks, core): module-level
constants, the :class:`ReconcileReport` / :class:`ReapCandidate` dataclasses,
the :class:`ProposedAction` enum, and the transcript / worktree / queue leaf
helpers. See the package ``__init__`` docstring for the full architecture.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from cw._transcript import locate_transcript
from cw._util import (
    _iter_sentinel_text_blocks,
    _last_content_entry_timestamp,
    claude_project_dir,
)
from cw.auto_dev_result import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED,
    BLOCKER_REASON_VALIDATION_FAILED,
    SALVAGE_TERMINAL_STATUSES,
    AutoDevResult,
    BlockedResult,
    _is_placeholder_sentinel_text,
    extract_block,
    is_documented_example,
    parse_stdout,
    queue_status_for_terminal_sentinel,
)
from cw.config import (
    get_client,
    save_state,
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import read_events, record_event
from cw.exceptions import USAGE_LIMIT_RE, CwError, EmitValidationError
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    OCCUPIED_LANE_STATUSES,
    ClientConfig,
    CompletionReason,
    LastResultSource,
    OrchestratorConfig,
    OrchestratorEventType,
    QueueItemStatus,
    ReapPolicy,
    ReapReason,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile import _deps
from cw.result import (
    EmitOutcome,
    _validate_harvest_payload,
    emit_result_on,
    has_terminal_result,
)
from cw.worktree import (
    reconcile_result_scope,
    remove_worktree,
    resolve_scope_guard_default_branch,
    worktree_has_unsaved_work,
    worktree_path_for,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cw.dispatch import _StagePosition
    from cw.models import CwState, Session

_log = logging.getLogger(__name__)


# Session-name prefix for DAEMON sessions spawned by the dispatch loop. The
# full name is ``<client>/<AUTO_DEV_LABEL_PREFIX><ticket_id>``; reconciliation
# uses it to recover the ticket id when reverting phantom tickets. Defined
# here (not in ``cw.dispatch``) to avoid a circular import — ``cw.dispatch``
# imports :func:`reconcile` from this module.
AUTO_DEV_LABEL_PREFIX = "auto-dev/"

# Wall-clock budget for headless daemon sessions. Mirrors the constant in
# cli.py signal_stop; cli.py imports this value so there is a single source
# of truth. See GitHub issue #185.
#
# Bumped 30 → 60 min on 2026-05-25 after ticket #215 (tier=large, 11 files,
# 626 lines) hit the 30-min cap mid-implementation. Per-ticket / per-tier
# override mechanism tracked in #265.
HEADLESS_TIMEOUT_SECONDS = 3600  # 60 minutes

# Watchdog budget for DAEMON RUNNING sessions that have not yet emitted any
# AUTO_DEV_RESULT sentinel. Per-tier overrides in
# OrchestratorConfig.idle_watchdog_by_tier take precedence; per-ticket
# TicketTask.idle_watchdog_override beats both. After this window, reconcile
# flags the session as BLOCKED_ON_USER and fires a push notification.
IDLE_WATCHDOG_SECONDS = 900  # 15 minutes

DEFAULT_IDLE_RETRY_CAP = 2  # idle-stall auto-retries before parking (#384)
DEFAULT_STALLED_RETRY_CAP = 2  # wall-clock-budget retries before parking (#756)

# How recently a session's transcript must have been modified to be considered
# actively making progress. If the newest .jsonl under the session's project
# dir was written within this window, the watchdog skips the session (GitHub
# #340). Conservative default: 2 min = well below the 15-min budget.
# 5 min — widened from 2 min (#384): covers short inter-turn gaps;
# subagent gaps handled by _awaiting_subagent below.
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

# A worker awaiting a subagent leaves the parent transcript quiet (subagent output
# only lands on return). Treat a pending tool_use at the transcript tail as alive
# for up to this long before concluding the subagent itself is hung. See #384.
# 1800 (30 min) — widened from 900 (#544): a large refactor can run a single tool
# call quietly for 20-30 min; reaping at 15 min false-killed live workers (#543).
# Data-safety: benefit-of-the-doubt is only extended when a *recent* pending
# tool_use is present (strong alive signal). No pending tool_use → reaped normally.
SUBAGENT_LIVENESS_WINDOW_SECONDS = 1800

# Paused-status value written to SESSION_NEEDS_ATTENTION events for sessions
# the watchdog flags (no sentinel ever emitted, daemon surface still live).
_SILENTLY_IDLE_REASON = "silently_idle"
_SALVAGE_SKIP_REASON = "park_marker_blocks_salvage"
# Paused-status written to SESSION_NEEDS_ATTENTION events when an
# `external`-counterparty session (reviewing a teammate's PR) reaches the
# confirmed-idle threshold. Escalated rather than reaped/parked. RFC 0011 B1
# (#1158).
_EXTERNAL_COUNTERPARTY_IDLE_REASON = "external_counterparty_idle"
# paused_status written to SESSION_NEEDS_ATTENTION when a client's
# consecutive freshness-gate-block latch trips (RFC 0007 §W2).
_FRESHNESS_BLOCK_ESCALATED_REASON = "freshness_gate_blocked"
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
# replay or unresolvable position, #1019). Flips the "last_result is None"
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


_SalvageCandidate = tuple[str, str | None, str, str, bool]


class ProposedAction(StrEnum):
    """Action the act dispatcher will take for a classified session.

    See GitHub #552, ADR-0006.
    """

    REVERT_TASK = "revert_task"
    CRASH_COMPLETE = "crash_complete"
    SALVAGE_COMPLETION = "salvage_completion"
    PARK_BLOCKED_ON_USER = "park_blocked_on_user"
    SALVAGE_GIT = "salvage_git"
    SKIP_PARKED = "skip_parked"
    INCREMENT_COUNTER = "increment_counter"
    RECOVER_COUNTER = "recover_counter"
    # Emitted sentinel that signal_stop never routed (turn never completed).
    # Fires at sentinel_unrouted_check_seconds; exempt from signal_only.
    # See GitHub #578.
    ROUTE_EMITTED_SENTINEL = "route_emitted_sentinel"
    # Session at Stage.FINALIZE timed out with commits pushed but no PR.
    # Worktree is preserved; rescue_finalize_blocked_sessions opens the PR.
    # See GitHub #812.
    PARK_FINALIZE_BLOCKED = "park_finalize_blocked"
    # LOCAL fire-and-forget aider process exited (dead liveness handle); harvest
    # synthesizes the git-based completion and advances the task. See #888.
    HARVEST_LOCAL_COMPLETE = "harvest_local_complete"
    # Zero a session's consecutive_salvage_skips latch on recovery (any
    # non-SKIP_PARKED detect-phase disposition). Carries no event of its own —
    # a pure state-mutation candidate. Closes #974.
    RESET_SALVAGE_SKIP_COUNTER = "reset_salvage_skip_counter"
    # Emits `session.park_vetoed` and increments the session's
    # consecutive_park_vetoes latch. The stalled sweep's wall-clock-budget /
    # retry-cap park is suppressed while the session's freshly-classified
    # liveness bucket is still LIVE — but only up to OrchestratorConfig.
    # park_veto_cap consecutive post-budget vetoes; past the cap the pending
    # park proceeds instead (closes #976, bounded by #1445).
    PARK_VETOED = "park_vetoed"
    # Side-effect-only candidate — emits `session.needs_attention`, mutates
    # nothing. An `external`-counterparty session (teammate-review idle-reap
    # exemption) that reaches the confirmed-idle threshold is escalated, not
    # reaped/parked. Closes #1158, RFC 0011 B1.
    ESCALATE_EXTERNAL_IDLE = "escalate_external_idle"
    # Side-effect-only candidate — emits `session.sentinel_stage_mismatch_vetoed`,
    # mutates nothing. The phantom sweep's already_refused -> CRASH_COMPLETE
    # fall-through is suppressed while the session's transcript is still
    # actively advancing. Closes #1281.
    SENTINEL_STAGE_MISMATCH_VETOED = "sentinel_stage_mismatch_vetoed"
    # A live session whose last_result already carries a validated terminal
    # result from another authority (RFC 0012 first-writer-wins) -- e.g. an
    # out-of-band `cw result emit`, which never flips session.status. No door
    # write: the session/task are completed directly from the existing data.
    # See #1470.
    COMPLETE_FOREIGN_RESULT = "complete_foreign_result"


@dataclass(frozen=True)
class ReapCandidate:
    """Classification result from detect phase. Consumed by act dispatcher.

    See GitHub #552, ADR-0006.
    """

    session_id: str
    proposed_action: ProposedAction
    ticket_id: str | None = None
    worktree_dirty: bool = False
    salvage_result: AutoDevResult | None = None
    salvage_csid: str | None = None
    # ROUTE_EMITTED_SENTINEL carries the full parsed result (any status).
    # COMPLETE_FOREIGN_RESULT also carries its validated foreign result here
    # (a second producer of this same field). See #1470.
    routed_sentinel: AutoDevResult | BlockedResult | None = None
    usage_limit_detected: bool = False
    elapsed_seconds: float = 0.0
    reap_reason: ReapReason | None = None
    branch: str | None = None
    worktree_path_str: str | None = None
    post_review_clean: bool = False
    paused_status: str | None = None
    new_observation_count: int = 0
    # Lane the owning task is assigned to; stamped from task.lane in detect phase.
    # Candidates without an owning task carry DEFAULT_LANE. Used by
    # resolve_reap_policy to select per-lane reap_policy over the global default.
    lane: str = DEFAULT_LANE
    # Phantom sweep: carry client + worktree_path for SESSION_PHANTOM_REVERTED payload.
    # Also stamped in stalled/idle detect from session.client so resolve_reap_policy
    # can look up the lane config for this candidate.
    client: str | None = None
    worktree_path: Path | None = None
    # Stamped from task.stage / task.attempts in stalled detect; carried into
    # SESSION_STAGE_TIMED_OUT_RETRIED payload. See GitHub #724.
    stage: Stage = DEFAULT_STAGE
    attempts: int = 0
    # PARK_VETOED / SENTINEL_STAGE_MISMATCH_VETOED only: the freshly-computed
    # transcript-staleness minutes that produced the LIVE classification,
    # carried into the session.park_vetoed / session.sentinel_stage_mismatch_vetoed
    # event payload so the act phase does not need to recompute it. See #976, #1281.
    stale_minutes: float | None = None
    # The session's consecutive_park_vetoes value the act phase should persist
    # after this candidate. See #1445. Two producers: (1) PARK_VETOED sets it
    # to current + 1 (the ordinary increment), carried into the
    # session.park_vetoed payload; (2) a wall-clock REVERT_TASK candidate with
    # veto_cap_exhausted=True sets it to park_veto_cap + 1 — a deliberate bump
    # past the cap so the escalation this candidate drives is edge-triggered
    # (see _liveness_veto_candidate's docstring) rather than re-firing every
    # tick the session stays LIVE. Meaningless (left 0) on every other
    # ProposedAction/veto_cap_exhausted combination.
    new_veto_count: int = 0
    # Stamped True on the fallthrough PARK_BLOCKED_ON_USER / REVERT_TASK
    # candidate when the liveness veto declined *because the veto cap was
    # reached* (as opposed to the session being genuinely stale). Distinguishes
    # "cap fired, escalate to the operator" from an ordinary timeout so the act
    # phase can emit an immediate session.needs_attention at parity across both
    # cap-fire sites. See #1445.
    veto_cap_exhausted: bool = False
    # Stamped from task.regress_attempts / task.spawn_error_count in stalled
    # detect's cap-park site so the SESSION_NEEDS_ATTENTION and
    # SESSION_REAP_PROPOSED payloads for a stalled_retry_cap_parked disposition
    # can carry these correction-signal fields without the consumer having to
    # cross-reference the task record by hand. See #1625.
    regress_attempts: int = 0
    spawn_error_count: int = 0


@dataclass(frozen=True)
class ReconcileReport:
    """What reconciliation would do / did.

    ``phantom_session_ids`` — sessions whose ``surface_ref`` is not in the
    live set. Ordered by the original order in ``state.sessions``.
    ``phantom_session_names`` — session names in the same order as
    ``phantom_session_ids``. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    ``reverted_ticket_ids`` — ticket IDs whose TicketTasks got reverted
    from RUNNING to PENDING. Populated by :func:`reconcile`; empty after
    :func:`compute_drift`.
    ``completed_ticket_ids`` — ticket IDs whose PENDING TicketTasks were
    auto-completed because their TIMED_OUT session's PR merged. Populated
    by :func:`reconcile` via :func:`complete_timed_out_merged_tasks`.
    ``usage_limited`` — True when any reaped session had
    cause=usage_limit_cutoff during this reconcile pass. Signals the
    dispatch loop to enter back-off mode.
    ``salvaged_ticket_ids`` — ticket IDs auto-completed via the HIGH-path
    git-state salvage (committed-but-no-PR reaped sessions). Populated by
    :func:`salvage_committed_no_pr_sessions`. See GitHub issue #497.
    ``rescued_ticket_ids`` — ticket IDs auto-completed via the finalize-blocked
    rescue path (TIMED_OUT sessions whose PR creation previously failed).
    Populated by :func:`rescue_finalize_blocked_sessions`. See GitHub #812 #816.
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    phantom_session_names: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)
    completed_ticket_ids: list[str] = field(default_factory=list)
    usage_limited: bool = False
    salvaged_ticket_ids: list[str] = field(default_factory=list)
    rescued_ticket_ids: list[str] = field(default_factory=list)


def _claude_agents_json() -> list[dict[str, object]]:
    """Call ``claude agents --json`` and return the parsed list.

    Raises ``subprocess.CalledProcessError`` when the daemon is not running,
    or ``subprocess.TimeoutExpired`` if the call hangs past the timeout (#1230).
    """
    proc = subprocess.run(
        ["claude", "agents", "--json"],
        capture_output=True,
        text=True,
        check=True,
        # Why: bare literal (not a module constant) — single call site, matches
        # the RealNativeDaemonClient.stop timeout=10 precedent (native_daemon.py:352)
        # and keeps this fix minimal per #1230's scope fence (see .cw/plan.md).
        timeout=15,
    )
    data = json.loads(proc.stdout)
    return data if isinstance(data, list) else []


def _queue_status_for_salvaged(result: AutoDevResult) -> QueueItemStatus:
    """Map a salvaged AutoDevResult to the appropriate QueueItemStatus.

    Delegates to the shared dispatch/salvage classifier (#1566) so this path
    cannot drift from live dispatch's Rule 1/2/5/3b hold routing again.
    """
    return queue_status_for_terminal_sentinel(result.status)


def _validate_existing_result_for_routing(
    existing_result: dict[str, Any] | None,
) -> AutoDevResult | BlockedResult | None:
    """Validate a door-refused foreign ``existing_result`` for routing, or None.

    Reuses the door's own discriminated validation
    (:func:`cw.result._validate_harvest_payload`) against a **foreign, untrusted**
    dict written by an unknown authority. A validation failure means the shape is
    unroutable, so the caller falls through to the PENDING-requeue floor.

    RFC 0012 A3 (#1459): this read-side foreign-shape check is the one deliberate
    exception to R6's "no defensive ladder around this ticket's own emit sites"
    rule -- it validates a dict this ticket did NOT construct, so a
    ``EmitValidationError`` here is a genuine "is this even routable" reading, not
    a widening of a known-valid payload.

    Promoted here from ``cw.reconcile.concierge`` (#1470) so stalled.py's
    COMPLETE_FOREIGN_RESULT detect-phase guard can reuse it without a
    cross-module private import; concierge.py's own call site now delegates here.
    """
    if existing_result is None:
        return None
    try:
        return _validate_harvest_payload(existing_result)
    except EmitValidationError:
        return None


def _foreign_result_target_queue_status(
    validated: AutoDevResult | BlockedResult,
) -> QueueItemStatus:
    """Map a validated foreign result to its target QueueItemStatus.

    Extracted from ``cw.reconcile.concierge``'s
    ``_route_park_marker_poison_task`` isinstance/status ladder (#1470) so both
    concierge.py's park-marker-poison routing and stalled.py's
    COMPLETE_FOREIGN_RESULT routing share one mapping.

    isinstance(BlockedResult) MUST precede the delegated
    ``_queue_status_for_salvaged`` call: ``BlockedResult`` has no
    ``AutoDevResult`` fields, so the isinstance check is what proves the else
    branch's operand is an ``AutoDevResult`` for mypy --strict (RFC 0012 A3
    #1459). The former ``status == "blocked"`` special case (#1470's "round-3
    bug fix") is gone: "blocked" is in ``STAGE_FAILURE_STATUSES``, so
    ``queue_status_for_terminal_sentinel`` now routes it to BLOCKED_ON_USER
    without a special case (#1566).
    """
    if isinstance(validated, BlockedResult):
        return QueueItemStatus.BLOCKED_ON_USER
    return _queue_status_for_salvaged(validated)


def compute_drift(
    state: CwState,
    native_live: set[str],
    *,
    now: datetime | None = None,
) -> ReconcileReport:
    """Return a report naming sessions whose surface is no longer live.

    An ACTIVE or IDLE session is phantom when:
    - it has a ``surface_ref`` (None means it was never spawned), AND
    - that ref is not in *native_live*, AND
    - its ``started_at`` is older than :data:`SPAWN_GRACE_SECONDS` ago
      (newly-spawned sessions are still registering with the daemon).

    *native_live* is the set of short session IDs reported by
    ``claude agents --json``; callers obtain it via :func:`_claude_agents_json`.

    *now* is injected for testability; defaults to ``datetime.now(UTC)``.

    This function does not mutate state. It also does not distinguish
    "backend reports zero live entries" from "backend is unreachable";
    that guard lives in :func:`reconcile`.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SPAWN_GRACE_SECONDS)
    phantoms: list[str] = []
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.surface_ref is None:
            continue
        if session.surface_ref in native_live:
            continue
        if session.started_at > cutoff:
            continue
        if session.purpose is SessionPurpose.ORCHESTRATE:
            continue
        phantoms.append(session.id)
    return ReconcileReport(phantom_session_ids=phantoms)


def ticket_id_for_session(session_name: str) -> str | None:
    """Extract the ticket id from a daemon session name, or None."""
    _, _, tail = session_name.partition("/")
    if tail.startswith(AUTO_DEV_LABEL_PREFIX):
        return tail[len(AUTO_DEV_LABEL_PREFIX) :]
    return None


def _looks_like_daemon_outage(
    state: CwState,
    daemon_errored: bool,
    native_live: set[str],
) -> bool:
    """True when the daemon appears unreachable and the state still has live refs.

    Fires when:
    - the daemon subprocess raised ``CalledProcessError`` (*daemon_errored*), OR
    - the daemon returned an empty roster while the persisted state has at
      least one ACTIVE/IDLE session with a ``surface_ref``.

    In either case, assume the daemon is transiently unreachable rather than
    "somehow every session died at once". Aborting here is the difference
    between a 5-second restart and permanent data loss.

    When *native_live* is non-empty the daemon is clearly reachable, so
    this returns False regardless of *daemon_errored*.
    """
    if not daemon_errored and native_live:
        return False
    return any(
        s.surface_ref is not None and s.status in _LIVE_STATUSES for s in state.sessions
    )


def _backfill_claude_session_ids(
    state: CwState, surface_to_full: dict[str, str]
) -> int:
    """Backfill claude_session_id from the daemon roster for DAEMON sessions.

    Called once per reconcile tick, after the outage guard. Returns the number
    of sessions updated; saves state when non-zero.
    """
    count = 0
    for session in state.sessions:
        if (
            session.claude_session_id is None
            and session.surface_ref is not None
            and session.status in _LIVE_STATUSES
            and session.origin is SessionOrigin.DAEMON
        ):
            from_agents = surface_to_full.get(session.surface_ref)
            resolved = from_agents or _csid_from_transcript(session)
            if resolved is not None:
                session.claude_session_id = resolved
                count += 1
    if count:
        _log.debug("Backfilled claude_session_id for %d session(s)", count)
        save_state(state)
    return count


def resolve_headless_budget(
    task: TicketTask | None,
    session: Session | None,
    config: OrchestratorConfig,
) -> int:
    """Return the wall-clock budget (seconds) for *session*.

    Precedence (highest first):
    1. task.headless_timeout_override — explicit per-ticket escape hatch.
    1.5. config.headless_timeout_by_stage[task.stage] — per-stage default,
         when task is not None and task.stage has an entry in the map.
    2. session.last_result scope.tier — look up per-tier default in config.
    2.5. task.scope_hint — fallback when last_result tier is unavailable (#314).
    3. HEADLESS_TIMEOUT_SECONDS — global fallback (pre-Stage-1 or unknown tier).

    *session* may be None when called from the dispatch path (pre-spawn,
    no session object exists yet). In that case step 2 is skipped and
    step 2.5 fires if task.scope_hint is set.
    """
    if task is not None and task.headless_timeout_override is not None:
        return task.headless_timeout_override
    if task is not None:
        stage_budget = config.headless_timeout_by_stage.get(task.stage)
        if stage_budget is not None:
            return stage_budget
    last_result = session.last_result if session is not None else None
    if last_result is not None:
        tier: str | None = None
        try:
            scope = last_result.get("scope")
            if isinstance(scope, dict):
                tier = scope.get("tier")
        except (AttributeError, TypeError):
            pass
        if isinstance(tier, str):
            return config.headless_timeout_by_tier.get(tier, HEADLESS_TIMEOUT_SECONDS)
    # Step 2.5: last_result is None or had no extractable tier — try scope_hint.
    # Fires for sessions that haven't yet emitted a sentinel (pre-Stage-1) and
    # for sessions whose last result had no scope.tier. Fixes the dogfood reap
    # incident where large-tier sessions fell back to the 60-min global default
    # because their first spawn had no last_result (#314).
    if task is not None and task.scope_hint is not None:
        return config.headless_timeout_by_tier.get(
            task.scope_hint, HEADLESS_TIMEOUT_SECONDS
        )
    return HEADLESS_TIMEOUT_SECONDS


def _is_headless(session: Session) -> bool:
    """Return True if session's worktree has a headless cw-context.json.

    Fail-open: returns False when worktree_path is None, or when the context
    file is missing or unreadable — a deleted worktree must not be falsely
    flagged as headless. Mirrors cli.py signal_stop at line 1003-1005.
    """
    if session.worktree_path is None:
        return False
    context_path = session.worktree_path / ".claude" / "cw-context.json"
    try:
        context = json.loads(context_path.read_text())
        return bool(context.get("headless")) if isinstance(context, dict) else False
    except (OSError, json.JSONDecodeError):
        return False


# Alias so _salvage_terminal_result can reference the shared constant by the
# private-looking name used throughout this module. The real definition lives
# in auto_dev_result.py as SALVAGE_TERMINAL_STATUSES — single source of truth
# so reconcile.py and cli.py cannot drift apart. See GitHub issues #372, #431.
_SALVAGE_TERMINAL_STATUSES: frozenset[str] = SALVAGE_TERMINAL_STATUSES

# Constants for _apply_sentinel_to_task (moved from cli.py; shared by both
# signal_stop and the ROUTE_EMITTED_SENTINEL reconcile path). See GitHub #578.
_VALIDATION_FAILED_MAX_ATTEMPTS = 3
_DETERMINISTIC_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_SCHEMA_VERSION_UNSUPPORTED}
)
_TRANSIENT_PARSE_FAILURES: frozenset[str] = frozenset(
    {BLOCKER_REASON_NO_RESULT_EMITTED}
)
_TERMINAL_NO_RETRY_STATUSES: frozenset[str] = SALVAGE_TERMINAL_STATUSES


class UsageLimitDetection(NamedTuple):
    """Outcome of scanning a session transcript for a usage-limit message (#1345).

    ``detected`` is True iff any post-start assistant record's text matched
    :data:`USAGE_LIMIT_RE`. ``matched_at`` is the ``timestamp`` of the LAST such
    matching record that carried a parseable timestamp (last-match-wins
    tie-break); ``None`` when nothing matched or no matching record had a usable
    timestamp. ``transcript_tail_at`` is the timestamp of the transcript's last
    content-bearing record — matched or not — via
    :func:`_last_content_entry_timestamp`; ``None`` when no record has a
    parseable timestamp. The recency gate (:func:`_usage_limit_is_recent`)
    compares the two so a stale limit message is not mistaken for a live cutoff.
    """

    detected: bool
    matched_at: datetime | None
    transcript_tail_at: datetime | None


def _parse_iso_timestamp(raw: object) -> datetime | None:
    """Parse a record's top-level ``"timestamp"`` value, or ``None`` if unusable."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _iter_assistant_records(path: Path) -> Iterator[tuple[datetime | None, str]]:
    """Yield ``(timestamp, text)`` for each assistant record in a jsonl transcript.

    ``timestamp`` is the record's top-level ``"timestamp"`` parsed via
    :func:`_parse_iso_timestamp`, or ``None`` when absent/malformed — the
    record is still yielded, because its text may match even without a usable
    anchor. ``text`` concatenates every text block of the assistant message.
    Follows the top-level-``"timestamp"`` convention of
    :func:`_last_content_entry_timestamp`. Yields nothing on any read error.
    """
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                text = "\n".join(
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
                yield _parse_iso_timestamp(record.get("timestamp")), text
    except OSError:
        return


def _detect_usage_limit(session: Session) -> UsageLimitDetection:
    """Scan the newest post-start transcript for a usage-limit message (#1345).

    Returns a :class:`UsageLimitDetection`: ``detected`` True iff any assistant
    record's text matched :data:`USAGE_LIMIT_RE`, ``matched_at`` the LAST
    matching record's timestamp (last-match-wins among records with a parseable
    timestamp), ``transcript_tail_at`` the transcript's last content-bearing
    timestamp. Uses :func:`_locate_session_transcript` for precise per-session
    lookup (surface_ref-prefix glob, #541). Never raises; returns an all-empty
    detection when the project dir is absent, no matching .jsonl exists, or the
    transcript predates the session start.
    """
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return UsageLimitDetection(
            detected=False, matched_at=None, transcript_tail_at=None
        )
    detected = False
    matched_at: datetime | None = None
    for ts, text in _iter_assistant_records(transcript):
        if USAGE_LIMIT_RE.search(text):
            detected = True
            if ts is not None:
                matched_at = ts  # last-match-wins
    return UsageLimitDetection(
        detected=detected,
        matched_at=matched_at,
        transcript_tail_at=_last_content_entry_timestamp(transcript),
    )


def _usage_limit_is_recent(
    detection: UsageLimitDetection,
    *,
    window_seconds: float,
    fail_open: bool = True,
) -> bool:
    """Decide whether a detected usage-limit message is *current* (#1345).

    Contract (operator resolution, issue #1345):
    - not detected → ``False``;
    - detected but either ``matched_at`` or ``transcript_tail_at`` is ``None``
      (no usable anchor) → return ``fail_open`` verbatim;
    - else → recent iff the message landed within ``window_seconds`` of the
      transcript's own tail:
      ``(transcript_tail_at - matched_at).total_seconds() <= window_seconds``.
    """
    if not detection.detected:
        return False
    if detection.matched_at is None or detection.transcript_tail_at is None:
        return fail_open
    gap = (detection.transcript_tail_at - detection.matched_at).total_seconds()
    return gap <= window_seconds


def _parse_sentinel_from_blocks(path: Path) -> AutoDevResult | BlockedResult | None:
    """Parse the LAST transcript block carrying a complete sentinel frame.

    Scans candidate blocks via :func:`_iter_sentinel_text_blocks` — assistant
    text AND ``tool_result`` (Bash stdout) blocks — returning the parse of the
    last block whose framing is complete. Last-match mirrors ``extract_block``'s
    §3.1 "LAST occurrence wins" rule (GitHub #591). Documented-example blocks
    (the illustrative ``pr=42 / PROJ-1234`` placeholder in the skill prompt)
    are skipped; if only an example block is present, returns None.

    A worker may emit the sentinel via ``cat <<EOF`` rather than as assistant
    text, landing the frame in a tool_result block; scanning only assistant
    text misses it and the stage stalls (GitHub #731). Returns ``None`` when no
    non-example block carries a complete frame.
    """
    last_result: AutoDevResult | BlockedResult | None = None
    for text in _iter_sentinel_text_blocks(path):
        block = extract_block(text)
        if block is not None:
            if _is_placeholder_sentinel_text(block):
                continue
            result = parse_stdout(text)
            if isinstance(result, AutoDevResult) and is_documented_example(result):
                continue
            last_result = result
    return last_result


def _salvage_terminal_result(
    session: Session,
) -> tuple[AutoDevResult, str] | None:
    """Recover a terminal-success AUTO_DEV_RESULT from the session's transcript.

    A headless session that emitted a valid sentinel and then stalled (e.g.
    sitting in ``wait_for_ci``) or crashed before session lifecycle completion
    may have its disposition lost. This recovers it directly from the transcript.

    Uses the same two-layer transcript search as
    :func:`_parse_any_sentinel_from_transcript` (csid-exact, then surface_ref-
    newest fallback when the csid transcript is absent or has no sentinel) so a
    terminal sentinel written before a resume/backfill is never missed
    (GitHub #1353; mirrors the #892 fix already applied to that sibling).

    Returns ``(result, claude_session_id)`` only when the parsed result is an
    :class:`AutoDevResult` whose status is in :data:`_SALVAGE_TERMINAL_STATUSES`.
    Returns ``None`` otherwise.
    """
    parsed = _parse_any_sentinel_from_transcript(session)
    if parsed is None:
        return None
    result, csid = parsed
    if (
        isinstance(result, AutoDevResult)
        and result.status in _SALVAGE_TERMINAL_STATUSES
    ):
        return result, csid
    return None


def _verify_salvaged_scope(result: AutoDevResult, session: Session) -> AutoDevResult:
    """Correct a salvaged sentinel's self-reported scope against git facts (#1487).

    Salvage recovers a sentinel the worker wrote about itself; nothing has
    checked its ``scope.files``/``scope.lines_actual`` against the branch. A
    config failure must not cost us the sentinel, so an unresolvable client
    falls back to the ``main`` default rather than propagating.
    """
    default_branch = resolve_scope_guard_default_branch(
        session.client, log_context=f"session={session.id}"
    )
    return reconcile_result_scope(
        result,
        worktree_path=session.worktree_path,
        default_branch=default_branch,
    )


def _parse_any_sentinel_from_transcript(
    session: Session,
) -> tuple[AutoDevResult | BlockedResult, str] | None:
    """Parse any sentinel from the transcript, regardless of status.

    Like :func:`_salvage_terminal_result` but applies no status filter — returns
    the result for any valid parse including PAUSED_FOR_USER_INPUT statuses that
    :func:`_salvage_terminal_result` would skip.  Returns None when no sentinel
    framing is present (the no-frame case ``parse_stdout`` reports as
    BLOCKER_REASON_NO_RESULT_EMITTED).

    Uses a two-layer transcript search (mirrors queue_peek's locate logic):

    - Layer 1: csid-exact transcript (``<project_dir>/<csid>.jsonl``).  Returned
      immediately when a sentinel is found, so the csid transcript always wins
      when it contains the result.
    - Layer 2: surface_ref newest-only transcript (``<surface_ref>*.jsonl``).
      Fires when the csid transcript is absent **or** contains no sentinel — the
      latter catches the case where a REVIEW worker emitted the sentinel before
      spawning fanout subagents (the sentinel lives in the pre-resume V1 transcript
      while backfill has already updated ``claude_session_id`` to the resumed V2
      session that has no sentinel).  Skipped when Layer 2 would return the same
      path already tried in Layer 1.

    Used by the ROUTE_EMITTED_SENTINEL detection path for sessions where the
    sentinel was emitted but the Stop hook never fired.  See GitHub #578, #731,
    #892.
    """

    def _try(path: Path) -> tuple[AutoDevResult | BlockedResult, str] | None:
        result = _parse_sentinel_from_blocks(path)
        if result is None or (
            isinstance(result, BlockedResult)
            and result.blocker.reason == BLOCKER_REASON_NO_RESULT_EMITTED
        ):
            return None
        if isinstance(result, AutoDevResult):
            result = _verify_salvaged_scope(result, session)
        return result, path.stem

    project_dir = _session_project_dir(session)

    # Layer 1: csid-exact (does NOT fall through to surface_ref)
    csid_transcript: Path | None = None
    if session.claude_session_id is not None and project_dir is not None:
        csid_transcript = locate_transcript(
            project_dir=project_dir,
            claude_session_id=session.claude_session_id,
            surface_ref=None,
            started_at=session.started_at,
        )
        if csid_transcript is not None:
            parsed = _try(csid_transcript)
            if parsed is not None:
                return parsed

    # Layer 2: surface_ref newest-only — fires when csid transcript absent or
    # contains no sentinel.  Skip when it resolves to the same path as Layer 1.
    if session.surface_ref is not None and project_dir is not None:
        surface_transcript = _newest_surface_ref_transcript(project_dir, session)
        if surface_transcript is not None and surface_transcript != csid_transcript:
            return _try(surface_transcript)

    return None


def classify_sentinel_stage_position(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> tuple[_StagePosition, list[Stage] | None, int | None]:
    """Circular-safe re-export of dispatch's stage-position classifier (#1149).

    ``cw.dispatch`` imports ``cw.reconcile`` at module level, so a top-level
    import of the classifier from a reconcile sweep would create a cycle. This
    thin wrapper (co-located with ``_apply_sentinel_to_task``, which delegates to
    dispatch the same way) lets stalled.py's Path 1 backstop resolve a sentinel's
    stage position against ``task.stage`` without an inline import at its own call
    site. Returns ``(position, stages, target_idx)``; see
    ``dispatch._classify_sentinel_stage_position`` for the semantics.
    """
    from cw.dispatch import _classify_sentinel_stage_position

    return _classify_sentinel_stage_position(task, last_result, clients)


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
    """

    rescued: bool
    routed: bool
    landed_terminal: bool


def _apply_sentinel_to_task(
    ticket_id: str,
    session: Session,
    sentinel: AutoDevResult | BlockedResult,
    *,
    now: datetime | None = None,
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
    arm's catch-all needs a transcript path to resolve liveness against
    (#1406); the task lookup itself still keys off ``session.id`` alone.
    ``now`` is the caller's sweep timestamp, threaded through to that liveness
    comparison so a reconcile tick judges every session against one clock;
    it defaults to wall-clock ``now`` for callers that have none.
    """
    cw_session_id = session.id
    with dev_queue_lock():
        store = load_dev_queue()
        target: TicketTask | None = None
        target_status: QueueItemStatus | None = None
        # #1189: track whether a same-ticket/session task was seen at all
        # (regardless of status) that did not win the occupied match --
        # distinguishes "raced to terminal by a concurrent caller" (R3a) from
        # "no such task anywhere" (R3b). Keep scanning past an excluded-status
        # match in case a later row has the occupied match (post-review
        # amendment A2).
        matched_excluded = False
        for task in store.tasks:
            if task.ticket_id == ticket_id and task.session_id == cw_session_id:
                if task.status in OCCUPIED_LANE_STATUSES:
                    target = task
                    target_status = task.status
                    break
                matched_excluded = True
        if target is None:
            if matched_excluded:
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
            return SentinelRouteOutcome(
                rescued=False, routed=not matched_excluded, landed_terminal=False
            )

        rescued = False
        routed = True
        landed_terminal = False
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
            routed = _route_blocked_result_to_task(target, session, sentinel, now=now)
            landed_terminal = not routed
        else:
            # True no-op: a late BlockedResult against an already-parked task
            # carries no success signal and must not write (#918).
            mutated = False

        if mutated:
            save_dev_queue(store)
        return SentinelRouteOutcome(
            rescued=rescued, routed=routed, landed_terminal=landed_terminal
        )


def _route_blocked_result_to_task(
    target: TicketTask,
    session: Session,
    sentinel: BlockedResult,
    *,
    now: datetime | None = None,
) -> bool:
    """Route a malformed/unparseable BlockedResult to a RUNNING task's status.

    A BlockedResult means the sentinel failed to parse or was malformed.
    Deterministic parse failures are terminal FAILED; validation_failed and
    transient failures re-queue to PENDING (clearing session_id) until the
    attempt cap. Extracted from _apply_sentinel_to_task to keep that function
    under the branch cap (#918).

    Returns False when this call just landed the task terminal-FAILED (the
    caller must not also complete the owning session on that outcome), True
    when it re-queued to PENDING instead (#1189).

    GitHub #1406: the unrecognized-reason catch-all is additionally vetoed by
    ``session``'s transcript liveness. #1281 gave the phantom sweep's
    stage-mismatch fall-through the same guard; this closes the alternative
    route to the same incident -- a still-advancing worker whose sentinel
    merely failed to *parse* was landed terminal-FAILED (and, via #1273's
    ``landed_terminal``, had its daemon stopped) while it was still working.
    Only the catch-all is guarded: a deterministic parse failure reproduces
    identically no matter how much further the worker gets, and
    validation_failed already carries its own attempt-cap tolerance. ``now``
    is the caller's sweep timestamp; it defaults to wall-clock ``now``.
    """
    # #1266: the two branches below (deterministic parse failures and
    # validation_failed at the attempt cap) are deliberately out of scope for
    # the last_blocked_result diagnostic write added below -- only the
    # unrecognized-reason catch-all gets it. Follow-up: neither of these
    # landings records *why* either, but widening scope here wasn't part of
    # this fix.
    if sentinel.blocker.reason in _DETERMINISTIC_PARSE_FAILURES:
        transition_task_status(target, QueueItemStatus.FAILED, disposition="abandoned")
        return False
    if sentinel.blocker.reason == BLOCKER_REASON_VALIDATION_FAILED:
        if target.attempts >= _VALIDATION_FAILED_MAX_ATTEMPTS:
            transition_task_status(
                target, QueueItemStatus.FAILED, disposition="abandoned"
            )
            return False
        transition_task_status(target, QueueItemStatus.PENDING)
        target.session_id = None
        return True
    if sentinel.blocker.reason in _TRANSIENT_PARSE_FAILURES:
        transition_task_status(target, QueueItemStatus.PENDING)
        target.session_id = None
        return True
    # An unparseable/unknown-status sentinel (status_unknown,
    # multiple_result_blocks, any unrecognized reason) carries NO success
    # signal. Never mark it COMPLETED — that silently retires unshipped work
    # as "shipped" (#750, the #728 loss). Surface as FAILED so the operator
    # sees it instead of a phantom completion.
    # #1406: ...unless the worker behind it is demonstrably still advancing.
    # An unparseable sentinel is evidence the *frame* was malformed, not that
    # the run is over -- a fresh transcript means killing it here would drop
    # work that is still in flight (the #1281 incident shape). Re-queue to
    # PENDING, same shape as the _TRANSIENT_PARSE_FAILURES branch above (no
    # last_blocked_result write: a re-queue rejects nothing), plus an audit
    # event -- unlike that branch, this one overrides what would otherwise be
    # a terminal FAILED landing, so it needs a durable trace an operator can
    # find later. record_event nests _inbox_lock inside dev_queue_lock here,
    # the same safe nesting order crud.py's _emit_task_deleted and
    # dev_queue/lifecycle.py already rely on (RFC 0008 W1, #978: the reverse
    # nesting -- _inbox_lock then dev_queue_lock -- never occurs). Not #765:
    # that ticket is the opposite lesson, a deadlock caused by record_event
    # inside dev_queue_lock in a since-fixed call site (_shared.py's own
    # SESSION_NEEDS_ATTENTION emission a few hundred lines below still emits
    # after the lock releases for that reason) -- citing it here as evidence
    # of safety would point a future reader at the wrong precedent.
    #
    # The `0 <= age` floor is load-bearing, not defensive: a negative age
    # means the transcript's mtime is *after* `now` (clock skew, or a caller
    # passing a fictional/frozen timestamp), which is not evidence of
    # liveness. Without the floor every such case is trivially `< WINDOW` and
    # would misclassify as LIVE. A None age (no locatable transcript) is
    # likewise not evidence -- both fall through to the FAILED landing below,
    # preserving pre-#1406 behavior wherever liveness is unknowable.
    _now = now if now is not None else datetime.now(UTC)
    age = _transcript_age_seconds(session, _now)
    if age is not None and 0 <= age < TRANSCRIPT_LIVENESS_WINDOW_SECONDS:
        transition_task_status(target, QueueItemStatus.PENDING)
        target.session_id = None
        record_event(
            OrchestratorEventType.SESSION_SENTINEL_LIVENESS_VETOED,
            {
                "ticket_id": target.ticket_id,
                "client": target.client,
                "session_id": session.id,
                "transcript_age_seconds": age,
                "blocker_reason": sentinel.blocker.reason,
            },
            correlation_id=target.ticket_id,
        )
        return True
    # #1266: persist the rejected sentinel so an operator can tell an absent
    # sentinel (last_blocked_result stays None) from a rejected one, instead
    # of a bare `abandoned` disposition with no diagnostic.
    target.last_blocked_result = sentinel.model_dump(mode="json")
    transition_task_status(target, QueueItemStatus.FAILED, disposition="abandoned")
    return False


def _session_project_dir(session: Session) -> Path | None:
    """Return the Claude project dir for *session*, or None if worktree path unset."""
    worktree = session.worktree_path
    if worktree is None:
        return None
    return claude_project_dir(worktree)


def _newest_surface_ref_transcript(project_dir: Path, session: Session) -> Path | None:
    """Return the newest ``<surface_ref>*.jsonl`` newer than session start, else None.

    Thin wrapper — delegates to ``locate_transcript`` for surface_ref-only
    resolution. Caller guarantees ``surface_ref`` is set.
    """
    return locate_transcript(
        project_dir=project_dir,
        claude_session_id=None,
        surface_ref=session.surface_ref,
        started_at=session.started_at,
    )


def _locate_session_transcript(session: Session) -> Path | None:
    """Return the session's transcript path, or None if not locatable.

    Thin wrapper — unpacks the Session and delegates to ``locate_transcript``.
    Resolution order:
    1. ``claude_session_id`` set and ``<project_dir>/<csid>.jsonl`` exists →
       return that path directly (mtime guard not needed; csid is exact).
    2. ``surface_ref`` set → newest ``<project_dir>/<surface_ref>*.jsonl``
       with mtime strictly after ``session.started_at``, else None
       (reused-worktree stale-transcript guard, #358/#372).
    3. No project_dir, or neither identifier set → None.
    """
    return locate_transcript(
        project_dir=_session_project_dir(session),
        claude_session_id=session.claude_session_id,
        surface_ref=session.surface_ref,
        started_at=session.started_at,
    )


def _csid_from_transcript(session: Session) -> str | None:
    """Return claude_session_id from the transcript filename, or None.

    Thin wrapper around :func:`_locate_session_transcript`.  The transcript
    is named ``<project_dir>/<full-csid>.jsonl`` where
    ``full-csid[:8] == surface_ref``; the stem is therefore the full csid.
    """
    path = _locate_session_transcript(session)
    if path is None:
        return None
    csid = path.stem
    _log.debug(
        "Resolved claude_session_id=%s for session %s via transcript fallback",
        csid,
        session.id,
    )
    return csid


def _detect_post_review_clean(session: Session) -> bool:
    """Return True iff the event bus has a post-review-clean marker for this session.

    Reads STAGE_ENTERED events from the inbox and checks for an event with
    payload["stage"] == _STAGE_REVIEW_COMPLETE correlated to session.id,
    with a time-window guard (event after session.started_at).

    Returns False on any error — conservative default.
    """
    if session.worktree_path is None:
        return False
    try:
        events = read_events(
            event_types=[OrchestratorEventType.STAGE_ENTERED],
            since_ts=session.started_at,
        )
    except Exception:  # noqa: BLE001 — events._parse_lines deliberately re-raises on interior corruption "so callers see real corruption"; this liveness check is the one caller that must not propagate that, so it fails safe to False instead
        return False
    for ev in events:
        session_id = ev.payload.get("session_id")
        stage = ev.payload.get("stage")
        if session_id == session.id and stage == _STAGE_REVIEW_COMPLETE:
            return True
    return False


def _effective_transcript_timestamp(transcript: Path) -> datetime:
    """Return the timestamp to use for liveness checks on *transcript*.

    Prefers the timestamp of the last *content-bearing* transcript entry
    (:func:`cw._util._last_content_entry_timestamp`) over the file's mtime,
    so a trailing metadata-only write (e.g. an ``ai-title`` record) does not
    falsely resurrect liveness or understate idle age (GitHub #1076). Falls
    back to mtime, unchanged from prior behavior, when no content entry has
    a parseable timestamp. Raises ``OSError`` if the file cannot be stat'd —
    callers are expected to catch it, matching existing fail-open behavior.
    """
    content_ts = _last_content_entry_timestamp(transcript)
    if content_ts is not None:
        return content_ts
    return datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)


def _project_transcripts_latest_timestamp(session: Session) -> datetime | None:
    """Return the max effective timestamp across ALL transcripts in the project dir.

    Widens the single-file csid/surface_ref resolution (#1283): a subagent's own
    transcript in the same project dir has a filename that matches neither the
    csid nor the ``<surface_ref>*`` prefix, so :func:`_locate_session_transcript`
    is blind to it even while it carries fresh activity — leaving a worker that
    is mid-subagent-delegation for >``TRANSCRIPT_LIVENESS_WINDOW_SECONDS`` (its
    own tail quiet) to look dead. Globs every ``*.jsonl`` under the project dir
    and returns the max :func:`_effective_transcript_timestamp` across files
    whose mtime is strictly after ``session.started_at`` — the same reused-
    worktree stale-transcript guard (#358/#372) that ``locate_transcript``
    applies to the surface_ref candidate, here applied per-file across the whole
    glob so a leftover prior-session transcript never counts. Fails open
    (``None``) on missing dir / ``OSError``, matching every sibling helper.

    Why no filename filter beyond the mtime guard: a worktree's project dir is
    reused sequentially per-ticket, never shared concurrently across unrelated
    tickets, so ``mtime > started_at`` alone bounds the glob to this session's
    lineage.
    """
    project_dir = _session_project_dir(session)
    if project_dir is None or not project_dir.is_dir():
        return None
    max_ts: datetime | None = None
    for candidate in project_dir.rglob("*.jsonl"):
        # Per-candidate: a stat/read failure on one sibling (deleted/rotated
        # mid-glob) must not discard max_ts already found from other, valid
        # siblings -- only that one candidate is skipped.
        try:
            mtime = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            if mtime <= session.started_at:
                continue
            ts = _effective_transcript_timestamp(candidate)
        except OSError:
            continue
        if max_ts is None or ts > max_ts:
            max_ts = ts
    return max_ts


def _widened_transcript_timestamp(session: Session) -> datetime | None:
    """Return the freshest liveness timestamp for *session*, or None (#1283).

    Takes the max of the single-file csid/surface_ref resolution
    (:func:`_locate_session_transcript` + :func:`_effective_transcript_timestamp`)
    and :func:`_project_transcripts_latest_timestamp` (the sibling-transcript
    glob). Monotonic widening — never reports a session as *more* stale than the
    registered transcript alone would. The two sources are computed
    independently: a stat failure on the registered transcript alone does not
    prevent the sibling-glob fallback from being consulted, so the widened
    signal fix (b) exists to add is never discarded by an unrelated primary-file
    error. Fails open (``None``) if both sources are unavailable.
    """
    best_ts: datetime | None = None
    transcript = _locate_session_transcript(session)
    if transcript is not None:
        try:
            best_ts = _effective_transcript_timestamp(transcript)
        except OSError:
            best_ts = None
    project_ts = _project_transcripts_latest_timestamp(session)
    if project_ts is not None and (best_ts is None or project_ts > best_ts):
        best_ts = project_ts
    return best_ts


def _transcript_recently_active(
    session: Session,
    now: datetime,
    *,
    window_seconds: int = TRANSCRIPT_LIVENESS_WINDOW_SECONDS,
) -> bool:
    """Return True if the transcript shows activity within *window_seconds* ago.

    Uses :func:`_widened_transcript_timestamp` — the max of the precise
    per-session lookup (surface_ref-prefix glob, #541) and any fresher sibling
    subagent transcript in the same project dir (#1283).  Returns False —
    permitting the watchdog to proceed — when no transcript is found
    (pre-first-write or path unavailable).  See GitHub #340.
    """
    try:
        ts = _widened_transcript_timestamp(session)
        if ts is None:
            return False
        return (now - ts).total_seconds() < window_seconds
    except OSError:
        return False


def _transcript_age_seconds(
    session: Session,
    now: datetime,
) -> float | None:
    """Return seconds since the session's transcript last showed activity, or None.

    Returns None when no transcript file can be located.  Uses
    :func:`_widened_transcript_timestamp` — the max of the precise per-session
    lookup (surface_ref-prefix glob, #541) and any fresher sibling subagent
    transcript in the same project dir (#1283).
    """
    try:
        ts = _widened_transcript_timestamp(session)
        if ts is None:
            return None
        return (now - ts).total_seconds()
    except OSError:
        return None


def _awaiting_subagent(session: Session, now: datetime) -> bool:
    """Return True if the worker is mid-tool/subagent (parent tail pending).

    A subagent's output only lands in the parent transcript when it returns,
    so the parent goes quiet during execution and mtime-based liveness
    false-positives. Detect the in-flight case: the last assistant turn is a
    ``tool_use`` with no following ``tool_result``, and that turn is within
    ``SUBAGENT_LIVENESS_WINDOW_SECONDS`` of *now* (a pending tool_use older than
    that is a hung subagent — not alive). See GitHub #384.

    Fail-open to False (permit the watchdog to proceed) on any read/parse error.
    """
    # Why: _locate_session_transcript applies the mtime > started_at guard
    # uniformly (#541).  A stale transcript (from a prior run in a reused
    # worktree) that previously could cause a false-positive "subagent pending"
    # signal now returns None → this function returns False (fail-open).
    # The behavior change is intentional and conservative.
    transcript = _locate_session_transcript(session)
    if transcript is None:
        return False
    try:
        if not transcript.is_file():
            return False

        last_tool_use_ts: datetime | None = None
        saw_result_after_tool_use = True
        for raw in transcript.read_text().splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            if etype == "assistant" and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in content
            ):
                ts = entry.get("timestamp")
                if isinstance(ts, str):
                    try:
                        last_tool_use_ts = datetime.fromisoformat(ts)
                    except ValueError:
                        last_tool_use_ts = None
                saw_result_after_tool_use = False
            elif etype == "user" and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                saw_result_after_tool_use = True

        if saw_result_after_tool_use or last_tool_use_ts is None:
            return False
        return (
            now - last_tool_use_ts
        ).total_seconds() < SUBAGENT_LIVENESS_WINDOW_SECONDS
    except OSError:
        return False


def _apply_salvaged_completion(
    session: Session,
    result: AutoDevResult,
    claude_session_id: str,
    *,
    now: datetime,
) -> EmitOutcome:
    """Mark ``session`` COMPLETED from a salvaged sentinel (like signal_stop).

    RFC 0012 A3 (#1459): the ``last_result`` write is routed through the door
    (:func:`emit_result_on`, source=SALVAGE_TRANSCRIPT) FIRST. On a first-
    writer-wins refusal -- another authority already recorded a terminal result
    for this session -- the status/completed_at/completed_reason/cost_usd/
    claude_session_id mutations are ALL skipped and the refusal ``EmitOutcome``
    is returned so the caller can drop this candidate from its downstream
    ticket-routing / event-emission accounting. The door's own ``emit_result_on``
    warning already logs ``existing_source``/``attempted_source`` on refusal, so
    no duplicate log is emitted here.

    Returns the ``EmitOutcome`` (``refused=True`` when the door declined). All
    four callers (phantom/idle/stalled/concierge) check ``.refused``.
    """
    outcome = emit_result_on(
        session,
        result.model_dump(mode="json"),
        source=LastResultSource.SALVAGE_TRANSCRIPT,
    )
    if outcome.refused:
        return outcome
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    if result.cost_usd is not None:
        session.cost_usd = result.cost_usd
    session.claude_session_id = claude_session_id
    return outcome


def _compute_worktree_dirty(client_name: str, branch: str | None) -> bool:
    """Return True when the worktree has unpushed commits or uncommitted changes.

    Fail-safe: returns False when branch is None or empty, the client config is
    absent, or any other error occurs — mirrors _cleanup_timed_out_worktree's
    pattern.
    """
    if not branch:
        return False
    try:
        client = get_client(client_name)
        return worktree_has_unsaved_work(client, branch)
    except Exception:  # noqa: BLE001 — fail-safe on any error (client lookup or git); mirrors _cleanup_timed_out_worktree
        return False


def _worktree_dirty_by_path(client_name: str, worktree_path: Path | None) -> bool:
    """Return True if the worktree at *worktree_path* has unsaved work.

    Uses worktree_path (always set on DAEMON sessions) instead of
    session.branch (always None on DAEMON sessions, making the branch-based
    check a production no-op).  Mirror _compute_worktree_dirty's fail-safe:
    returns False on any error, None path, or missing path.
    """
    if not worktree_path:
        return False
    try:
        branch = _deps.checked_out_branch(worktree_path)
        if not branch:
            return False
        client = get_client(client_name)
        return worktree_has_unsaved_work(client, branch)
    except Exception:  # noqa: BLE001 — fail-safe on any error; mirrors _compute_worktree_dirty
        return False


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


def _has_terminal_sentinel(session: Session) -> bool:
    """True when the session has already emitted a terminal sentinel.

    A real AUTO_DEV sentinel dump always carries a ``"status"`` key; the park
    markers (``silently_idle``/``needs_salvage``) carry ``"paused_status"`` and
    no ``"status"``. Key presence — not value — is the structural discriminant,
    so a parked session is correctly NOT treated as terminal and the idle
    watchdog re-checks it for a late terminal sentinel. See #418, #497.

    Thin delegation onto ``cw.result.has_terminal_result`` (RFC 0012 S2,
    #1456), which now owns this predicate since the emit_result_locked door
    also uses it to arbitrate first-writer-wins.
    """
    return has_terminal_result(session.last_result)


def resolve_idle_watchdog_budget(
    task: TicketTask | None,
    config: OrchestratorConfig,
) -> int:
    """Return the idle-watchdog budget (seconds) for a session's ticket.

    Precedence (highest first):
    1. task.idle_watchdog_override — explicit per-ticket escape hatch.
    2. config.idle_watchdog_by_stage[task.stage] — per-stage default, when
       task is not None and task.stage has an entry in the map.
    3. task.scope_hint — look up per-tier default in config.
    4. config.idle_watchdog_seconds — operator-tunable global default.
    5. IDLE_WATCHDOG_SECONDS — hardcoded fallback.
    """
    global_default = (
        IDLE_WATCHDOG_SECONDS
        if config.idle_watchdog_seconds is None
        else config.idle_watchdog_seconds
    )
    if task is None:
        return global_default
    if task.idle_watchdog_override is not None:
        return task.idle_watchdog_override
    stage_budget = config.idle_watchdog_by_stage.get(task.stage)
    if stage_budget is not None:
        return stage_budget
    if task.scope_hint is not None:
        tier_budget = config.idle_watchdog_by_tier.get(task.scope_hint)
        if tier_budget is not None:
            return tier_budget
    return global_default


def resolve_idle_retry_cap(
    task: TicketTask | None,
    config: OrchestratorConfig,
) -> int:
    """Return the idle-stall auto-retry cap for a session's ticket.

    Precedence: task.scope_hint per-tier override, else the global default.
    See GitHub issue #384.
    """
    if task is None:
        return DEFAULT_IDLE_RETRY_CAP
    if task.scope_hint is not None:
        tier_cap = config.idle_retry_cap_by_tier.get(task.scope_hint)
        if tier_cap is not None:
            return tier_cap
    return DEFAULT_IDLE_RETRY_CAP


def resolve_stalled_retry_cap(
    task: TicketTask | None,
    config: OrchestratorConfig,
) -> int:
    """Return the wall-clock-budget stalled-stage auto-retry cap for a ticket.

    Precedence: task.scope_hint per-tier override, else the global default.
    See GitHub issue #756.
    """
    if task is None:
        return DEFAULT_STALLED_RETRY_CAP
    if task.scope_hint is not None:
        tier_cap = config.stalled_retry_cap_by_tier.get(task.scope_hint)
        if tier_cap is not None:
            return tier_cap
    return DEFAULT_STALLED_RETRY_CAP


def resolve_reap_policy(
    candidate: ReapCandidate,
    clients: dict[str, ClientConfig],
    global_cfg: OrchestratorConfig,
) -> ReapPolicy:
    """Resolve the effective reap_policy for a candidate.

    Precedence (highest to lowest):
      1. Lane-level LaneConfig.reap_policy in the candidate's client config.
      2. Global OrchestratorConfig.reap_policy.
      3. ReapPolicy.SIGNAL_ONLY fail-safe (built into OrchestratorConfig default).

    A candidate whose client is absent from *clients* or whose lane name is not
    declared in that client's lanes falls through to the global config. This
    keeps behaviour identical to the pre-#560 flat read for any candidate that
    predates lane stamping.
    """
    client_cfg = clients.get(candidate.client) if candidate.client else None
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == candidate.lane and lane_cfg.reap_policy is not None:
                return lane_cfg.reap_policy
    return global_cfg.reap_policy


def _cleanup_timed_out_worktree(
    session: Session,
    ticket_id: str | None = None,
) -> None:
    """Remove a timed-out session's worktree so the re-dispatch starts clean.

    A timed-out DAEMON session has its ``TicketTask`` reverted to PENDING for
    re-dispatch. If its worktree is left on disk, ``create_worktree`` would
    reuse it (or, post-#404, refuse and spin) — either way feeding the retry a
    prior run's branch and commits. Removing it here means the next claim builds
    a fresh worktree from the current default branch. See GitHub issue #404.

    Dirty-check guard (#425): if the worktree contains uncommitted changes or
    unpushed commits the removal is SKIPPED.  Instead the owning task is flipped
    from PENDING back to BLOCKED_ON_USER so the operator can inspect the tree
    before it is removed.  *ticket_id* is required for the BLOCKED_ON_USER flip;
    when omitted the skip is logged but the queue is not mutated.

    Best-effort: every failure is logged and swallowed. Worktree cleanup must
    never abort the reconcile sweep — a missing/renamed client, an
    already-gone directory, or a git error is non-fatal.
    """
    if not session.branch:
        return
    try:
        client = get_client(session.client)
        if worktree_has_unsaved_work(client, session.branch):
            wt_path = str(worktree_path_for(client, session.branch))
            _log.warning(
                "worktree_cleanup_skip_dirty: %s/%s has unsaved work"
                " — leaving worktree at %s for operator inspection"
                " (ticket=%s)",
                session.client,
                session.branch,
                wt_path,
                ticket_id,
            )
            parked = False
            if ticket_id:
                with dev_queue_lock():
                    store = load_dev_queue()
                    for task in store.tasks:
                        if (
                            task.ticket_id == ticket_id
                            and task.status == QueueItemStatus.PENDING
                        ):
                            transition_task_status(
                                task,
                                QueueItemStatus.BLOCKED_ON_USER,
                                disposition="dirty_worktree",
                            )
                            save_dev_queue(store)
                            parked = True
                            break
            # Emitted after dev_queue_lock() releases (#1257): record_event
            # acquires _inbox_lock, so holding dev_queue_lock while acquiring
            # it risks deadlock with a concurrent process taking the two locks
            # in the opposite order (same rationale as this function's
            # sibling in reconcile/tasks.py, #765).
            if parked:
                record_event(
                    OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                    {
                        "session_id": session.id,
                        "session_name": session.name,
                        "client": session.client,
                        "ticket_id": ticket_id,
                        "claude_session_id": session.claude_session_id,
                        "paused_status": _DIRTY_WORKTREE_REASON,
                        "breadcrumbs": wt_path,
                        "crashed": False,
                        "lane": session.lane,
                    },
                    correlation_id=ticket_id,
                )
            return
        remove_worktree(client, session.branch, force=True)
    except (CwError, OSError) as exc:
        _log.warning(
            "worktree_cleanup_skip: %s/%s: %s",
            session.client,
            session.branch,
            exc,
        )
    else:
        # Audit breadcrumb for a destructive (force=True) removal — a skip is
        # logged above, so a successful reap leaves a matching record (#404).
        _log.info("worktree_cleanup_ok: %s/%s", session.client, session.branch)


def feature_branch_key(
    client_name: str,
    ticket_id: str,
    clients: dict[str, ClientConfig],
) -> str:
    """Return the git branch key for a ticket, respecting feature_branch_prefix.

    Looks up the client's :attr:`ClientConfig.feature_branch_prefix` (SSOT for
    the branch name the staged pipeline provisions and the auto-dev skills push
    to). Falls back to ``"dev"`` when the client is absent from *clients* so
    behaviour is identical to the old hardcoded ``"dev/" + ticket_id``.

    See GitHub issue #728.
    """
    client = clients.get(client_name)
    prefix = client.feature_branch_prefix if client is not None else "dev"
    return f"{prefix}/{ticket_id}"


_REAP_PROPOSED_ACTIONS: frozenset[ProposedAction] = frozenset(
    {
        ProposedAction.REVERT_TASK,
        ProposedAction.CRASH_COMPLETE,
        ProposedAction.PARK_BLOCKED_ON_USER,
    }
)


def _emit_reap_proposed(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    native_live: set[str],
    now: datetime | None = None,
) -> set[str]:
    """Emit SESSION_REAP_PROPOSED for reap-shaped candidates before act phase.

    Called from _reconcile_locked after each _detect_* and before the
    corresponding _act_on_*. Satisfies ADR-0006 invariant 3 (propose before act).

    Only emits for REVERT_TASK, CRASH_COMPLETE, PARK_BLOCKED_ON_USER candidates.
    Dedup: sessions with reap_proposed_at already set are skipped.

    Returns the set of session_ids newly stamped in this call. Callers use this
    to gate edge-triggered events (e.g. SESSION_STAGE_TIMED_OUT_RETRIED) so they
    fire only on first detection, not on every re-detect tick. See GitHub #782.

    save_state is safe under sessions_lock — it is a raw file write, not a
    reentrant lock acquisition. See existing _act_on_stalled_candidates,
    _act_on_idle_candidates.

    evidence.transcript_age_seconds reuses the same content-aware staleness
    computation (_transcript_age_seconds) the liveness veto decided on (#1427);
    evidence.transcript_mtime_age_seconds is the raw file-mtime age, retained
    separately for diagnostics.
    """
    _now = now or datetime.now(UTC)
    session_by_id = {s.id: s for s in state.sessions}
    newly_stamped: set[str] = set()

    for candidate in candidates:
        if candidate.proposed_action not in _REAP_PROPOSED_ACTIONS:
            continue
        session = session_by_id.get(candidate.session_id)
        if session is None or session.reap_proposed_at is not None:
            continue

        in_roster = (
            session.surface_ref is not None and session.surface_ref in native_live
        )

        # Content-aware staleness — same computation the liveness veto used to
        # make its park/no-park decision (#976, #1277), so the audit evidence
        # never diverges from what was actually decided (#1427).
        transcript_age_seconds = _transcript_age_seconds(session, _now)

        # Raw mtime age, retained separately for diagnostics only — a trailing
        # metadata-only record (queue-operation/ai-title/mode/...) can bump
        # this far above transcript_age_seconds; do not confuse the two (#1427).
        transcript_mtime_age_seconds: float | None = None
        transcript_path = _locate_session_transcript(session)
        if transcript_path is not None and transcript_path.exists():
            with contextlib.suppress(OSError):
                mtime = transcript_path.stat().st_mtime
                transcript_mtime_age_seconds = _now.timestamp() - mtime

        payload = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "lane": candidate.lane,
            "proposed_action": candidate.proposed_action.value,
            "reason": candidate.reap_reason.value if candidate.reap_reason else None,
            "evidence": {
                "elapsed_seconds": candidate.elapsed_seconds,
                "in_roster": in_roster,
                "transcript_age_seconds": transcript_age_seconds,
                "transcript_mtime_age_seconds": transcript_mtime_age_seconds,
            },
        }
        # Stamp before record_event: dedup guard fires on retry if write fails.
        session.reap_proposed_at = _now
        newly_stamped.add(candidate.session_id)
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload,
            correlation_id=candidate.ticket_id or candidate.session_id,
        )

    if newly_stamped:
        save_state(state)
    return newly_stamped


# Non-underscore aliases for the cross-cutting helpers above, so cluster modules
# can call them as public attributes (``_shared.NAME``) without tripping the
# private-member-access lint. Routing every cluster's call through the single
# ``_shared`` attribute preserves the pre-split property that one test patch at
# ``cw.reconcile._shared.NAME`` intercepts all callers. These helpers are not
# called elsewhere inside this module, so there is no dual-name hazard.
detect_usage_limit = _detect_usage_limit
usage_limit_is_recent = _usage_limit_is_recent
salvage_terminal_result = _salvage_terminal_result
worktree_dirty_by_path = _worktree_dirty_by_path
