"""Session spawn helpers shared between CLI and dispatch loop."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.models import Session, SessionOrigin, SessionPurpose

if TYPE_CHECKING:
    from pathlib import Path

    from cw.cmux import CmuxAdapter
    from cw.models import ClientConfig


def spawn_create_impl(
    *,
    client: ClientConfig,
    worktree: Path,
    prompt_file: Path,
    surface: str,
    label: str | None,
    adapter: CmuxAdapter,
) -> str:
    """Create a daemon-spawned session.

    Separated from the Click command so tests and the dispatch loop can
    inject adapters directly.  Returns the new session's ID.

    The worktree is established before dispatch (see ``cw.worktree``), so
    the spawned shell just ``cd``s into it. ``claude -w`` is NOT used here
    — that flag takes a worktree *name* and would create a nested worktree
    from whatever directory the pane happened to start in.
    """
    prompt_content = prompt_file.read_text()
    workspace = client.cmux_workspace or client.name
    cwd = shlex.quote(str(worktree))
    command = f"cd {cwd} && claude --print {prompt_content!r}"
    surface_ref = adapter.spawn(workspace, command, surface)

    session_label = label or "daemon"
    sess = Session(
        name=f"{client.name}/{session_label}",
        client=client.name,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        workspace_path=client.workspace_path,
        worktree_path=worktree,
        surface_ref=surface_ref,
    )

    state = load_state()
    state.sessions.append(sess)
    save_state(state)
    return sess.id
