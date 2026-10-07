"""Dependency-free constants shared across ``cw.models.orchestrator_config``.

The DAG root of the package: imports nothing from ``cw``. Holds the config
defaults ``OrchestratorConfig`` reads, the backend names, the per-worktree
relative paths and cw-context.json keys that several layers which cannot import
each other share, ``extract_unresolved_spawn_count``, and :data:`_LOGGER_NAME`,
the one logger name every submodule of the package logs under.
"""

from __future__ import annotations

from pathlib import Path

# The pre-split module's ``__name__``. Pinned so the package split (#2497)
# leaves the logger name operators filter on unchanged; every submodule logs
# via ``logging.getLogger(_LOGGER_NAME)``, never ``__name__``.
_LOGGER_NAME = "cw.models.orchestrator_config"

# Ceiling on TicketTask.unproductive_attempts -- claims that left RUNNING
# with no evidence of progress (#786, re-pointed at the narrower counter
# by #1750). NOT a cap on the raw `attempts` claim counter, which bumps
# once per pipeline stage and is never compared against this value
# (#2256). Enforced at exactly one seam: dispatch/claim/screening.py.
# Lives here so OrchestratorConfig.global_attempt_ceiling can reference it
# directly without a circular import (dispatch.py imports from models.py).
DEFAULT_GLOBAL_ATTEMPT_CEILING = 10


# Judgment default for the claim-time disk-pressure gate (#1887, split from
# #1858) -- conservative and open to tuning per host/mount, not derived from a
# measured incident threshold. Named (not an inline literal at the field
# default) so an operator or reviewer can find it by name, same convention as
# DEFAULT_GLOBAL_ATTEMPT_CEILING above.
DEFAULT_DISK_PRESSURE_MIN_FREE_GB = 5.0

# Inode dimension of the same claim-time gate (#2470): a tmpfs can exhaust its
# inodes long before its bytes (the 2026-09-27 ENOSPC incident). The gate
# refuses a spawn when free inodes fall below
# ``max(DEFAULT_DISK_PRESSURE_MIN_FREE_INODES,
# DEFAULT_DISK_PRESSURE_MIN_FREE_INODE_FRACTION * total_inodes)`` -- the
# absolute floor protects small mounts, the fraction scales to large ones.
# Judgment defaults, same posture as DEFAULT_DISK_PRESSURE_MIN_FREE_GB above.
DEFAULT_DISK_PRESSURE_MIN_FREE_INODES = 50_000
DEFAULT_DISK_PRESSURE_MIN_FREE_INODE_FRACTION = 0.05


CLAUDE_NATIVE_BACKEND: str = "claude-native"
LOCAL_BACKEND: str = "local"
CODEX_BACKEND: str = "codex"
OPENCODE_BACKEND: str = "opencode"

# Relative path of the per-worktree materialized ticket context, shared by
# dispatch's pre-spawn invalidation (#1046) and local_runner's prompt builder
# so the two never drift onto different literal paths for the same file.
CONTEXT_JSON_RELATIVE_PATH: Path = Path(".cw", "context.json")

# Relative path of the per-worktree hook/correlation context written at spawn
# (``spawn._write_hook_context``). Distinct LAYER from CONTEXT_JSON_RELATIVE_PATH
# above: that file is *ticket* context (title/body/comments) materialized by
# Stage 0 and deleted by dispatch's stale-context invalidation (#1046), while
# this one carries *dispatch/session* state (session ids, queue_metadata) and
# survives a rescued respawn. Shared by the writer and by the readers of
# ``queue_metadata`` so a reader can never drift onto the other file's literal
# path — the defect #1730 shipped, where the pending_operator_comment read
# pointed at .cw/context.json and silently always returned False.
HOOK_CONTEXT_RELATIVE_PATH: Path = Path(".claude", "cw-context.json")

