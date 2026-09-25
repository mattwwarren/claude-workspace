"""Session spawn helpers shared between CLI and dispatch loop."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.atomic import atomic_write_text
from cw.auto_dev_result import AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION
from cw.config import (
    load_orchestrator_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.events import record_event
from cw.exceptions import (
    CwError,
    HookContextConflictError,
    SpawnUnregisteredError,
    WorktreeError,
)
from cw.models import (
    AGENT_SPAWN_LAST_STAMPED_AT_KEY,
    AGENT_SPAWN_STAMP_KEY,
    AGENT_SPAWN_UNRESOLVED_COUNT_KEY,
    BASH_TOOL_NAME,
    HOOK_CONTEXT_RELATIVE_PATH,
    MONITOR_TOOL_NAME,
    PLAN_APPROVED_FINGERPRINT_KEY,
    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY,
    SCOPE_DRIFT_APPROVED_HEAD_KEY,
    TERMINAL_SESSION_STATUSES,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    TicketTask,
)
from cw.native_daemon import (
    get_native_daemon_client,
    resolve_permission_mode,
    wait_for_roster_presence,
)
from cw.reconcile import _csid_from_transcript, ticket_id_for_session
from cw.session_retention import find_session_by_id
from cw.worktree import live_home_reason

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger(__name__)

# Schema version for cw-context.json. Increment when the shape changes so
# workers can detect whether they are reading a context written by an older cw.
# v2: added `workspace_path` (#766 — forbidden main-checkout path for git guard).
# v3: added `queue_metadata.regressed_into_stage` (#1794 — per-arrival signal to
#     the impl-stage Pre-Stage Detector Guard that this IMPL entry was reached
#     via a deliberate backward stage move, not a fresh dispatch or an ordinary
#     forward advance).
# v4: added `queue_metadata.pending_operator_comment` (#1730 — per-arrival
#     signal to the REVIEW stage that this entry followed a regress and may
#     carry an operator send-back comment to treat as a binding adjudication
#     input rather than background context).
# v5: added `agent_spawn_stamp` (#1646 — unresolved-subagent-spawn counter
#     maintained by the `cw agent-spawn-pre` / `cw agent-spawn-post` hook pair
#     and read by the phantom sweep, so a worker that died with a sub-agent
#     spawn still in flight parks under its own disposition instead of the
#     generic phantom_surface).
# v6: added `lane` (#1946 — the dispatch lane this worker's task sits in, so
#     the `cw guard-busy-wait` PreToolUse hook can resolve its per-lane config
#     override from inside the hook subprocess. The hook has no cw session or
#     queue row in hand, only this file; without the key every worker would
#     resolve the global default and the per-lane knob would be unreachable.
#     Null for USER-origin sessions, which have no lane.)
# v7: added `queue_metadata.plan_approved_at` (dev-queue schema v35 — the
#     tracker-neutral record that `cw dev-queue approve` released this row's
#     PLAN-stage approval gate, as an ISO-8601 timestamp or null. Read by
#     auto-dev-plan.md's Checkpoint 1 as operator approval evidence, so a
#     Linear-tracked ticket — whose tracker the GitHub-only `--post-marker`
#     comment never reaches — stops re-parking at plan_pending_approval on
#     every re-dispatch.)
# v8: added `queue_metadata.plan_approved_fingerprint` (dev-queue schema v36 —
#     GitHub #2102 — the SHA-256 fingerprint of the draft the v7 approval was
#     given for, as a hex string or null. Checkpoint 1 compares it against the
#     draft it is about to auto-skip; without it, v7's timestamp proves only
#     that some approval happened, so a draft edited after approval resumed
#     straight past the Large-scope carve-out.)
# v9: added `queue_metadata.scope_drift_approved_extra_files` and
#     `queue_metadata.scope_drift_approved_head` (dev-queue schema v40 —
#     GitHub #2337 — the operator's `cw dev-queue approve --scope-drift` grant,
#     as a path list and a commit SHA, or nulls. Step 2.5 gate 2 passes the
#     paths to check_plan_scope_conformance.py as an allowlist while the SHA is
#     still an ancestor of origin/<branch>.)
CW_CONTEXT_SCHEMA_VERSION = 9


def build_disallowed_tools_arg(patterns: list[str]) -> list[str]:
    """Return the single ``--disallowed-tools=<patterns>`` argv token, or [].

    Empty *patterns* → ``[]`` (cw forwards no restriction). Non-empty → one
    ``=``-joined token whose value is the patterns comma-joined; claude accepts
    a comma/space-separated list, so every pattern rides one token. Callers pass
    ``OrchestratorConfig.disallowed_mcp_tools``, whose validator rejects any
    comma-bearing entry — so the comma-join here cannot split a single pattern.

    The ``=``-joined single-token form is mandatory. ``claude``'s
    ``--disallowed-tools <tools...>`` is variadic: as the two-token form
    ``["--disallowed-tools", pattern]`` it greedily consumes the following
    positional — the worker prompt — leaving the worker promptless (it idles,
    emits no transcript). The ``=`` form binds exactly one value and cannot
    reach the prompt. See GitHub #733 (the regression this shape prevents) and
    #726 (the former hard-coded, tracker-gated Linear block this replaces —
    now ``OrchestratorConfig.disallowed_mcp_tools``).
    """
    if not patterns:
        return []
    return [f"--disallowed-tools={','.join(patterns)}"]


# Max chars kept for blocker.details in prior_attempts_summary entries — long
# details (tracebacks, test output) would bloat the context injected into the
# next retry's prompt. 500 chars captures the failure type without dragging in
# megabytes of pane scrollback.
_PRIOR_ATTEMPT_DETAILS_MAX_LEN = 500

# Roster-registration verification: after spawn_bg returns a short id, poll
# roster.json until the id appears. Isolates the silent-spawn flake (#520)
# where the supervisor accepts the short id without adopting the worker.
# Sized to be well under SPAWN_GRACE_SECONDS (30s) so reconcile's grace gate
# does not fire before this check can fail fast.
_ROSTER_POLL_INTERVAL_SECS: float = 1.0
_ROSTER_POLL_TIMEOUT_SECS: float = 10.0
_SPAWN_FAIL_REASON_UNREGISTERED = "spawn_unregistered"


# Why: this function's completeness for a given (client, ticket_id) depends
# entirely on session_retention.prune_sessions()'s dev-queue exemption
# (#1983) — it never scans archives itself. A spawn only happens because a
# live dev-queue row exists for this ticket, and the exemption guarantees
# every terminal session matching that row's (client, ticket_id) stays in
# sessions.json regardless of age. If that exemption is ever removed or
# narrowed, this function silently starts returning truncated retry
# history with no error — check session_retention.py before changing it.
def _collect_prior_attempts_summary(
    ticket_id: str, *, client: str
) -> list[dict[str, object]]:
    """Return compact failure summaries for prior sessions on *ticket_id*.

    Called only when task.attempts > 0. Scans persisted state for TIMED_OUT or
    COMPLETED sessions belonging to *client* whose name encodes *ticket_id*,
    builds one entry per session from last_result, sorts by completed_at
    ascending, and returns the list. Returns [] on any exception so a
    state-read failure never blocks spawn.
    """
    try:
        state = load_state()
        matching = [
            s
            for s in state.sessions
            if s.client == client
            and s.status in TERMINAL_SESSION_STATUSES
            and ticket_id_for_session(s.name) == ticket_id
        ]
        matching.sort(key=lambda s: s.completed_at or s.started_at)
        summaries: list[dict[str, object]] = []
        for sess in matching:
            result = sess.last_result
            if result is None:
                summaries.append(
                    {
                        "status": "no_sentinel",
                        "stage_reached": None,
                        "blocker_reason": None,
                        "blocker_details": None,
                        "friction_highlights": [],
                    }
                )
                continue
            blocker = result.get("blocker") or {}
            blocker_dict = blocker if isinstance(blocker, dict) else {}
            raw_details = str(blocker_dict.get("details", ""))
            details_str = raw_details[:_PRIOR_ATTEMPT_DETAILS_MAX_LEN]
            summaries.append(
                {
                    "status": result.get("status"),
                    "stage_reached": result.get("stage_reached"),
                    "blocker_reason": blocker_dict.get("reason"),
                    "blocker_details": details_str,
                    "friction_highlights": result.get("friction_highlights") or [],
                }
            )
    except Exception:  # noqa: BLE001 — safety net; spawn must not fail on retry-hints read
        _log.warning(
            "prior_attempts_summary: failed to collect for ticket=%r client=%r; "
            "falling back to []",
            ticket_id,
            client,
            exc_info=True,
        )
        return []
    else:
        return summaries


def _git_clean_env() -> dict[str, str]:
    """Return os.environ with GIT_* vars stripped.

    GIT_* vars (e.g. GIT_DIR, GIT_WORK_TREE) can misdirect git commands to the
    wrong repository when cw itself runs inside a git hook. Strip them so every
    subprocess.run git call operates on the path it is explicitly given via -C.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _verify_roster_registration(
    daemon: NativeDaemonClient,
    short_id: str,
    ticket_id: str | None = None,
    *,
    timeout: float = _ROSTER_POLL_TIMEOUT_SECS,
    interval: float = _ROSTER_POLL_INTERVAL_SECS,
) -> None:
    """Poll the daemon roster until *short_id* appears, or raise.

    After ``claude --bg`` returns a short id, the supervisor may still not have
    adopted the worker (GitHub issue #520). Polling here catches the flake
    within the spawn call rather than leaving a phantom RUNNING session that
    burns a 30-minute idle cycle before the watchdog reaps it.

    Emits a ``SESSION_SPAWN_UNREGISTERED`` event before raising so the failure
    is diagnosable in the event inbox.
    """
    if wait_for_roster_presence(
        daemon, short_id, present=True, timeout=timeout, interval=interval
    ):
        return
    _log.warning(
        "spawn_unregistered: worker %r absent from roster after %.0fs poll; "
        "treating spawn as failed (ticket=%s)",
        short_id,
        timeout,
        ticket_id,
    )
    record_event(
        OrchestratorEventType.SESSION_SPAWN_UNREGISTERED,
        {
            "surface_ref": short_id,
            "ticket_id": ticket_id,
            "reason": _SPAWN_FAIL_REASON_UNREGISTERED,
            "poll_timeout_secs": timeout,
        },
        correlation_id=ticket_id,
    )
    msg = (
        f"Spawned worker {short_id!r} never appeared in the daemon roster "
        f"within {timeout:.0f}s ({_SPAWN_FAIL_REASON_UNREGISTERED}). "
        "The supervisor likely did not adopt the worker; treat spawn as failed."
    )
    raise SpawnUnregisteredError(msg)


