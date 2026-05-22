"""Session spawn helpers shared between CLI and dispatch loop."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import Session, SessionOrigin, SessionPurpose

if TYPE_CHECKING:
    from pathlib import Path

    from cw.cmux import CmuxAdapter
    from cw.models import ClientConfig


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
    surface: str,
    label: str | None,
    adapter: CmuxAdapter,
    parent: str | None = None,
    ticket_id: str | None = None,
) -> str:
    """Create a daemon-spawned session.

    Separated from the Click command so tests and the dispatch loop can
    inject adapters directly. Returns the new session's ID.

    Callers pass the prompt as a string. The CLI ``cw spawn`` reads it
    from a file at the user-facing boundary; everything else inlines
    directly. ``claude -w`` is NOT used — that flag takes a worktree
    *name* and creates a nested worktree at a path cw cannot track. The
    worktree is established by the caller (``create_worktree`` / the
    interactive start path) and we just ``cd`` the spawned shell into it.

    When *parent* is supplied, write bidirectional linkage in the same
    state save: ``sess.parent_session_id = parent.id`` and append
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

    workspace = client.cmux_workspace or client.name
    cwd = shlex.quote(str(worktree))

    # Pre-create the Session so we can pass its ID into the spawned command
    # via CW_SESSION_ID. The wrapper uses that to disambiguate when multiple
    # daemon sessions share the same (client, purpose=impl) pair.
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
    # turn finishes — works even under ``claude --bg`` where env vars are
    # not propagated. See GitHub issue #147.
    _write_hook_context(
        worktree,
        session_id=sess.id,
        session_name=sess.name,
        client=client.name,
        purpose=SessionPurpose.IMPL.value,
        ticket_id=ticket_id,
    )

    env_prefix = (
        f"CW_CLIENT={shlex.quote(client.name)} "
        f"CW_PURPOSE={shlex.quote(SessionPurpose.IMPL.value)} "
        f"CW_SESSION_ID={shlex.quote(sess.id)} "
    )
    command = f"cd {cwd} && {env_prefix}cw run-claude -- --print {prompt!r}"
    surface_ref = adapter.spawn(workspace, command, surface)
    sess.surface_ref = surface_ref

    if parent_session is not None:
        sess.parent_session_id = parent_session.id
        parent_session.worker_session_ids.append(sess.id)

    state.sessions.append(sess)
    save_state(state)
    return sess.id
