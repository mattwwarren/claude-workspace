"""Session lifecycle management: start, background, resume, list."""

from __future__ import annotations

import shlex
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import click

if TYPE_CHECKING:
    from pathlib import Path

from cw.cmux import CmuxAdapter, _resolve_backend_name, get_cmux_adapter
from cw.config import get_client, load_state, save_state
from cw.exceptions import CwError
from cw.handoff import extract_resumption_prompt, find_latest_handoff
from cw.history import EventType, HistoryEvent, record_event
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionStatus,
)
from cw.prompts import build_session_context, get_purpose_prompt
from cw.reconcile import reconcile
from cw.worktree import create_worktree, remove_worktree

# Purposes that receive worktree cwd (impl works on the feature branch,
# idea brainstorms within it; debt stays on the main workspace).
WORKTREE_PURPOSES: frozenset[str] = frozenset({"impl", "idea"})


def _build_env_prefix(client_name: str, purpose: str) -> str:
    """Build ``CW_CLIENT=… CW_PURPOSE=…`` shell prefix for claude commands."""
    return f"CW_CLIENT={client_name} CW_PURPOSE={purpose}"


CLAUDE_INIT_DELAY_S = 2


def _build_pane_args(
    sessions: dict[str, Session],
    client: ClientConfig | None = None,
) -> dict[str, dict[str, str]]:
    """Build pane data for each session including a claude command.

    Args:
        sessions: Map of purpose name to Session.
        client: Client config for resolving purpose prompts.
    """
    panes: dict[str, dict[str, str]] = {}
    client_overrides = client.purpose_prompts if client else None
    client_name = client.name if client else None
    workspace_path = str(client.workspace_path) if client else None
    for purpose, session in sessions.items():
        # Build extra flags (e.g. --append-system-prompt)
        extra = ""
        prompt = get_purpose_prompt(
            purpose,
            client_overrides,
            client_name=client_name,
            workspace_path=workspace_path,
        )
        if prompt:
            # Collapse newlines for single-line shell command
            escaped_prompt = shlex.quote(prompt.replace("\n", " "))
            extra = f" --append-system-prompt {escaped_prompt}"

        # Two-mode launch: recovery uses --resume <uuid>, fresh uses --session-id <uuid>
        if session.claude_session_id:
            session_flag = f" --resume {session.claude_session_id}"
        else:
            new_id = str(uuid4())
            session.claude_session_id = new_id
            session_flag = f" --session-id {new_id}"

        if client_name:
            env_prefix = f"{_build_env_prefix(client_name, purpose)} "
        else:
            env_prefix = ""
        cmd = f"{env_prefix}cw run-claude --{session_flag}{extra}"
        pane_data: dict[str, str] = {"claude_cmd": cmd}
        cwd = str(session.worktree_path or session.workspace_path)
        pane_data["cwd"] = cwd
        panes[purpose] = pane_data
    return panes


def _create_all_purpose_sessions(
    client_name: str,
    client: ClientConfig,
    state: CwState,
    *,
    worktree_path: Path | None = None,
    worktree_branch: str | None = None,
    prior_sessions: dict[str, Session] | None = None,
) -> dict[str, Session]:
    """Create Session objects for all purposes.

    worktree_path/branch apply to impl and idea purposes.
    When *prior_sessions* is provided, carries forward ``claude_session_id``
    from the matching purpose so recovery uses ``--resume <uuid>``.
    """
    sessions: dict[str, Session] = {}
    for purpose_enum in client.auto_purposes:
        purpose = purpose_enum.value
        # Carry forward claude_session_id from prior session for recovery
        prior_claude_id: str | None = None
        if prior_sessions and purpose in prior_sessions:
            prior_claude_id = prior_sessions[purpose].claude_session_id
        session = Session(
            name=f"{client_name}/{purpose}",
            client=client_name,
            purpose=purpose_enum,
            workspace_path=client.workspace_path,
            surface_ref=purpose,
            claude_session_id=prior_claude_id,
        )
        # Apply worktree to impl and idea panes
        if worktree_path and purpose in WORKTREE_PURPOSES:
            session.worktree_path = worktree_path
            session.branch = worktree_branch
        sessions[purpose] = session
        state.sessions.append(session)
        record_event(
            client_name,
            HistoryEvent(
                event_type=EventType.SESSION_STARTED,
                client=client_name,
                session_id=session.id,
                session_name=session.name,
                purpose=purpose,
            ),
        )
    return sessions


def _spawn_session_surface(
    client: ClientConfig,
    session: Session,
    command: str,
    adapter: CmuxAdapter,
) -> None:
    """Spawn a cmux surface for the session and store the surface_ref."""
    workspace = client.cmux_workspace or client.name
    surface_ref = adapter.spawn(workspace, command)
    session.surface_ref = surface_ref


