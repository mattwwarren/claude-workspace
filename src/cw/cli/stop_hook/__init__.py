"""The ``cw signal-stop`` Stop-hook backstop.

Extracted verbatim from ``cw.cli.sessions`` (module-size split): the Stop-hook
handler and its headless-resolution helpers are a separate concern from the
session lifecycle commands. Wired in via ``.claude/settings.local.json``
written by spawn into each dispatched session's worktree; see
:func:`signal_stop` for the full contract (GitHub #133, #147, #151, #176).

It was split out of a single ``cli/stop_hook.py`` module (#2496); every
``from cw.cli.stop_hook import X`` site is preserved here via re-exports, and
importing this package imports ``command``, which registers ``signal-stop``
on ``main``. Submodules, in dependency order:

- ``_constants`` -- reason and key constants, and the pinned
  :data:`_LOGGER_NAME`. Imports from no sibling.
- ``payload`` -- stdin payload and ``cw-context.json`` resolution. Imports
  from no sibling.
- ``park`` -- the #2135 abandoned-exit park. Imports from no sibling.
- ``agent_stamp`` -- ``agent_spawn_stamp`` snapshot and clear. Imports
  ``_constants``.
- ``sentinel`` -- headless sentinel parse, scope check, reconstruction and
  emit-door harvest. Imports ``_constants``.
- ``staged_emit`` -- staged ``cw result emit`` bookkeeping. Imports
  ``_constants``.
- ``headless`` -- :class:`_HeadlessResolution` and the headless resolution.
  Imports ``sentinel``, ``park`` and ``staged_emit``.
- ``locked`` -- the ``sessions_lock`` window, the only submodule that imports
  ``sessions_lock`` or ``load_state``. Imports ``headless``.
- ``command`` -- the ``signal-stop`` click command and its post-lock actions.
  Imports ``_constants``, ``payload``, ``agent_stamp``, ``staged_emit`` and
  ``locked``.

Every submodule logs under the pinned name ``cw.cli.stop_hook`` (never
``__name__``), so the emitted logger name is unchanged by the split.

A test that monkeypatches a module global the code reads
(``get_native_daemon_client``, ``load_state``, ``emit_result_locked``, ...)
must target the submodule that owns the reading function: this package
re-exports only its own names, so a patch on its namespace raises
``AttributeError`` instead of silently not intercepting.
"""

from __future__ import annotations

import logging

from cw.cli.stop_hook._constants import (
    _LOGGER_NAME,
    _SENTINEL_UNROUTABLE_PAGED_KEY,
    _SENTINEL_UNROUTABLE_REASON,
    _STAGED_ROUTE_RESCUED_KEY,
    _STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY,
)
from cw.cli.stop_hook.agent_stamp import (
    _agent_spawn_stamp_is_clear,
    _clear_agent_spawn_stamp,
    _snapshot_agent_spawn_stamp,
)
from cw.cli.stop_hook.command import (
    _build_completed_payload,
    _handle_unrouted_stop,
    _page_sentinel_unroutable,
    _sentinel_unroutable,
    signal_stop,
)
from cw.cli.stop_hook.headless import (
    _HeadlessResolution,
    _resolve_and_complete_headless_session,
)
from cw.cli.stop_hook.locked import (
    _handle_user_origin_stop,
    _LockedStop,
    _resolve_stop_under_lock,
)
from cw.cli.stop_hook.park import (
    _armed_running_task,
    _park_if_abandoned,
    _sentinel_frame_follows_marker,
)
from cw.cli.stop_hook.payload import (
    _read_stop_hook_payload,
    _resolve_signal_stop_context,
)
from cw.cli.stop_hook.sentinel import (
    _handle_headless_no_sentinel,
    _harvest_last_result_through_door,
    _parse_headless_sentinel,
    _reconstruct_emitted_sentinel,
    _verify_headless_scope,
)
from cw.cli.stop_hook.staged_emit import (
    _clear_staged_emit_result_marker,
    _maybe_clear_staged_emit_result,
    _maybe_stamp_sentinel_unroutable_paged,
    _peek_staged_emit_result,
    _restore_staged_route_outcome,
    _sentinel_unroutable_already_paged,
    _stamp_staged_route_outcome,
)

logger = logging.getLogger(_LOGGER_NAME)

__all__ = [
    "_SENTINEL_UNROUTABLE_PAGED_KEY",
    "_SENTINEL_UNROUTABLE_REASON",
    "_STAGED_ROUTE_RESCUED_KEY",
    "_STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY",
    "_HeadlessResolution",
    "_LockedStop",
    "_agent_spawn_stamp_is_clear",
    "_armed_running_task",
    "_build_completed_payload",
    "_clear_agent_spawn_stamp",
    "_clear_staged_emit_result_marker",
    "_handle_headless_no_sentinel",
    "_handle_unrouted_stop",
    "_handle_user_origin_stop",
    "_harvest_last_result_through_door",
    "_maybe_clear_staged_emit_result",
    "_maybe_stamp_sentinel_unroutable_paged",
    "_page_sentinel_unroutable",
    "_park_if_abandoned",
    "_parse_headless_sentinel",
    "_peek_staged_emit_result",
    "_read_stop_hook_payload",
    "_reconstruct_emitted_sentinel",
    "_resolve_and_complete_headless_session",
    "_resolve_signal_stop_context",
    "_resolve_stop_under_lock",
    "_restore_staged_route_outcome",
    "_sentinel_frame_follows_marker",
    "_sentinel_unroutable",
    "_sentinel_unroutable_already_paged",
    "_snapshot_agent_spawn_stamp",
    "_stamp_staged_route_outcome",
    "_verify_headless_scope",
    "logger",
    "signal_stop",
]