def _validate_worktree(path: Path) -> None:
    """Ensure *path* is a real git worktree, not an empty dir.

    Catches the #186 symptom: a prior ``git worktree add -b <branch>``
    failed (e.g. branch already taken) but the directory was mkdir'd
    by the shell anyway, leaving cw spawn to run on an empty dir.
    """
    if not path.exists():
        msg = f"Worktree path does not exist: {path}"
        raise WorktreeError(msg)
    if not (path / ".git").exists():
        msg = (
            f"Worktree path is not a git checkout: {path} (missing .git/). "
            f"A prior 'git worktree add' likely failed; check that the "
            f"branch name was not already taken."
        )
        raise WorktreeError(msg)
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
        env=_git_clean_env(),
    )
    if result.returncode != 0:
        msg = (
            f"Worktree path failed 'git rev-parse --git-dir': {path}\n"
            f"stderr: {result.stderr.strip()}"
        )
        raise WorktreeError(msg)


# Hook matcher for the subagent-spawning tool (#1646).
#
# The name was settled EMPIRICALLY, not from the ticket's prose. A temporary
# catch-all hook installed in a live dispatch worktree (2026-08-12) captured a
# real subagent spawn's PreToolUse and PostToolUse payloads: both report
# `"tool_name": "Agent"`. The ticket text (and Claude Code's older docs) call
# it the `Task` tool, so `Task` is kept as an alternation branch for version
# robustness -- a wrong single guess here means the mechanism silently never
# fires, with no fixture able to catch it.
#
# Anchored deliberately. Claude Code treats the matcher as a regex, and an
# unanchored `Task` would also match unrelated tool names (`TaskStop`), pairing
# a spurious increment with a spurious decrement. The anchored form was
# verified against the same live harness: it fires for a subagent spawn and
# does not fire for `Bash`.
#
# 2026-08-19 re-verification (#1947): the PostToolUse:Agent decrement this
# matcher used to drive was confirmed HOLLOW by replaying a live async
# `Agent(isolation="worktree")` spawn (session ea2f3d42, ticket #1902) --
# `PostToolUse:Agent` fired at 13:12:09.513Z, ~3.9s after the matching
# PreToolUse and paired with the `Async agent launched successfully.`
# tool_result at 13:12:09.763Z (launch-return), while the harness's own
# turn_duration record at 13:12:13.028Z still reported
# `pendingBackgroundAgentCount: 1`. The stamp balanced to 0 while the
# subagent was, per the harness's own accounting, still running. The
# PostToolUse wiring for this matcher is removed; `cw signal-stop`
# (`cli/stop_hook.py`) now snapshots/clears `agent_spawn_stamp.unresolved_count`
# off the Stop hook payload's own `background_tasks` list instead -- a signal
# that tracks the harness's live turn-accounting rather than a tool-call
# return that races ahead of it. This matcher constant is now read only by
# the PreToolUse entry below.
_AGENT_TOOL_MATCHER = "^(Agent|Task)$"