def start_session(
    client_name: str,
    purpose: str,
    *,
    worktree: str | None = None,
    parent: str | None = None,
    adapter: CmuxAdapter | None = None,
) -> None:
    """Start or resume a Claude Code session for a client.

    Args:
        client_name: Name of the client to start.
        purpose: Session purpose (impl, idea, debt, explore).
        worktree: Optional git branch for worktree isolation.
        parent: Optional parent session ID for bidirectional linkage.
        adapter: CmuxAdapter instance. Defaults to get_cmux_adapter() (macOS only).
                 Inject FakeCmuxAdapter for tests.
    """
    if adapter is None:
        adapter = get_cmux_adapter()

    client = get_client(client_name)
    state = load_state()

    # Reap phantom sessions so we don't short-circuit on a dead "active" row.
    reconcile(adapter)
    state = load_state()

    # Auto-resolve worktree for worktree-mode clients
    worktree_path: Path | None = None
    worktree_branch: str | None = worktree
    if client.is_worktree_client:
        branch = client.branch
        if branch is None:
            msg = "Worktree client must have branch set"
            raise CwError(msg)
        click.echo(f"Creating worktree for branch '{branch}'...")
        worktree_path = create_worktree(client, branch)
        worktree_branch = branch
        # Patch workspace_path to the real worktree path
        client = client.model_copy(update={"workspace_path": worktree_path})
        click.echo(f"Worktree ready: {worktree_path}")
    elif worktree:
        click.echo(f"Creating worktree for branch '{worktree}'...")
        worktree_path = create_worktree(client, worktree)
        click.echo(f"Worktree ready: {worktree_path}")

    # Check for existing backgrounded session
    existing = state.find_session(client_name, purpose)

    if existing and existing.status == SessionStatus.BACKGROUNDED:
        click.echo(f"Found backgrounded session: {existing.name}")
        resume_session(existing.name, adapter=adapter)
        return

    if existing and existing.status == SessionStatus.ACTIVE:
        click.echo(f"Session already active: {existing.name}")
        return

    # Validate parent session before creating any new sessions
    parent_session: Session | None = None
    if parent is not None:
        parent_session = state.find_by_name_or_id(parent)
        if parent_session is None:
            msg = f"Parent session not found: {parent}"
            raise CwError(msg)

    # Create sessions for ALL purposes
    all_sessions = _create_all_purpose_sessions(
        client_name,
        client,
        state,
        worktree_path=worktree_path,
        worktree_branch=worktree_branch,
    )

    # Bidirectional linkage: set parent_session_id on each new session,
    # append each new session's ID to the parent's worker_session_ids.
    # Both mutations happen before the single save_state call so the
    # persisted state is always consistent.
    if parent_session is not None:
        for new_session in all_sessions.values():
            new_session.parent_session_id = parent_session.id
            parent_session.worker_session_ids.append(new_session.id)

    save_state(state)
    panes = _build_pane_args(all_sessions, client=client)

    for s in all_sessions.values():
        click.echo(f"  {s.name}")

    # Spawn surfaces for all purposes
    backend = _resolve_backend_name()
    click.echo(f"Launching {backend.value} surfaces for {client_name}...")
    for purpose_str, session in all_sessions.items():
        pane_cmd = panes[purpose_str]["claude_cmd"]
        _spawn_session_surface(client, session, pane_cmd, adapter)

    save_state(state)


def _resolve_session(state: CwState, session_name: str | None) -> Session:
    """Resolve which session to operate on.

    Looks up by name/id if given, otherwise auto-detects from active sessions.
    Raises CwError if the session can't be found or is ambiguous.
    """
    if session_name:
        session = state.find_by_name_or_id(session_name)
        if session is None:
            msg = f"Session not found: {session_name}"
            raise CwError(msg)
        return session

    active = state.active_sessions()
    if len(active) == 1:
        return active[0]
    if not active:
        msg = "No active sessions to background."
        raise CwError(msg)
    names = ", ".join(s.name for s in active)
    msg = f"Multiple active sessions. Specify which one: {names}"
    raise CwError(msg)


def _notify_sibling(
    client_name: str,
    source_purpose: str,
    target_purpose: str,
    adapter: CmuxAdapter,
) -> None:
    """Send a short notification to a sibling session after backgrounding."""
    state = load_state()
    target = state.find_session(client_name, target_purpose)
    if target is None or target.status != SessionStatus.ACTIVE:
        click.echo(
            f"Warning: No active {target_purpose} session for {client_name} to notify."
        )
        return

    message = (
        f"\n[cw] {source_purpose} session has been backgrounded."
        f" Handoff context is available."
    )
    if target.surface_ref:
        try:
            # Send via cmux surface if we have a reference
            client = get_client(client_name)
            workspace = client.cmux_workspace or client_name
            adapter.spawn(workspace, message)
        except CwError:
            pass
    click.echo(f"Notified {target.name}.")


