"""Wedge-class and park-disposition constants for ``cw.doctor.wedge``.

The ``wedge/...`` class strings the detectors stamp onto findings and the
reap keys off, the park dispositions the BLOCKED_ON_USER detector and collapse
exclude, and :data:`_LOGGER_NAME`, the pinned logger name every logging
submodule of this package uses. Imports nothing from its siblings: the root of
the package's layering. Split out of the flat ``doctor/wedge.py`` (#2164).
"""

from __future__ import annotations

from cw.auto_dev_result import PAUSED_FOR_USER_INPUT_STATUSES

# Pinned logger name for every record this package emits. Deliberately a
# literal, not ``__name__``: the pre-split ``doctor/wedge.py`` logged under
# this name, and anything filtering/configuring logging by exact logger name
# must keep seeing it after the package split (#2164).
_LOGGER_NAME = "cw.doctor.wedge"

# Wedge class for BLOCKED_ON_USER tasks whose sessions are dead (OOM/crash path).
_WEDGE_BLOCKED_DEAD_SESSION = "wedge/blocked-on-user-dead-session"

# Wedge class for BLOCKED_ON_USER tasks parked terminal_sibling (#2100): a
# duplicate row minted by a lock-contention race for a ticket whose real row
# already reached a terminal status (see
# ``cw.reconcile.tasks.park_terminal_sibling_tasks``). Deliberately distinct
# from ``_WEDGE_BLOCKED_DEAD_SESSION`` above: that class's remedy (revert the
# oldest blocked row to PENDING) has nothing to revert THIS row to — the
# ticket's real row already finished — so reverting it just gets it re-parked
# terminal_sibling on the very next reconcile pass, a silent ping-pong. Both
# the class-5 detector and ``_collapse_blocked_on_user_tasks`` exclude this
# disposition outright (see ``_is_terminal_sibling_park``) so a row can only
# ever surface here, with CANCEL — never a PENDING revert — as its --reap
# remedy (``_cancel_terminal_sibling_parks``).
_WEDGE_TERMINAL_SIBLING = "wedge/terminal-sibling-park"

# Dispositions marking a park that waits on a HUMAN, not on a wedge (#1653):
# the four sentinel statuses stamped verbatim onto the task at park time.
# A human-gated park's worker has legitimately exited, so the dead-session
# heuristic matches every one of them — but reverting one to PENDING
# mechanically re-dispatches a ticket with zero new information and produces
# the identical park (observed: 10 retries at a fixed cadence, ~20.5h, ending
# in manual queue removal). These parks are released by an operator verb
# (requeue/approve) or a gate recipe reading fresh tracker state, never by
# the reap path. Sourced from the schema constant so the sets cannot drift.
_HUMAN_GATED_PARK_DISPOSITIONS: frozenset[str] = PAUSED_FOR_USER_INPUT_STATUSES

# A dirty-worktree park (#425/#2114) is the same shape: session_id is None by
# construction (the pre-spawn guard in dispatch/claim.py never spawned one;
# the reconcile paths clear it), so the dead-session heuristic always matches
# it -- but reverting it to PENDING re-claims the same stale worktree, which
# re-derives the same park and, before #2114, charged another attempt each
# time. Its remedy is an operator reading the breadcrumb (which now names the
# predicate and base ref) and committing, pushing, or removing the tree, then
# `cw dev-queue requeue`; never the reap path. Kept as a local literal rather
# than added to the schema set above, which enumerates sentinel statuses.
_DIRTY_WORKTREE_DISPOSITION = "dirty_worktree"

# Wedge class for ACTIVE/IDLE sessions with no matching daemon entry (crash/SSH
# failure path that leaves roster absent but session still "active" in cw state).
_WEDGE_ACTIVE_NO_DAEMON_ENTRY = "wedge/active-no-daemon-entry"

# Wedge class for ACTIVE DAEMON sessions still present ("idle") in the daemon
# roster with a stale transcript and no terminal sentinel (#2078). Mirror of
# _WEDGE_ACTIVE_NO_DAEMON_ENTRY for the opposite roster shape: that class
# fires when the daemon entry is ABSENT (crash/SSH failure); this one fires
# when the entry is PRESENT but the harness never signaled completion (the
# stop_hook/command.py background_tasks permanent-defer race) -- see
# _check_wedge_active_daemon_stale_no_sentinel's docstring for the mechanism.
_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL = "wedge/active-daemon-stale-no-sentinel"

# Wedge class for ACTIVE/IDLE DAEMON sessions carrying NEITHER liveness channel
# -- no daemon surface_ref and no local_liveness handle -- past their spawn
# grace (#2237). compute_drift skips a null surface_ref (load-bearing for the
# local/opencode executors, whose local_liveness reconcile/local.py owns), so
# class-6 never sees such a row, yet dispatch's running_count still counts it
# against the client ceiling. Advisory only (ADR-0014): the eligibility test is
# absence plus elapsed time, with no roster, PID, or terminal-result evidence,
# so --reap never mutates it; the recipe names `cw spawn close <id>` instead.
_WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN = "wedge/active-null-liveness-orphan"

# Wedge class for daemon roster workers whose surface_ref names a cw session
# already TERMINAL, or no cw session at all (#2480). Distinct from every
# class above: those key off a queue task or cw Session; this one keys off a
# live *roster* entry that has outlived its (or never had a) owning session
# -- the leak that makes cw.worktree.live_home_reason report a finished
# ticket's worktree occupied forever. See cw.reconcile.leaked_workers for the
# shared detection/stop authority this check and its --reap remedy both use.
_WEDGE_LEAKED_DAEMON_WORKER = "wedge/leaked-daemon-worker"
