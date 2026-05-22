"""Session spawn helpers shared between CLI and dispatch loop."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import Session, SessionOrigin, SessionPurpose
from cw.native_daemon import get_native_daemon_client

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig
    from cw.native_daemon import NativeDaemonClient


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
) -> None:
    """Write hook config + correlation context into the worktree pre-spawn.

    Two files land under ``<worktree>/.claude/``:

    - ``settings.local.json`` — configures a Stop hook that invokes
      ``cw signal-stop`` after each agent turn.
    - ``cw-context.json`` — correlation metadata the hook reads to emit a
      ``SESSION_COMPLETED`` event keyed back to the cw session + dev_queue
      task. Bypasses the env-var injection limitation on ``claude --bg``
      (see GitHub issue #133).
    """
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.local.json").write_text(
        json.dumps(_HOOK_SETTINGS_TEMPLATE, indent=2) + "\n",
    )
    context = {
        "session_id": session_id,
        "session_name": session_name,
        "client": client,
        "purpose": purpose,
        "ticket_id": ticket_id,
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
    )

    daemon = native_daemon or get_native_daemon_client()
    sess.surface_ref = daemon.spawn_bg(cwd=worktree, prompt=prompt)

    if parent_session is not None:
        sess.parent_session_id = parent_session.id
        parent_session.worker_session_ids.append(sess.id)

    state.sessions.append(sess)
    save_state(state)
    return sess.id