def background_session(
    session_name: str | None = None,
    *,
    notify: str | None = None,
    auto: bool = False,
    adapter: CmuxAdapter | None = None,
) -> None:
    """Background a session by triggering /session-done and recording the handoff."""
    state = load_state()
    session = _resolve_session(state, session_name)

    if session.status not in (SessionStatus.ACTIVE, SessionStatus.IDLE):
        msg = f"Session {session.name} is not active or idle (status: {session.status})"
        raise CwError(msg)

    click.echo(f"Backgrounding session: {session.name}...")

    if session.status == SessionStatus.IDLE:
        # Claude already exited — no /session-done needed.
        latest = find_latest_handoff(session.workspace_path)
        if latest:
            session.last_handoff_path = latest
    else:
        latest = find_latest_handoff(session.workspace_path)
        if latest:
            session.last_handoff_path = latest
        click.echo(
            "Not inside cmux session."
            " Marking as backgrounded without /session-done injection."
        )

    session.status = SessionStatus.BACKGROUNDED
    session.backgrounded_at = datetime.now(UTC)
    if auto:
        session.auto_backgrounded = True
    save_state(state)
    record_event(
        session.client,
        HistoryEvent(
            event_type=EventType.SESSION_BACKGROUNDED,
            client=session.client,
            session_id=session.id,
            session_name=session.name,
            purpose=session.purpose,
        ),
    )
    click.echo(f"Session {session.name} backgrounded.")

    if notify:
        if adapter is None:
            adapter = get_cmux_adapter()
        _notify_sibling(session.client, session.purpose, notify, adapter)


def background_all_sessions(
    *,
    notify: str | None = None,
    auto: bool = False,
) -> None:
    """Background all active sessions sequentially."""
    state = load_state()
    active = state.active_sessions()
    if not active:
        click.echo("No active sessions to background.")
        return

    click.echo(f"Backgrounding {len(active)} active session(s)...")
    for session in active:
        try:
            background_session(session.name, notify=notify, auto=auto)
        except CwError as exc:
            click.echo(f"Warning: could not background {session.name}: {exc}")


def resume_session(
    session_name: str,
    *,
    adapter: CmuxAdapter | None = None,
) -> None:
    """Resume a backgrounded session with its handoff context.

    Args:
        session_name: Session name or ID to resume.
        adapter: CmuxAdapter instance. Defaults to get_cmux_adapter() (macOS only).
                 Inject FakeCmuxAdapter for tests.
    """
    if adapter is None:
        adapter = get_cmux_adapter()

    state = load_state()

    session = state.find_by_name_or_id(session_name)
    if session is None:
        msg = f"Session not found: {session_name}"
        raise CwError(msg)

    if session.status not in (SessionStatus.BACKGROUNDED, SessionStatus.IDLE):
        msg = (
            f"Session {session.name} is not backgrounded or idle"
            f" (status: {session.status})"
        )
        raise CwError(msg)

    # Extract resumption prompt from handoff
    prompt = None
    handoff_path = session.last_handoff_path
    if handoff_path and handoff_path.exists():
        prompt = extract_resumption_prompt(handoff_path)
        if prompt:
            click.echo(f"Loaded resumption context from: {handoff_path}")
        else:
            click.echo("Warning: Could not extract resumption prompt from handoff.")
    else:
        click.echo("No handoff file available. Starting fresh session.")

    # Get client config
    client = get_client(session.client)

    # Prepend client identity so resumed sessions know who they are
    context = build_session_context(
        session.client,
        str(client.workspace_path),
        session.purpose,
    )
    full_prompt = f"{context}\n\n{prompt}" if prompt else context

    # Spawn a new cmux surface for the resumed session
    env_prefix = _build_env_prefix(session.client, session.purpose)
    resume_cmd = f"{env_prefix} claude --resume"
    _spawn_session_surface(client, session, resume_cmd, adapter)

    session.status = SessionStatus.ACTIVE
    session.resumed_at = datetime.now(UTC)
    save_state(state)
    record_event(
        session.client,
        HistoryEvent(
            event_type=EventType.SESSION_RESUMED,
            client=session.client,
            session_id=session.id,
            session_name=session.name,
            purpose=session.purpose,
        ),
    )

    click.echo(f"Resumed session: {session.name}")

    # Output prompt for injection (cmux surface has been spawned with the command)
    if full_prompt:
        time.sleep(CLAUDE_INIT_DELAY_S)  # Wait for Claude to initialize
        click.echo("\nResumption prompt:")
        click.echo(full_prompt)


def done_session(
    session_name: str | None = None,
    *,
    cleanup: bool = False,
    force: bool = False,
) -> None:
    """Mark a session as completed and optionally remove its worktree."""
    state = load_state()
    session = _resolve_session(state, session_name)

    if session.status == SessionStatus.COMPLETED:
        msg = f"Session {session.name} is already completed."
        raise CwError(msg)

    session.status = SessionStatus.COMPLETED
    session.completed_reason = CompletionReason.USER
    session.completed_at = datetime.now(UTC)
    save_state(state)
    record_event(
        session.client,
        HistoryEvent(
            event_type=EventType.SESSION_COMPLETED,
            client=session.client,
            session_id=session.id,
            session_name=session.name,
            purpose=session.purpose,
        ),
    )
    click.echo(f"Session {session.name} marked as completed.")

    if cleanup and session.worktree_path and session.branch:
        client = get_client(session.client)
        click.echo(f"Removing worktree for branch '{session.branch}'...")
        remove_worktree(client, session.branch, force=force)
        click.echo("Worktree removed.")
