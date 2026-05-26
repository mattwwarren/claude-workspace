"""Session lifecycle management: start, background, resume, list."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from pathlib import Path

from cw.config import get_client, load_state, save_state
from cw.exceptions import CwError
from cw.handoff import extract_resumption_prompt, find_latest_handoff
from cw.history import EventType, HistoryEvent, record_event
from cw.models import (
    CompletionReason,
    CwState,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import NativeDaemonClient, get_native_daemon_client
from cw.prompts import build_session_context, get_purpose_prompt
from cw.reconcile import reconcile
from cw.spawn import _write_hook_context
from cw.worktree import create_worktree, remove_worktree

# Purposes that receive worktree cwd (impl works on the feature branch,
# idea brainstorms within it; debt stays on the main workspace).
WORKTREE_PURPOSES: frozenset[str] = frozenset({"impl", "idea"})

# Hex characters used in Claude daemon short session ids.
_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdef")
# Length of the Claude daemon short session id (8 hex chars).
_SHORT_ID_LEN: int = 8

_DETACH_HINT = (
    "Detach with Ctrl+Z. Do NOT use Ctrl+D"
    " (terminates the daemon session in all attached terminals)."
)


def _is_native_surface_ref(ref: str) -> bool:
    """Return True if *ref* looks like an 8-char hex daemon short id."""
    return len(ref) == _SHORT_ID_LEN and all(c in _HEX_CHARS for c in ref)


def _attach_session(short_id: str) -> None:
    """Attach the current terminal to ``claude attach <short_id>``.

    Blocks until the user exits (Ctrl+Z detaches without terminating the
    session; Ctrl+D terminates the daemon session in all attached terminals).
    """
    subprocess.run(["claude", "attach", short_id], check=False)


def start_session(
    client_name: str,
    purpose: str,
    *,
    worktree: str | None = None,
    parent: str | None = None,
    native_daemon: NativeDaemonClient | None = None,
) -> None:
    """Start or resume a Claude Code session for a client.

    Args:
        client_name: Name of the client to start.
        purpose: Session purpose (impl, idea, debt, explore).
        worktree: Optional git branch for worktree isolation.
        parent: Optional parent session ID for bidirectional linkage.
        native_daemon: NativeDaemonClient instance. Defaults to
                       get_native_daemon_client(). Inject FakeNativeDaemonClient
                       for tests.
    """
    daemon = native_daemon or get_native_daemon_client()
    client = get_client(client_name)
    state = load_state()

    # Reap phantom sessions so we don't short-circuit on a dead "active" row.
    reconcile()
    state = load_state()

    # Validate parent session FIRST so a bad ID always errors, even when
    # a short-circuit would otherwise fire — and BEFORE worktree creation
    # so a bad --parent on a worktree-mode client doesn't orphan an on-disk
    # worktree.
    parent_session: Session | None = None
    if parent is not None:
        parent_session = state.find_by_name_or_id(parent)
        if parent_session is None:
            msg = f"Parent session not found: {parent}"
            raise CwError(msg)
        if SessionPurpose.IMPL not in client.auto_purposes:
            msg = (
                "--parent requires the client config to include 'impl' in auto_purposes"
            )
            raise CwError(msg)

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
        client = client.model_copy(update={"workspace_path": worktree_path})
        click.echo(f"Worktree ready: {worktree_path}")
    elif worktree:
        click.echo(f"Creating worktree for branch '{worktree}'...")
        worktree_path = create_worktree(client, worktree)
        click.echo(f"Worktree ready: {worktree_path}")

    # Check for existing backgrounded session — resume it rather than spawning.
    existing = state.find_session(client_name, purpose)

    if existing and existing.status == SessionStatus.BACKGROUNDED:
        if parent is not None:
            msg = (
                f"Cannot apply --parent to existing backgrounded session "
                f"{existing.name}. Resume without --parent, or complete the "
                f"existing session first."
            )
            raise CwError(msg)
        click.echo(f"Found backgrounded session: {existing.name}")
        resume_session(existing.name, native_daemon=daemon)
        return

    if existing and existing.status == SessionStatus.ACTIVE:
        if parent is not None:
            msg = (
                f"Cannot apply --parent — session {existing.name} is already "
                f"active. Complete or background it first."
            )
            raise CwError(msg)
        click.echo(f"Session already active: {existing.name}")
        return

    # Determine cwd: worktree-eligible purposes use worktree_path when available.
    session_cwd: Path = (
        worktree_path
        if (worktree_path and purpose in WORKTREE_PURPOSES)
        else client.workspace_path
    )

    # Build the new session object.
    session = Session(
        name=f"{client_name}/{purpose}",
        client=client_name,
        purpose=SessionPurpose(purpose),
        workspace_path=client.workspace_path,
        origin=SessionOrigin.USER,
    )
    if worktree_path and purpose in WORKTREE_PURPOSES:
        session.worktree_path = worktree_path
        session.branch = worktree_branch

    if parent_session is not None:
        session.parent_session_id = parent_session.id
        parent_session.worker_session_ids.append(session.id)

    state.sessions.append(session)

    # Write Stop hook + correlation context before spawning. Raises
    # HookContextConflictError if a USER-origin worktree already has
    # settings.local.json (gate-behind-worktree strategy from #165).
    _write_hook_context(
        session_cwd,
        session_id=session.id,
        session_name=session.name,
        client=client_name,
        purpose=purpose,
        ticket_id=None,
        origin=SessionOrigin.USER,
    )

    # Build per-purpose system prompt for the session.
    prompt = (
        get_purpose_prompt(
            purpose,
            client.purpose_prompts,
            client_name=client_name,
            workspace_path=str(client.workspace_path),
        )
        or ""
    )

    click.echo(f"Spawning session {session.name}...")
    short_id = daemon.spawn_bg(cwd=session_cwd, prompt=prompt)
    session.surface_ref = short_id

    save_state(state)
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
    click.echo(f"Session {session.name} started (short-id: {short_id}).")
    click.echo(_DETACH_HINT)
    _attach_session(short_id)


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


def background_session(
    session_name: str | None = None,
    *,
    notify: str | None = None,
    auto: bool = False,
) -> None:
    """Background a session by triggering /session-done and recording the handoff."""
    state = load_state()
    session = _resolve_session(state, session_name)

    if session.status not in (SessionStatus.ACTIVE, SessionStatus.IDLE):
        msg = f"Session {session.name} is not active or idle (status: {session.status})"
        raise CwError(msg)

    click.echo(f"Backgrounding session: {session.name}...")

    latest = find_latest_handoff(session.workspace_path)
    if latest:
        session.last_handoff_path = latest

    if session.status == SessionStatus.ACTIVE:
        click.echo(
            "Marking as backgrounded without /session-done injection"
            " (not inside a cmux session)."
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
        click.echo(
            f"Warning: --notify {notify!r} is not supported in native daemon mode."
        )


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
    native_daemon: NativeDaemonClient | None = None,
) -> None:
    """Resume a backgrounded session by attaching to its daemon session.

    Args:
        session_name: Session name or ID to resume.
        native_daemon: NativeDaemonClient instance. Defaults to
                       get_native_daemon_client(). Inject FakeNativeDaemonClient
                       for tests.
    """
    daemon = native_daemon or get_native_daemon_client()

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

    # Extract handoff context for the resumption prompt.
    handoff_prompt: str | None = None
    handoff_path = session.last_handoff_path
    if handoff_path and handoff_path.exists():
        handoff_prompt = extract_resumption_prompt(handoff_path)
        if handoff_prompt:
            click.echo(f"Loaded resumption context from: {handoff_path}")
        else:
            click.echo("Warning: Could not extract resumption prompt from handoff.")
    else:
        click.echo("No handoff file available. Starting fresh session.")

    client = get_client(session.client)
    context = build_session_context(
        session.client,
        str(client.workspace_path),
        session.purpose,
    )
    full_prompt = f"{context}\n\n{handoff_prompt}" if handoff_prompt else context

    surface = session.surface_ref
    live_ids = daemon.list_live_session_short_ids()

    if surface and _is_native_surface_ref(surface) and surface in live_ids:
        # Happy path: session still alive in daemon — attach directly.
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
        click.echo(f"Resuming session {session.name} (short-id: {surface}).")
        click.echo(_DETACH_HINT)
        _attach_session(surface)
    else:
        # Dead or missing surface: spawn a new daemon session using --resume
        # to re-enter the Claude transcript.
        session_cwd: Path = session.worktree_path or session.workspace_path

        extra_args: list[str] = []
        if session.claude_session_id:
            extra_args = ["--resume", session.claude_session_id]

        # Forward client.worker_model for DAEMON-origin resume, mirroring the
        # spawn_create_impl chokepoint. USER-origin sessions inherit the
        # operator's default model (issue #248).
        if session.origin == SessionOrigin.DAEMON and client.worker_model:
            extra_args = [*extra_args, "--model", client.worker_model]

        click.echo(
            f"Session {session.name} not live in daemon;"
            " spawning new background session..."
        )
        new_short_id = daemon.spawn_bg(
            cwd=session_cwd,
            prompt=full_prompt,
            extra_args=extra_args or None,
        )
        session.surface_ref = new_short_id
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
        click.echo(f"New daemon session started (short-id: {new_short_id}).")
        click.echo(_DETACH_HINT)
        _attach_session(new_short_id)


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

    if cleanup and session.worktree_path and session.branch:
        client = get_client(session.client)
        click.echo(f"Removing worktree for branch '{session.branch}'...")
        remove_worktree(client, session.branch, force=force)
        click.echo("Worktree removed.")

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