# One command backs both the Bash and the Monitor PreToolUse entries (#2303),
# so the two refusals share a single classifier and cannot drift apart. The
# matchers themselves are cw.models.BASH_TOOL_NAME / MONITOR_TOOL_NAME, the same
# constants that classifier branches on.
_BACKGROUND_TOOL_GUARD_COMMAND = "cw background-tool-guard-pre"


def _stop_hook_command(context_path: Path) -> str:
    """Return the Stop hook command for a worktree whose context file is *context_path*.

    #2226. ``cw signal-stop`` is a no-op in any session cw did not spawn -- it
    reads the hook payload's ``cwd``, finds no ``.claude/cw-context.json`` and
    returns -- but it pays a full Python interpreter start plus ``from cw.cli
    import main`` to reach that conclusion: measured at ~250ms per invocation
    against ~1.5ms for the shell guard below. On a user-level install (the
    shape #2226 was filed for) that is ~250ms on every turn of every Claude
    session on the machine.

    The guard is one POSIX-sh existence test on the **absolute** path of the
    context file this same code writes, in front of the unchanged call:
    ``[ -f '<abs>/.claude/cw-context.json' ] || exit 0; cw signal-stop``.
    cw writes the hook and the file together, per worktree, so the path is
    known at injection time and is stable for the life of the worktree. That
    makes the guard identity-free and unable to skip a dispatch worker: it
    depends on no environment variable (ADR-0003 rejected env vars as the
    identity channel -- ``claude --bg`` does not propagate the caller's
    environment, #133) and on no ambient cwd (a worker's cwd legitimately
    moves during a turn, e.g. into a detached gate worktree). An earlier
    revision keyed the test on ``$CLAUDE_PROJECT_DIR`` with a hook-cwd
    fallback; both are ambient, and when neither pointed at the session
    worktree it skipped ``cw signal-stop`` and lost the completion signal.

    The path is ``shlex.quote``-d so a worktree path with spaces or shell
    metacharacters stays one word; ``json.dumps`` escapes the result when the
    settings file is rendered. A hand-written or user-level copy of the hook
    has no such path to bake in -- ``cw doctor``'s ``stop-hook-scope`` check
    is what surfaces that shape.
    """
    return f"[ -f {shlex.quote(str(context_path))} ] || exit 0; cw signal-stop"


