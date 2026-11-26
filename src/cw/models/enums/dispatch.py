"""Dispatch skip-reason enum (#3739)."""

from __future__ import annotations

from enum import StrEnum


class DispatchSkipReason(StrEnum):
    """First-match skip_reason values emitted in dispatch.tick events.

    Precedence (highest first):
    AVAILABILITY_GATE > SSH_KEY_GATE > DISK_PRESSURE_GATE > FRESHNESS_GATE
    > USAGE_LIMITED
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
    DISK_PRESSURE_GATE ranks third (#1887, split from #1858): a per-client
    ``shutil.disk_usage`` probe of the worktree-base mount, checked before the
    freshness gate so a nearly-full disk holds the client PENDING rather than
    letting that gate's ``git pull --ff-only`` -- and then a whole session --
    write more data onto it.
    ATTEMPT_CAP_BLOCKED is emitted per-task when the global attempt ceiling
    parks a task; it is not part of the per-client-tick precedence chain.
    STALE_PR_BLOCKED (#1862) is likewise per-task and outside the precedence
    chain: it is emitted when the pre-dispatch open-PR gate parks a PLAN/IMPL-
    stage task whose branch already carries an open, unmerged PR.
    WORKTREE_OCCUPIED (#2077) is likewise per-task and outside the precedence
    chain: emitted when a PENDING task's per-ticket worktree is currently held
    by a live session or daemon worker -- caught either before the row is
    claimed (the pre-claim occupancy screen) or by a genuinely-live
    ``HookContextConflictError`` release during spawn.
    BINDING_BLOCKED (#2442, renamed by RFC 0019 H2b #3744 from
    ``tracker_mcp_gate_blocked``) is likewise per-task and outside the
    precedence chain: emitted when the tick-side reachability check parks a
    task whose harness verifiably cannot reach the client's tickets binding.
    TICKET_CLOSED (#1605) is likewise per-task and outside the precedence
    chain: emitted when the closed-ticket guard cancels a PENDING row whose
    GitHub issue is CLOSED.
    HOST_CAPACITY_GATED ranks just above CAP_FULL (#1444): a fleet-wide
    ``OrchestratorConfig.host_session_budget`` ceiling on concurrently-running
    DAEMON sessions across the whole host, folded into the per-client
    ``available_client_slots`` admission math ahead of the per-client cap
    check so an operator can distinguish "this client's own cap is full"
    from "the whole host is out of budget".
    """

    AVAILABILITY_GATE = "availability_gate"
    SSH_KEY_GATE = "ssh_key_gate"
    DISK_PRESSURE_GATE = "disk_pressure_gate"
    FRESHNESS_GATE = "freshness_gate"
    USAGE_LIMITED = "usage_limited"
    HOST_CAPACITY_GATED = "host_capacity_gated"
    CAP_FULL = "cap_full"
    LANE_CAP_BLOCKED = "lane_cap_blocked"
    ATTEMPT_CAP_BLOCKED = "attempt_cap_blocked"
    STALE_PR_BLOCKED = "stale_pr_blocked"
    WORKTREE_OCCUPIED = "worktree_occupied"
    BINDING_BLOCKED = "binding_blocked"
    TICKET_CLOSED = "ticket_closed"
    SPAWN_ERROR = "spawn_error"
    LANE_CIRCUIT_PAUSED = "lane_circuit_paused"
    SPAWN_ERROR_BACKOFF = "spawn_error_backoff"
    NO_PENDING = "no_pending"
    NONE = "none"
