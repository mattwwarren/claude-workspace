"""Session spawn helpers shared between CLI and dispatch loop."""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.exceptions import CwError, HookContextConflictError, WorktreeError
from cw.models import Session, SessionOrigin, SessionPurpose
from cw.native_daemon import get_native_daemon_client

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient


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
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
        env=clean_env,
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
        ]
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
    """
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.local.json"
    if origin is SessionOrigin.USER and settings_path.exists():
        msg = (
            "Cannot inject Stop hook: "
            f"{settings_path} already exists in a USER-origin worktree. "
            "Refusing to overwrite user-managed settings."
        )
        raise HookContextConflictError(msg)
    settings_path.write_text(
        json.dumps(_HOOK_SETTINGS_TEMPLATE, indent=2) + "\n",
    )
    context = {
        "session_id": session_id,
        "session_name": session_name,
        "client": client,
        "purpose": purpose,
        "ticket_id": ticket_id,
        "headless": headless,
    }
    (claude_dir / "cw-context.json").write_text(json.dumps(context, indent=2) + "\n")


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
    """
    _validate_worktree(worktree)
    state = load_state()

    parent_session: Session | None = None
    if parent is not None:
        parent_session = state.find_by_name_or_id(parent)
        if parent_session is None:
            msg = f"Parent session not found: {parent}"
            raise CwError(msg)

    session_label = label or "daemon"
    sess = Session(
        name=f"{client.name}/{session_label}",
        client=client.name,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        workspace_path=client.workspace_path,
        worktree_path=worktree,
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
        purpose=SessionPurpose.IMPL.value,
        ticket_id=ticket_id,
        origin=SessionOrigin.DAEMON,
        headless=headless,
    )

    extra_args: list[str] | None = None
    if client.worker_model:
        extra_args = ["--model", client.worker_model]

    daemon = native_daemon or get_native_daemon_client()
    sess.surface_ref = daemon.spawn_bg(
        cwd=worktree,
        prompt=prompt,
        extra_args=extra_args,
    )

    if parent_session is not None:
        sess.parent_session_id = parent_session.id
        parent_session.worker_session_ids.append(sess.id)

    state.sessions.append(sess)
    save_state(state)
    return sess.id