def _build_hook_settings(context_path: Path) -> dict[str, dict[str, list[object]]]:
    """Return the ``settings.local.json`` content for a worktree.

    *context_path* is the absolute path of the worktree's ``cw-context.json``;
    it is baked into the Stop hook's guard (see :func:`_stop_hook_command`).
    Everything else is a per-worktree constant.
    """
    return {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _stop_hook_command(context_path),
                        }
                    ],
                }
            ],
            # PreToolUse guard (#940 R5): blocks a Bash tool call when the
            # worker's cwd resolves to the operator main checkout
            # (workspace_path in cw-context.json), preventing the #925/#766
            # isolation breach. Fail-open (exit 0) on any missing/malformed
            # context so it never blocks legit work.
            # The second command on the Bash matcher is `cw guard-busy-wait`
            # (#1946): it blocks a bare `true`/`:`/`sleep` no-op, and an
            # identical command repeated past the configured threshold inside a
            # rolling window. Appended to THIS entry's hooks list rather than
            # declared as a second "Bash"-matched entry — one matcher, two
            # commands, is the shape the template already uses for Stop and the
            # shape whose dispatch behavior is exercised by the existing suite.
            # Fail-open like its neighbour, and disable-able per lane or
            # globally via busy_wait_guard_enabled in orchestrator.yaml.
            # The third is `cw background-tool-guard-pre` (#2303): in a
            # headless worker it refuses `run_in_background: true`, which has
            # no completion-notification path for a headless DAEMON session
            # (#2250/#2280/#2275). The same command also backs the "Monitor"
            # entry below — one classifier branching on tool_name, so the two
            # refusals cannot drift. Disable-able per lane or globally via
            # background_tool_guard_enabled in orchestrator.yaml. The
            # harness's PreToolUse chain runs for a subagent's own tool calls
            # in the same worktree cwd (#2275 transcript evidence), so this
            # covers the impl-subagent path the wedges actually took.
            "PreToolUse": [
                {
                    "matcher": BASH_TOOL_NAME,
                    "hooks": [
                        {"type": "command", "command": "cw guard-cwd"},
                        {"type": "command", "command": "cw guard-busy-wait"},
                        {
                            "type": "command",
                            "command": _BACKGROUND_TOOL_GUARD_COMMAND,
                        },
                    ],
                },
                # #1646: stamp an unresolved-subagent-spawn marker before the
                # spawn starts. The matching PostToolUse decrement was removed
                # by #1947 -- see the re-verification note above
                # _AGENT_TOOL_MATCHER.
                # The stamp half still never blocks. #2211 folded a spawn-shape
                # policy into the same command (no new hook, no new interpreter
                # start, and already-provisioned worktrees pick it up on a cw
                # upgrade since they reference the command by name): in a
                # headless worker it refuses both an explicitly-forked subagent
                # and one naming no subagent_type at all, since neither enters
                # cw's roster (#2017) and a fork also inherits the parent's
                # implementation mandate. Default-on, disable-able per lane or
                # globally via subagent_spawn_guard_enabled in
                # orchestrator.yaml. Every other spawn shape still allows.
                {
                    "matcher": _AGENT_TOOL_MATCHER,
                    "hooks": [{"type": "command", "command": "cw agent-spawn-pre"}],
                },
                # #2303: refuse the Monitor tool in a headless worker — it
                # watches a background task for an interactive operator, and
                # a headless turn that ends waiting on it never resumes. See
                # the Bash entry above for the shared command and toggle.
                {
                    "matcher": MONITOR_TOOL_NAME,
                    "hooks": [
                        {"type": "command", "command": _BACKGROUND_TOOL_GUARD_COMMAND}
                    ],
                },
            ],
        }
    }


