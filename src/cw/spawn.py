"""Session spawn helpers shared between CLI and dispatch loop."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from cw.config import load_state, save_state
from cw.exceptions import CwError
from cw.models import Session, SessionOrigin, SessionPurpose

if TYPE_CHECKING:
    from pathlib import Path

    from cw.cmux import CmuxAdapter
    from cw.models import ClientConfig


def spawn_create_impl(
    *,
    client: ClientConfig,
    worktree: Path,
    prompt: str,
    surface: str,
    label: str | None,
    adapter: CmuxAdapter,
    parent: str | None = None,
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