# Relative path of the per-worktree scratch directory every dispatched worker
# (Claude, aider, opencode, codex) gets as its TMPDIR/TMP/TEMP (#2470), so no
# worker writes scratch files to the host's shared /tmp tmpfs. Lives under the
# already-excluded ``.cw/`` tree and is removed with the worktree. Shared by
# ``cw.worktree.resolve_worker_tmpdir`` and every reader, same one-literal
# convention as CONTEXT_JSON_RELATIVE_PATH above.
WORKER_TMPDIR_RELATIVE_PATH: Path = Path(".cw", "tmp")

# Keys of the ``agent_spawn_stamp`` object inside cw-context.json (#1646).
# Three modules touch this one object across two layers that cannot import
# each other -- ``cw.spawn`` seeds it, ``cw.cli.agent_spawn_stamp``'s
# PreToolUse/PostToolUse pair increments and decrements it, and
# ``cw.reconcile._shared`` reads it during phantom classification. They live
# beside HOOK_CONTEXT_RELATIVE_PATH for exactly the reason its own docstring
# gives: a reader that hand-types the literal is one typo away from silently
# always returning the default, the defect #1730 shipped.
#
# Shape: {"unresolved_count": int, "last_stamped_at": isoformat str | None}.
# A counter rather than a flag because Claude Code can dispatch several
# subagent tool_use blocks in one assistant turn, so two Pre hooks can fire
# before either Post does -- a boolean would lose the second spawn.
AGENT_SPAWN_STAMP_KEY = "agent_spawn_stamp"
AGENT_SPAWN_UNRESOLVED_COUNT_KEY = "unresolved_count"
AGENT_SPAWN_LAST_STAMPED_AT_KEY = "last_stamped_at"

# Set True in cw-context.json by a successful ``cw result emit`` (#2458).
# The Stop hook's lock-free peek (``cw.cli.stop_hook._peek_staged_emit_result``)
# reads this flag instead of ``load_state()`` -- the peek's whole reason to
# exist is a near-zero-cost check on every Stop-hook fire with pending
# background_tasks, which a fleet-wide sessions.json load defeats. Lives here
# for the same reason AGENT_SPAWN_STAMP_KEY does: ``cw.result`` (writer) and
# ``cw.cli.stop_hook`` (reader) cannot import each other directly, so both
# import the literal from this shared, dependency-free module.
STAGED_EMIT_RESULT_KEY = "staged_emit_result"

# Tool names the ``cw background-tool-guard-pre`` hook (#2303) is both wired to
# and branches on: ``cw.spawn._build_hook_settings`` writes them as PreToolUse
# matchers, and ``cw.cli._background_tool_policy`` compares the payload's
# ``tool_name`` against them. One spelling for both sides, so a matcher the
# classifier does not recognise cannot ship as a silent no-op. Here rather than
# in the policy module for the reason the keys above give: ``cw.spawn`` cannot
# import ``cw.cli`` (``cw.cli`` imports ``cw.spawn``).
BASH_TOOL_NAME = "Bash"
MONITOR_TOOL_NAME = "Monitor"


def extract_unresolved_spawn_count(context: dict[str, object]) -> int:
    """Return the ``agent_spawn_stamp`` counter in *context*, or 0 for any odd shape.

    Shared by ``cw.cli.agent_spawn_stamp`` (write side) and
    ``cw.reconcile._shared`` (read side) so the two independent readers of
    this on-disk shape cannot silently drift onto different validation rules
    (#1646 review finding) -- reconcile cannot import ``cw.cli``, so this
    lives here instead, beside the key constants both layers already import.

    A missing/non-dict stamp, a missing count, a non-int count, or a ``bool``
    masquerading as an int (``bool`` is an ``int`` subclass in Python, so
    ``True`` would otherwise read as a live count of 1) all read as 0.
    """
    stamp = context.get(AGENT_SPAWN_STAMP_KEY)
    if not isinstance(stamp, dict):
        return 0
    count = stamp.get(AGENT_SPAWN_UNRESOLVED_COUNT_KEY)
    if isinstance(count, bool) or not isinstance(count, int):
        return 0
    return count