def _write_hook_context(
    worktree: Path,
    *,
    session_id: str,
    session_name: str,
    client: str,
    purpose: str,
    ticket_id: str | None,
    origin: SessionOrigin,
    headless: bool = False,
    task: TicketTask | None = None,
    wall_clock_budget_seconds: int | None = None,
    default_branch: str = "main",
    workspace_path: Path | None = None,
    lane: str | None = None,
    write_stop_hook: bool = True,
    daemon: NativeDaemonClient | None = None,
) -> None:
    """Write hook config + correlation context into the worktree pre-spawn.

    Two files land under ``<worktree>/.claude/`` when *write_stop_hook* is
    True (the default, preserving every existing caller's behavior
    byte-for-byte):

    - ``settings.local.json`` — configures a Stop hook that runs
      ``cw signal-stop`` after each agent turn, behind a POSIX-sh guard that
      tests the **absolute** path of the ``cw-context.json`` written below
      (#2226, see :func:`_stop_hook_command`). The guard is identity-free: the
      path it tests is written by this same function, so it cannot skip a
      dispatch worker whatever the hook's environment or cwd.
    - ``cw-context.json`` — correlation metadata the hook reads to emit a
      ``SESSION_COMPLETED`` event keyed back to the cw session + dev_queue
      task. Bypasses the env-var injection limitation on ``claude --bg``
      (see GitHub issue #133).

    Origin-aware ``settings.local.json`` strategy (Option A from issue #165
    Phase B):

    - ``SessionOrigin.DAEMON``: the worktree was freshly created by cw;
      any prior ``settings.local.json`` is from a defunct cw spawn, so we
      blind-overwrite with the current hook template.
    - ``SessionOrigin.USER``: the worktree may carry a user-owned
      ``settings.local.json``. If one already exists, raise
      :class:`HookContextConflictError` rather than clobbering. If none
      exists, write the hook template (same content as the DAEMON path).

    Phase C wires the typed error into a clean failure path so interactive
    ``claude --bg`` sessions surface the conflict instead of trampling the
    user's settings.

    Both files are written atomically (temp file + rename) so a concurrent
    reader (the Stop hook reads ``cw-context.json`` every turn) never
    observes an empty or partial file (issue #427 fix 1).

    For ``SessionOrigin.DAEMON``: before overwriting, check whether an
    existing ``cw-context.json`` references a session that is still live in
    cw state. If so, raise :class:`HookContextConflictError` rather than
    clobbering — the prior session has not finished and we must not steal
    its hook context (issue #427 fix 2).

    ``write_stop_hook=False`` (#2280) skips ``settings.local.json`` entirely
    — for a caller with no Claude session to signal-stop (``CodexExecutor.
    spawn()``, whose review runs as prompt-driven ``codex exec`` subprocesses,
    not a Claude turn loop), there is no Stop hook to install. The DAEMON
    conflict check and ``cw-context.json`` itself (including
    ``prior_attempts_summary``) are unaffected — both still run.

    *daemon* (#2077): when supplied, the DAEMON live-session conflict also
    consults :func:`cw.worktree.live_home_reason` -- the same liveness
    predicate the dispatch pre-claim occupancy screen and ``create_worktree``'s
    reuse refresh use. If it confirms a live session or daemon worker is homed
    on *worktree*, the raised error carries ``genuinely_live=True`` and a
    message telling the operator NOT to close that session (the conflict
    resolves itself). ``None`` (USER-origin callers, and any caller without a
    resolved daemon) keeps the pre-#2077 message and ``genuinely_live=False``.
    """
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    claude_dir = context_path.parent
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.local.json"

    if write_stop_hook and origin is SessionOrigin.USER and settings_path.exists():
        msg = (
            "Cannot inject Stop hook: "
            f"{settings_path} already exists in a USER-origin worktree. "
            "Refusing to overwrite user-managed settings."
        )
        raise HookContextConflictError(msg)

    if origin is SessionOrigin.DAEMON and context_path.exists():
        try:
            prior = json.loads(context_path.read_text(encoding="utf-8"))
            prior_session_id: str | None = prior.get("session_id")
        except (OSError, json.JSONDecodeError):
            prior_session_id = None

        if prior_session_id is not None:
            state = load_state()
            prior_sess = state.find_by_name_or_id(prior_session_id)
            if (
                prior_sess is not None
                and prior_sess.status not in TERMINAL_SESSION_STATUSES
            ):
                occupancy_reason = (
                    live_home_reason(worktree, daemon=daemon)
                    if daemon is not None
                    else None
                )
                genuinely_live = occupancy_reason in {
                    "a live session is homed on this worktree",
                    "a live daemon worker is homed on this worktree",
                }
                if genuinely_live:
                    msg = (
                        f"Worktree hook context at {context_path} is held by "
                        f"session {prior_session_id!r} (status: "
                        f"{prior_sess.status}), which is genuinely live and "
                        "actively working there. This is not an error to act "
                        "on -- it will resolve on its own once that session "
                        "finishes. Do not close it."
                    )
                else:
                    msg = (
                        f"Cannot overwrite hook context: {context_path} references "
                        f"live session {prior_session_id!r} "
                        f"(status: {prior_sess.status}). "
                        "Complete or close that session before reusing this "
                        "worktree."
                    )
                raise HookContextConflictError(
                    msg,
                    conflicting_session_id=prior_session_id,
                    genuinely_live=genuinely_live,
                )

    if write_stop_hook:
        atomic_write_text(
            settings_path,
            json.dumps(_build_hook_settings(context_path.resolve()), indent=2) + "\n",
        )
    context: dict[str, object] = {
        "schema_version": CW_CONTEXT_SCHEMA_VERSION,
        "session_id": session_id,
        "session_name": session_name,
        "client": client,
        # Why (#1946): the `cw guard-busy-wait` PreToolUse hook runs as a bare
        # subprocess with only this file for context — it has no cw session or
        # dev_queue row to read the lane from. Stamping it here is what makes
        # the guard's per-lane config override (LaneConfig.busy_wait_guard_*)
        # resolvable at all. Null for USER-origin sessions, which have no lane.
        "lane": lane,
        "purpose": purpose,
        "ticket_id": ticket_id,
        "headless": headless,
        # Why: the worker's isolation anchor. A headless /auto-dev run reads
        # this to confirm it operates on its own worktree and never falls back
        # to a git op against the operator's shared checkout (#402). Resolved
        # to canonicalize symlinks, matching check_not_main_checkout's compare.
        "worktree_path": str(worktree.resolve()),
        # Why (#766): the operator's main checkout — the FORBIDDEN path for any
        # git mutation from a dispatch worker. A PreToolUse hook or guard script
        # reads this to block git commit/push when the resolved repo root matches
        # this path, preventing the isolation breach proven in the #766 transcript.
        # Absent (null) for USER-origin sessions that lack a client workspace.
        "workspace_path": str(workspace_path.resolve())
        if workspace_path is not None
        else None,
        # Why (#1646): seeded resolved (count 0) so the PreToolUse/PostToolUse
        # stamp pair only ever has to read-modify-write, never create. A
        # session that crashes with this above zero died with a sub-agent spawn
        # still in flight -- committed work may exist behind a verification
        # tail that never ran -- which the phantom sweep parks under its own
        # disposition instead of the generic phantom_surface.
        AGENT_SPAWN_STAMP_KEY: {
            AGENT_SPAWN_UNRESOLVED_COUNT_KEY: 0,
            AGENT_SPAWN_LAST_STAMPED_AT_KEY: None,
        },
    }
    if task is not None:
        try:
            rev = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", f"origin/{default_branch}"],
                capture_output=True,
                text=True,
                check=True,
                env=_git_clean_env(),
            )
            origin_sha: str | None = rev.stdout.strip() or None
        except (subprocess.CalledProcessError, OSError):
            origin_sha = None

        context.update(
            {
                "attempt": task.attempts,
                "wall_clock_budget_seconds": wall_clock_budget_seconds,
                "stage_started_at": datetime.now(UTC).isoformat(),
                "expected_sentinel_schema_ref": {
                    "command": "cw schema show auto-dev-result --format=tldr",
                    "model": "AutoDevResult",
                    "version": AUTO_DEV_RESULT_CURRENT_SCHEMA_VERSION,
                },
                "queue_metadata": {
                    "scope_hint": task.scope_hint,
                    "plan_source": task.plan_source,
                    "headless_timeout_override": task.headless_timeout_override,
                    # #1794: Stage is a StrEnum, so json.dumps renders this as
                    # the plain stage value (or null) with no .value call.
                    "regressed_into_stage": task.regressed_into_stage,
                    # #1730: sibling per-arrival marker -- true means this
                    # re-entry may carry an operator send-back the review stage
                    # must treat as a binding adjudication input.
                    "pending_operator_comment": task.pending_operator_comment,
                    # v7: tracker-neutral plan-approval evidence for the
                    # plan stage's Checkpoint 1 (Large-scope carve-out).
                    "plan_approved_at": (
                        task.plan_approved_at.isoformat()
                        if task.plan_approved_at is not None
                        else None
                    ),
                    # v8 (#2102): the draft the approval above was bound to.
                    # Checkpoint 1 requires it to equal the resumed draft's own
                    # fingerprint before the approval counts as evidence.
                    PLAN_APPROVED_FINGERPRINT_KEY: task.plan_approved_fingerprint,
                    # v9 (#2337): the operator's plan_scope_drift grant and the
                    # branch head it is bound to, read by Step 2.5 gate 2.
                    SCOPE_DRIFT_APPROVED_EXTRA_FILES_KEY: (
                        task.scope_drift_approved_extra_files
                    ),
                    SCOPE_DRIFT_APPROVED_HEAD_KEY: task.scope_drift_approved_head,
                },
                "world_state_snapshot": {
                    "origin_main_sha_at_spawn": origin_sha,
                    "origin_main_branch": default_branch,
                    "prior_attempts_summary": (
                        _collect_prior_attempts_summary(ticket_id, client=client)
                        if ticket_id is not None and task.attempts > 0
                        else []
                    ),
                },
            }
        )
    atomic_write_text(context_path, json.dumps(context, indent=2) + "\n")


