"""Reconcile report, reap-candidate and proposed-action types.

:class:`ProposedAction`, :class:`ReapCandidate` and :class:`ReconcileReport`,
plus the two small helpers that fold routed-sentinel and correction-signal
fields into a candidate. Imports nothing from its siblings. Split out of the
flat ``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from cw.models import DEFAULT_LANE, DEFAULT_STAGE, LastResultSource, ReapReason, Stage

if TYPE_CHECKING:
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult, BlockedResult


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
    # See GitHub #578. Since #2426, stalled.py is a second producer: a live
    # session's foreign INTERMEDIATE_ADVANCE_STATUSES result reclassifies
    # from COMPLETE_FOREIGN_RESULT into this action too.
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
    # Proposal-only (#2524): a live session whose staged result a #2458
    # partial route already routed, stranded with no occupied row bound to
    # it. Nothing in reconcile acts on it, and cw orchestrate run's reap
    # drain never authorizes it; the operator closes it via cw doctor --reap
    # or cw spawn close.
    CLOSE_ROUTED_RESULT_SESSION = "close_routed_result_session"


@dataclass(frozen=True)
class ReapCandidate:
    """Classification result from detect phase. Consumed by act dispatcher.

    See GitHub #552, ADR-0006.
    """

    session_id: str
    proposed_action: ProposedAction
    ticket_id: str | None = None
    worktree_dirty: bool = False
    # Why the worktree is dirty (uncommitted changes or unpushed commits),
    # or None for a clean worktree. Computed in phantom detect alongside
    # worktree_dirty, from the same worktree_path, and carried payload-only
    # into the SESSION_PHANTOM_REVERTED event for operator visibility — never
    # read by resolve_reap_policy or any routing decision. Non-null iff
    # worktree_dirty is True. See GitHub #2118.
    worktree_dirty_reason: str | None = None
    salvage_result: AutoDevResult | None = None
    salvage_csid: str | None = None
    # ROUTE_EMITTED_SENTINEL carries the full parsed result (any status).
    # COMPLETE_FOREIGN_RESULT also carries its validated foreign result here
    # (a second producer of this same field). See #1470. stalled.py's
    # reclassified INTERMEDIATE_ADVANCE_STATUSES case is a third producer,
    # onto ROUTE_EMITTED_SENTINEL rather than a new field. See #2426.
    routed_sentinel: AutoDevResult | BlockedResult | None = None
    # Which authority produced ``routed_sentinel``, threaded into the
    # ``_apply_sentinel_to_task_audited`` call's ``source`` kwarg instead of a
    # hardcoded literal (#2458). Defaults to the transcript-reparse producer
    # (the common case across every ReapCandidate site); the idle sweep's
    # staged-``cw result emit`` producer (``_staged_emit_candidate``) is the
    # one override, so an emit_cli-originated result routed via that backstop
    # is audited with its true source instead of misattributed to
    # SALVAGE_TRANSCRIPT.
    result_source: LastResultSource = LastResultSource.SALVAGE_TRANSCRIPT
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
    # True when the session's worktree carries an unresolved subagent-spawn
    # stamp (#1646) — the worker died or hung with a sub-agent spawn still in
    # flight. Computed in phantom detect alongside worktree_dirty, from the
    # same worktree_path, and read in the act phase to select a distinct
    # disposition and to override a lane's reap_policy: auto. Fail-open: False
    # whenever the evidence is missing or unreadable.
    unresolved_subagent_spawn: bool = False
    # True when the session's transcript carries the provider-overload
    # (API 529) signature (#1923). Computed DAEMON-only in phantom detect,
    # mirroring usage_limit_detected/unresolved_subagent_spawn above.
    # PAYLOAD-ONLY per A1/R1: surfaced solely in the SESSION_PHANTOM_REVERTED
    # payload for operator visibility -- never read by resolve_reap_policy,
    # never aggregated into ReconcileReport.usage_limited or any other report
    # field, and never overrides reap_policy: signal_only the way
    # unresolved_subagent_spawn does. Stricter than both cited precedents.
    provider_overload_detected: bool = False


def _resolve_routed_sentinel(
    candidate: ReapCandidate,
) -> AutoDevResult | BlockedResult | None:
    """Return the sentinel *candidate* should be routed with, or None to skip it.

    Shared by ``phantom._mutations._apply_phantom_routed_mutations`` and
    ``idle._mutations._apply_idle_routed_mutations`` (GitHub #1762): both iterate
    a ROUTE_EMITTED_SENTINEL-only candidate list and used to duplicate this guard
    byte-for-byte, each additionally requiring ``salvage_csid``.

    ``salvage_csid`` is NOT required, and dropping it is what lets phantom's
    post-#1762 ``reconstruct_staged_sentinel`` producer -- which reads
    ``session.last_result`` and so has no transcript to derive a csid from --
    be routed instead of silently dropped. Safe because
    ``_apply_sentinel_to_task`` keys its task lookup off ``session.id`` alone,
    and its ``BlockedResult`` arm resolves liveness through
    ``_locate_session_transcript``'s three-tier fallback, landing on the
    pre-#1406 FAILED default whenever the csid is unresolvable.

    Returns the sentinel rather than a bare bool so callers bind a narrowed
    local, keeping ``mypy --strict`` able to see it is non-``None`` past the
    guard without either of them re-asserting the field.
    """
    return candidate.routed_sentinel


def _apply_correction_signal_fields(
    payload: dict[str, object], candidate: ReapCandidate
) -> None:
    """Merge the #1625 correction-signal fields onto a stalled_retry_cap_parked
    payload. Caller must already have gated on the disposition/reap_reason —
    shared by the two sites that can produce this disposition (SESSION_
    NEEDS_ATTENTION and SESSION_REAP_PROPOSED) so they cannot drift apart on
    which fields are copied from the candidate.
    """
    payload["regress_attempts"] = candidate.regress_attempts
    payload["spawn_error_count"] = candidate.spawn_error_count


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
    """

    phantom_session_ids: list[str] = field(default_factory=list)
    phantom_session_names: list[str] = field(default_factory=list)
    reverted_ticket_ids: list[str] = field(default_factory=list)
    completed_ticket_ids: list[str] = field(default_factory=list)
    usage_limited: bool = False
