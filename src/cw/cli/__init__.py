"""Click CLI dispatcher for cw commands.

``cli`` is a package: the root ``main`` group and shared framework helpers live
in :mod:`cw.cli._base`, and each command area is implemented in a submodule that
registers its commands onto ``main`` at import time. Importing the submodules
here (for their registration side effects) assembles the full command tree.

Names re-exported below preserve the historical ``from cw.cli import X`` surface
relied on by the test suite and ``cw.__main__``.
"""

from __future__ import annotations

# Command submodules — imported for their command-registration side effects.
from cw.cli import (
    agent_spawn_stamp,
    channels,
    config_cmds,
    focus,
    guard,
    maintenance,
    queues,
    review,
    session_inspect,
    sprint,
    statusline,
    stop_hook,
    watchdog,
    worktree,
)
from cw.cli._base import (
    _complete_client,
    _complete_session,
    _configure_logging,
    _relative_time,
    main,
)
from cw.cli._sentinels import (
    _parse_sentinel_from_transcript,
    _sentinel_present_in_transcript,
)
from cw.cli.dev_queue import (
    _WAIT_EXIT_ATTENTION,
    _WAIT_EXIT_BLOCKED,
    _WAIT_EXIT_FAILED,
    _WAIT_EXIT_SIGNOFF,
    _WAIT_EXIT_TIMEOUT,
)
from cw.cli.orchestrate import (
    _ORCHESTRATOR_AGENT,
    _ORCHESTRATOR_CHANNEL,
    _drain_reap_proposals,
    _format_status_human,
)
from cw.cli.session_inspect import session_group
from cw.cli.sessions import _display_sessions, _display_status
from cw.cli.spawn import (
    _spawn_close_impl,
    _spawn_close_requeue_impl,
    _spawn_complete_impl,
    _spawn_create_impl,
)
from cw.reconcile import _apply_sentinel_to_task
from cw.reconcile._shared import _transcript_age_seconds
from cw.result import result as result_group

# Result command group is defined in cw.result; attach it to the root group.
main.add_command(result_group)
# Session inspection group defined in cw.cli.session_inspect.
main.add_command(session_group)

__all__ = [
    "_ORCHESTRATOR_AGENT",
    "_ORCHESTRATOR_CHANNEL",
    "_WAIT_EXIT_ATTENTION",
    "_WAIT_EXIT_BLOCKED",
    "_WAIT_EXIT_FAILED",
    "_WAIT_EXIT_SIGNOFF",
    "_WAIT_EXIT_TIMEOUT",
    "_apply_sentinel_to_task",
    "_complete_client",
    "_complete_session",
    "_configure_logging",
    "_display_sessions",
    "_display_status",
    "_drain_reap_proposals",
    "_format_status_human",
    "_parse_sentinel_from_transcript",
    "_relative_time",
    "_sentinel_present_in_transcript",
    "_spawn_close_impl",
    "_spawn_close_requeue_impl",
    "_spawn_complete_impl",
    "_spawn_create_impl",
    "_transcript_age_seconds",
    "agent_spawn_stamp",
    "channels",
    "config_cmds",
    "focus",
    "guard",
    "main",
    "maintenance",
    "queues",
    "review",
    "session_group",
    "session_inspect",
    "sprint",
    "statusline",
    "stop_hook",
    "watchdog",
    "worktree",
]