def spawn_create_impl(
    *,
    client: ClientConfig,
    worktree: Path,
    prompt: str,
    label: str | None,
    native_daemon: NativeDaemonClient | None = None,
    parent: str | None = None,
    ticket_id: str | None = None,
    headless: bool = False,
    extra_args: list[str] | None = None,
    permission_mode: str | None = None,
    task: TicketTask | None = None,
    wall_clock_budget_seconds: int | None = None,
    lane: str | None = None,
    purpose: SessionPurpose = SessionPurpose.IMPL,
    _roster_poll_timeout: float = _ROSTER_POLL_TIMEOUT_SECS,
    _roster_poll_interval: float = _ROSTER_POLL_INTERVAL_SECS,
) -> str:
    """Create a daemon-spawned session via the native Claude background daemon.

    Replaces the prior tmux/cmux-based path (see GitHub issue #150). The
    worktree must already exist; cwd is passed to ``claude --bg`` so the
    spawned agent inherits the right project context, picks up the
    injected ``.claude/settings.local.json`` Stop hook, and reads the
    correlation file at ``.claude/cw-context.json`` when signaling
    completion.

    Returns the new cw session id. The Claude short session id (8 hex
    chars) is stored on the Session as ``surface_ref`` so reconcile can
    check liveness against the daemon's roster.

    When *parent* is supplied, it is resolved via
    :func:`cw.session_retention.find_session_by_id` — matching by cw ``id``,
    ``claude_session_id``, or an archived session, hot-then-archived
    (#2149) — and writes bidirectional linkage in the same state save:
    ``sess.parent_session_id = parent.id`` and appends ``sess.id`` to
    ``parent.worker_session_ids``. The reverse link is only written when the
    resolved parent is still in the hot ``sessions.json`` (an archived
    parent's ``worker_session_ids`` mutation would never be persisted, so it
    is skipped rather than attempted). Raises :class:`CwError` if *parent*
    cannot be resolved at all.

    After spawning, polls the daemon roster to verify the worker was
    actually adopted. Raises :class:`~cw.exceptions.SpawnUnregisteredError`
    if the short id never appears within *_roster_poll_timeout* seconds.
    The underscore-prefixed poll parameters are injectable for testing only.
    """
    _validate_worktree(worktree)

    # Validate parent exists before spawning (fail fast, no daemon call yet).
    if parent is not None:
        _pre_state = load_state()
        if find_session_by_id(parent, state=_pre_state) is None:
            msg = f"Parent session not found: {parent}"
            raise CwError(msg)

    session_label = label or "daemon"
    sess = Session(
        name=f"{client.name}/{session_label}",
        client=client.name,
        purpose=purpose,
        origin=SessionOrigin.DAEMON,
        workspace_path=client.workspace_path,
        worktree_path=worktree,
        lane=lane,
    )

    # Inject Stop-hook config + correlation context into the worktree so
    # the spawned session emits a SESSION_COMPLETED event when its agent
    # turn finishes — works under ``claude --bg`` where env vars are not
    # propagated. See GitHub issue #147. The daemon is resolved first so the
    # hook-context conflict check can corroborate liveness with it (#2077).
    daemon = native_daemon or get_native_daemon_client()
    _write_hook_context(
        worktree,
        session_id=sess.id,
        session_name=sess.name,
        client=client.name,
        purpose=purpose.value,
        ticket_id=ticket_id,
        origin=SessionOrigin.DAEMON,
        headless=headless,
        task=task,
        wall_clock_budget_seconds=wall_clock_budget_seconds,
        default_branch=client.default_branch,
        workspace_path=client.workspace_path,
        lane=lane,
        daemon=daemon,
    )

    final_extra: list[str] = []
    if client.worker_model:
        final_extra.extend(["--model", client.worker_model])
    final_extra.extend(
        build_disallowed_tools_arg(load_orchestrator_config().disallowed_mcp_tools)
    )
    if extra_args:
        final_extra.extend(extra_args)

    effective_permission_mode = resolve_permission_mode(
        client.worker_model, explicit=permission_mode
    )

    sess.surface_ref = daemon.spawn_bg(
        cwd=worktree,
        prompt=prompt,
        extra_args=final_extra or None,
        permission_mode=effective_permission_mode,
    )
    _verify_roster_registration(
        daemon,
        sess.surface_ref,
        ticket_id,
        timeout=_roster_poll_timeout,
        interval=_roster_poll_interval,
    )

    csid = _csid_from_transcript(sess)
    if csid is not None:
        sess.claude_session_id = csid

    with sessions_lock():
        state = load_state()
        if parent is not None:
            parent_session = find_session_by_id(parent, state=state)
            if parent_session is None:
                msg = f"Parent session not found: {parent}"
                raise CwError(msg)
            sess.parent_session_id = parent_session.id
            # An archive-resolved parent is not in state.sessions, so
            # mutating its worker_session_ids here would never be persisted
            # by this save_state() call — skip the reverse link rather than
            # silently no-op it (#2149).
            if any(s.id == parent_session.id for s in state.sessions):
                parent_session.worker_session_ids.append(sess.id)
        state.sessions.append(sess)
        save_state(state)
    return sess.id
