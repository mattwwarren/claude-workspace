"""Wedge detection and reap for ``cw doctor``.

Split out of ``cw.doctor.core`` (#1314, part 2). Holds the wedge-condition
detectors (RUNNING tasks with no/completed/dead session, repo-ahead-of-queue,
BLOCKED_ON_USER dead-session, ACTIVE-no-daemon-entry, ACTIVE-daemon-stale,
ACTIVE-null-liveness-orphan) plus the reap that acts
on actionable findings (:func:`_reap_wedge_findings`) and the
BLOCKED_ON_USER collapse helper (:func:`_collapse_blocked_on_user_tasks`).
The class-11 stranded-routed-result detector and its operator close live in
``cw.doctor.routed_result_wedge`` (#2524); this package only wires its close
into the reap tail.

``task_running`` imports :func:`_gh_pr_states` and ``reap`` imports
:func:`_reap_session_by_selector` from ``loop_health`` at top level (2-symbol
direction); ``loop_health``'s reach back for
:func:`_collapse_blocked_on_user_tasks` is a function-local deferred import of
this package to break the cycle.

This package was split out of a single ``doctor/wedge.py`` module (#2164);
every ``from cw.doctor.wedge import X`` site is preserved here via
re-exports. Submodules, in dependency order:

- ``_constants`` -- the wedge-class and park-disposition constants, and the
  pinned :data:`_LOGGER_NAME`. Imports from no sibling.
- ``task_running`` -- the RUNNING-task detectors (classes 2, 3 and 4) and
  :func:`_resolve_wedge_branch`; the only reader of ``run_git``. Imports no
  sibling.
- ``blocked_on_user`` -- the BLOCKED_ON_USER detectors (classes 5 and 7),
  their predicates, and the collapse/cancel queue mutators; the only
  submodule that logs. Imports ``_constants``.
- ``session_liveness`` -- the ACTIVE-session liveness detectors (classes 6
  and 8) and the :func:`_daemon_supervisor_alive` outage guard; the only
  reader of ``_ROSTER_PATH`` and ``load_orchestrator_config``. Imports
  ``_constants``.
- ``orphans`` -- the null-liveness orphan detector (class-9, advisory only)
  with its backend-resolution and recipe helpers, and the leaked daemon
  worker detector (class-10). Imports ``_constants``.
- ``reap`` -- the ``--reap`` remedy (:func:`_reap_wedge_findings`) and its
  bounded-lock session-reap, routed-result close and leaked-worker sweep
  tail. Imports ``_constants`` and ``blocked_on_user``.

A test that monkeypatches a module global the code reads
(``get_native_daemon_client``, ``run_git``, ``_ROSTER_PATH``, ...) must
target the submodule that owns the reading function: this package re-exports
only its own names, so a patch on its namespace raises ``AttributeError``
instead of silently not intercepting.
"""

from __future__ import annotations

from cw.doctor.wedge._constants import (
    _DIRTY_WORKTREE_DISPOSITION,
    _HUMAN_GATED_PARK_DISPOSITIONS,
    _WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL,
    _WEDGE_ACTIVE_NO_DAEMON_ENTRY,
    _WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN,
    _WEDGE_BLOCKED_DEAD_SESSION,
    _WEDGE_LEAKED_DAEMON_WORKER,
    _WEDGE_TERMINAL_SIBLING,
)
from cw.doctor.wedge._constants import (
    _LOGGER_NAME as _LOGGER_NAME,
)
from cw.doctor.wedge.blocked_on_user import (
    _cancel_terminal_sibling_parks,
    _check_wedge_dead_session_blocked_on_user,
    _check_wedge_terminal_sibling_park,
    _collapse_blocked_on_user_tasks,
    _is_dead_session_task,
    _is_terminal_sibling_disposition,
    _is_terminal_sibling_park,
    _log,
)
from cw.doctor.wedge.orphans import (
    _check_wedge_active_null_liveness_orphan,
    _check_wedge_leaked_daemon_worker,
    _is_null_liveness_candidate,
    _null_liveness_orphan_recipe,
    _resolve_backend_for_orphan_check,
)
from cw.doctor.wedge.reap import (
    _REAP_CHECK_NAME,
    _reap_daemon_sessions,
    _reap_sessions_and_sweep,
    _reap_timeout_check,
    _reap_wedge_findings,
)
from cw.doctor.wedge.session_liveness import (
    _check_wedge_active_daemon_stale_no_sentinel,
    _check_wedge_active_no_daemon_entry,
    _daemon_supervisor_alive,
)
from cw.doctor.wedge.task_running import (
    _check_wedge_repo_ahead,
    _check_wedge_task_running_completed_session,
    _check_wedge_task_running_no_session,
    _resolve_wedge_branch,
)

__all__ = [
    "_DIRTY_WORKTREE_DISPOSITION",
    "_HUMAN_GATED_PARK_DISPOSITIONS",
    "_REAP_CHECK_NAME",
    "_WEDGE_ACTIVE_DAEMON_STALE_NO_SENTINEL",
    "_WEDGE_ACTIVE_NO_DAEMON_ENTRY",
    "_WEDGE_ACTIVE_NULL_LIVENESS_ORPHAN",
    "_WEDGE_BLOCKED_DEAD_SESSION",
    "_WEDGE_LEAKED_DAEMON_WORKER",
    "_WEDGE_TERMINAL_SIBLING",
    "_cancel_terminal_sibling_parks",
    "_check_wedge_active_daemon_stale_no_sentinel",
    "_check_wedge_active_no_daemon_entry",
    "_check_wedge_active_null_liveness_orphan",
    "_check_wedge_dead_session_blocked_on_user",
    "_check_wedge_leaked_daemon_worker",
    "_check_wedge_repo_ahead",
    "_check_wedge_task_running_completed_session",
    "_check_wedge_task_running_no_session",
    "_check_wedge_terminal_sibling_park",
    "_collapse_blocked_on_user_tasks",
    "_daemon_supervisor_alive",
    "_is_dead_session_task",
    "_is_null_liveness_candidate",
    "_is_terminal_sibling_disposition",
    "_is_terminal_sibling_park",
    "_log",
    "_null_liveness_orphan_recipe",
    "_reap_daemon_sessions",
    "_reap_sessions_and_sweep",
    "_reap_timeout_check",
    "_reap_wedge_findings",
    "_resolve_backend_for_orphan_check",
    "_resolve_wedge_branch",
]
