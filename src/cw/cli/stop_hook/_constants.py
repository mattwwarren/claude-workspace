"""Reason and key constants for the ``cw.cli.stop_hook`` package.

The ``sentinel_unroutable`` page reason, the ``session.last_result`` keys the
staged-emit route merges in, and :data:`_LOGGER_NAME`, the pinned logger name
every submodule of this package logs under. Imports nothing from its
siblings: the root of the package's layering. Split out of the flat
``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

# Pinned logger name for every record this package emits. Deliberately a
# literal, not ``__name__``: the pre-split ``cli/stop_hook.py`` logged under
# this fixed name, and anything filtering/configuring logging by exact logger
# name must keep seeing it after the package split (#2496).
_LOGGER_NAME = "cw.cli.stop_hook"

# ``paused_status`` of the signal-only ``session.needs_attention`` page fired
# when a staged emit_cli result reaches the resolution step and neither its
# reconstruction nor the transcript fallback yields a routable sentinel
# (#2458). See docs/session-disposition.md §6d.
_SENTINEL_UNROUTABLE_REASON = "sentinel_unroutable"

# Merged-in (never overwriting) companions to reconcile._shared's own
# _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY, stamped onto session.last_result
# alongside it in the same complete_session=False accepted-route branch
# (#2458 fix cycle 5, Action 2). The already_routed short-circuit skips
# _apply_sentinel_to_task entirely on the completing call, which otherwise
# leaves rescued/task_already_terminal at their init-False defaults even
# when the first (partial-route) call's outcome was rescued=True (a #918
# late-parked-task rescue) -- silently dropping that fact from the eventual
# SESSION_COMPLETED payload. Local to this module (not reconcile/_shared/,
# out of this cycle's approved scope): only this function reads or writes
# them.
_STAGED_ROUTE_RESCUED_KEY = "sentinel_partial_route_rescued"
_STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY = "sentinel_partial_route_task_already_terminal"
# Merged-in (never overwriting) flag stamped onto session.last_result the
# first time the no-sentinel/not-parked bail below is found genuinely
# pageable (#2458 fix cycle 6). Deliberately NOT
# reconcile._shared._SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY: that flag means
# "actually routed" and is read by holds_staged_emit_result, the shared
# candidacy predicate for the idle sweep and cw spawn close's retry path --
# setting it here (a route that never succeeded) would wrongly tell those
# two call sites the result had been routed and block them from ever
# retrying it. This flag only dedups the WARNING + SESSION_NEEDS_ATTENTION
# sentinel_unroutable page across repeat Stops that land on the same
# still-unroutable bail; it is local to this module and read only by
# _sentinel_unroutable, never by holds_staged_emit_result.
_SENTINEL_UNROUTABLE_PAGED_KEY = "sentinel_unroutable_paged"
