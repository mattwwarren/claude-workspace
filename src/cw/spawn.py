"""Session spawn helpers shared between CLI and dispatch loop."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
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
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import get_native_daemon_client, resolve_permission_mode
from cw.reconcile import _csid_from_transcript, ticket_id_for_session

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient

_log = logging.getLogger(__name__)

# Schema version for cw-context.json. Increment when the shape changes so
# workers can detect whether they are reading a context written by an older cw.
# v2: added `workspace_path` (#766 — forbidden main-checkout path for git guard).
CW_CONTEXT_SCHEMA_VERSION = 2


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


def _collect_prior_attempts_summary(ticket_id: str) -> list[dict[str, object]]:
    """Return compact failure summaries for prior sessions on *ticket_id*.

    Called only when task.attempts > 0. Scans persisted state for TIMED_OUT or
    COMPLETED sessions whose name encodes *ticket_id*, builds one entry per
    session from last_result, sorts by completed_at ascending, and returns the
    list. Returns [] on any exception so a state-read failure never blocks spawn.
    """
    try:
        state = load_state()
        terminal = (SessionStatus.TIMED_OUT, SessionStatus.COMPLETED)
        matching = [
            s
            for s in state.sessions
            if s.status in terminal and ticket_id_for_session(s.name) == ticket_id
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
            "prior_attempts_summary: failed to collect for ticket=%r; "
            "falling back to []",
            ticket_id,
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
    deadline = time.monotonic() + timeout
    while True:
        if short_id in daemon.list_live_session_short_ids():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
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


_HOOK_SETTINGS_TEMPLATE = {
    "hooks": {
        "Stop": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": "cw signal-stop"}],
            }
        ],
        # PreToolUse guard (#940 R5): blocks a Bash tool call when the worker's
        # cwd resolves to the operator main checkout (workspace_path in
        # cw-context.json), preventing the #925/#766 isolation breach. Fail-open
        # (exit 0) on any missing/malformed context so it never blocks legit work.
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "cw guard-cwd"}],
            }
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
) -> None:
    """Write hook config + correlation context into the worktree pre-spawn.

    Two files land under ``<worktree>/.claude/``:

    - ``settings.local.json`` — configures a Stop hook that invokes
      ``cw signal-stop`` after each agent turn.
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
    """
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.local.json"
    context_path = claude_dir / "cw-context.json"

    if origin is SessionOrigin.USER and settings_path.exists():
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
            if prior_sess is not None and prior_sess.status not in (
                SessionStatus.COMPLETED,
                SessionStatus.TIMED_OUT,
            ):
                msg = (
                    f"Cannot overwrite hook context: {context_path} references "
                    f"live session {prior_session_id!r} "
                    f"(status: {prior_sess.status}). "
                    "Complete or close that session before reusing this worktree."
                )
                raise HookContextConflictError(
                    msg, conflicting_session_id=prior_session_id
                )

    atomic_write_text(
        settings_path,
        json.dumps(_HOOK_SETTINGS_TEMPLATE, indent=2) + "\n",
    )
    context: dict[str, object] = {
        "schema_version": CW_CONTEXT_SCHEMA_VERSION,
        "session_id": session_id,
        "session_name": session_name,
        "client": client,
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
                },
                "world_state_snapshot": {
                    "origin_main_sha_at_spawn": origin_sha,
                    "origin_main_branch": default_branch,
                    "prior_attempts_summary": (
                        _collect_prior_attempts_summary(ticket_id)
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

    When *parent* is supplied, writes bidirectional linkage in the same
    state save: ``sess.parent_session_id = parent.id`` and appends
    ``sess.id`` to ``parent.worker_session_ids``. Raises :class:`CwError`
    if the parent session is not in state.

    After spawning, polls the daemon roster to verify the worker was
    actually adopted. Raises :class:`~cw.exceptions.SpawnUnregisteredError`
    if the short id never appears within *_roster_poll_timeout* seconds.
    The underscore-prefixed poll parameters are injectable for testing only.
    """
    _validate_worktree(worktree)

    # Validate parent exists before spawning (fail fast, no daemon call yet).
    if parent is not None:
        _pre_state = load_state()
        if _pre_state.find_by_name_or_id(parent) is None:
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
    # propagated. See GitHub issue #147.
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

    daemon = native_daemon or get_native_daemon_client()
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
            parent_session = state.find_by_name_or_id(parent)
            if parent_session is None:
                msg = f"Parent session not found: {parent}"
                raise CwError(msg)
            sess.parent_session_id = parent_session.id
            parent_session.worker_session_ids.append(sess.id)
        state.sessions.append(sess)
        save_state(state)
    return sess.id
