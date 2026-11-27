"""Reap reason and reap policy enums (#3739)."""

from __future__ import annotations

from enum import StrEnum


class ReapReason(StrEnum):
    """Reason taxonomy for queue.session_reaped bus events.

    Each value maps to exactly one reconcile disposition path. See the
    reap-site decision table in GitHub issue #380.
    """

    PHANTOM_SURFACE = "phantom_surface"
    IDLE_STALL = "idle_stall"
    USAGE_LIMIT_CUTOFF = "usage_limit_cutoff"
    # GitHub #2324 — a still-roster-present worker whose transcript tail is a
    # usage-limit message with no sentinel. Distinct from USAGE_LIMIT_CUTOFF,
    # which names the roster-absent salvage path.
    USAGE_LIMIT_MID_TURN = "usage_limit_mid_turn"
    RETRY_CAP_PARKED = "retry_cap_parked"
    STALLED_RETRY_CAP_PARKED = "stalled_retry_cap_parked"
    WALL_CLOCK_BUDGET = "wall_clock_budget"
    COMPLETED_BACKSTOP = "completed_backstop"
    SALVAGE_COMPLETED = "salvage_completed"
    SALVAGE_PARKED = "salvage_parked"
    FINALIZE_BLOCKED = "finalize_blocked"
    TERMINAL_SIBLING = "terminal_sibling"
    # GitHub #1713 — a BLOCKED_ON_USER row parked behind a gate condition
    # (own PR pending merge, or blocked behind another ticket's open PR)
    # whose blocking PR has since merged. release_stale_gated_tasks detects
    # and (under ReapPolicy.AUTO) releases these rows.
    STALE_GATE = "stale_gate"
    # GitHub #2285 — a codex review orphaned by a crash still has a live codex
    # writer in its worktree at boot, or the process scan cannot rule one out.
    # The boot pass never signals it. Proposal-only: surfaces via
    # session.reap_proposed, never stamped as a session's reap_reason.
    CODEX_ORPHAN_LIVE_WRITER = "codex_orphan_live_writer_at_boot"
    # GitHub #2524 — an ACTIVE DAEMON session whose staged result a #2458
    # partial route already routed (the row advanced), with no occupied row
    # still bound to it and a stale transcript. Two jobs: the reason on the
    # reconcile sweep's session.reap_proposed page, and the reap_reason
    # stamped on the session when an operator closes it via cw doctor --reap.
    # GitHub #2715: cw.reconcile.shipped_release stamps it too when it closes
    # the shipped-with-own-PR subset, differing from the doctor close only in
    # completed_reason=NORMAL (doctor: USER) and its session.completed event
    # carrying pr_url.
    ROUTED_RESULT_STRANDED = "routed_result_stranded"
    # GitHub #3328 (Part B of #2615) — an unbound RUNNING row whose claiming
    # process is gone, with no worker homed on its worktree and no context
    # tying it to a session. A queue disposition and the session.reap_proposed
    # reason only: there is no Session, so it is never stamped as a session's
    # reap_reason.
    UNOWNED_RUNNING_ROW = "unowned_running_row"


class ReapPolicy(StrEnum):
    """Policy controlling whether the reconciler destroys a stalled session.

    Under ``SIGNAL_ONLY`` (default): route the owning task to BLOCKED_ON_USER,
    leave session/worktree/daemon surface intact.  Cleared by an explicit
    operator command (the orchestrator session counts, ADR-0020).
    Under ``AUTO``: self-healing — stop daemon, revert task to PENDING for retry.
    No reconcile act removes the worktree (ADR-0014).
    """

    SIGNAL_ONLY = "signal_only"
    AUTO = "auto"
