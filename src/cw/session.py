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

from cw import zellij
from cw.config import get_client, load_state, save_state
from cw.exceptions import CwError
from cw.handoff import (
    extract_resumption_prompt,
    find_handoffs_newer_than,
    find_latest_handoff,
)
from cw.history import EventType, HistoryEvent, record_event
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionStatus,
)
from cw.prompts import build_session_context, get_purpose_prompt
from cw.worktree import create_worktree, remove_worktree

CW_SESSION = "cw"

# Purposes that receive worktree cwd (impl works on the feature branch,
# idea brainstorms within it; debt stays on the main workspace).
WORKTREE_PURPOSES: frozenset[str] = frozenset({"impl", "idea"})


def _build_env_prefix(client_name: str, purpose: str) -> str:
    """Build ``CW_CLIENT=… CW_PURPOSE=…`` shell prefix for claude commands."""
    return f"CW_CLIENT={client_name} CW_PURPOSE={purpose}"


# Timing constants for session lifecycle
HANDOFF_POLL_TIMEOUT_S = 30
HANDOFF_POLL_INTERVAL_S = 1
CLAUDE_INIT_DELAY_S = 2


def _navigate_to_pane(session: Session, *, target: str | None = None) -> None:
    """Navigate to a session's tab and pane in Zellij."""
    tab = session.zellij_tab or session.client
    zellij.go_to_tab(tab, session=target)
    zellij.focus_pane(
        session.zellij_pane or session.purpose,
        session=target,
        tab_name=tab,
    )


def _ensure_zellij() -> None:
    """Verify zellij is installed."""
    if not zellij.is_installed():
        msg = "Zellij is not installed. Install it: https://zellij.dev/documentation/installation"
        raise CwError(msg)


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
            # Collapse newlines — KDL strings cannot span multiple lines
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
        # KDL-quote the whole command for the layout template.
        # Escape backslashes and double quotes so the KDL string is valid.
        kdl_cmd = cmd.replace("\\", "\\\\").replace('"', '\\"')
        pane_data: dict[str, str] = {"claude_cmd": f'"{kdl_cmd}"'}
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
            zellij_pane=purpose,
            zellij_tab=client_name,
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


def _create_session_if_needed(
    client: ClientConfig,
    panes: dict[str, dict[str, str]] | None = None,
) -> bool:
    """Create and attach to the cw Zellij session if it doesn't exist.

    Returns True if a new session was created (terminal taken over),
    False if already running.  Refuses to create a nested session when
    already inside Zellij.
    """
    if zellij.session_exists(CW_SESSION):
        return False

    if zellij.in_zellij_session():
        msg = (
            "Already inside a Zellij session but the 'cw' session"
            " was not found. Cannot create a nested session."
        )
        raise CwError(msg)

    purposes = [p.value for p in client.auto_purposes]
    layout_path = zellij.generate_layout(client, panes=panes, purposes=purposes)
    click.echo(f"Launching Zellij session '{CW_SESSION}' for {client.name}...")
    # This will take over the terminal - user lands directly in the session
    zellij.create_and_attach(CW_SESSION, layout_path)
    return True


def start_session(
    client_name: str,
    purpose: str,
    *,
    worktree: str | None = None,
) -> None:
    """Start or resume a Claude Code session for a client."""
    _ensure_zellij()
    # Clean up stale EXITED Zellij session so we don't try to inject into it
    if zellij.delete_exited_session(CW_SESSION):
        click.echo(f"Cleaned up exited Zellij session '{CW_SESSION}'.")
    client = get_client(client_name)
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
        resume_session(existing.name)
        return

    if existing and existing.status == SessionStatus.ACTIVE:
        # Verify Zellij session is actually running
        if zellij.session_exists(CW_SESSION):
            # Check if Claude is still alive in the pane
            health = zellij.check_pane_health(
                session=CW_SESSION,
                tab_name=client_name,
            )
            pane_name = existing.zellij_pane or existing.purpose
            if health.get(pane_name) is False:
                click.echo(f"Claude crashed in {existing.name}. Recovering...")
                existing.status = SessionStatus.COMPLETED
                existing.completed_reason = CompletionReason.CRASHED
                existing.completed_at = datetime.now(UTC)
                save_state(state)
                record_event(
                    client_name,
                    HistoryEvent(
                        event_type=EventType.SESSION_CRASHED,
                        client=client_name,
                        session_id=existing.id,
                        session_name=existing.name,
                        purpose=existing.purpose,
                    ),
                )
                # Fall through to fresh start / recovery below
            else:
                click.echo(f"Session already active: {existing.name}")
                if zellij.in_zellij_session():
                    _navigate_to_pane(existing)
                else:
                    click.echo(f"Attaching to Zellij session '{CW_SESSION}'...")
                    zellij.attach_session(CW_SESSION)
                return
        # Zellij session died - mark old sessions completed and start fresh.
        click.echo("Zellij session gone. Recovering sessions...")
        prior_sessions: dict[str, Session] = {}
        for s in state.sessions:
            if s.client == client_name and s.status == SessionStatus.ACTIVE:
                prior_sessions[s.purpose] = s
                s.status = SessionStatus.COMPLETED

        all_sessions = _create_all_purpose_sessions(
            client_name,
            client,
            state,
            prior_sessions=prior_sessions,
        )
        save_state(state)

        panes = _build_pane_args(all_sessions, client=client)
        click.echo("Resuming Claude sessions in new Zellij layout...")
        _create_session_if_needed(client, panes=panes)
        return  # User is now inside Zellij

    # Create sessions for ALL purposes and build pane layout
    all_sessions = _create_all_purpose_sessions(
        client_name,
        client,
        state,
        worktree_path=worktree_path,
        worktree_branch=worktree_branch,
    )
    save_state(state)
    panes = _build_pane_args(all_sessions, client=client)

    for s in all_sessions.values():
        click.echo(f"  {s.name}")

    if not zellij.session_exists(CW_SESSION):
        click.echo(f"Launching Zellij session '{CW_SESSION}' for {client_name}...")
        _create_session_if_needed(client, panes=panes)
        return  # User is now inside Zellij

    # Zellij already running — inject a new tab for this client
    click.echo(f"Adding tab for {client_name} to Zellij session '{CW_SESSION}'...")
    zellij.new_tab(client, panes=panes, session=CW_SESSION)

    if not zellij.in_zellij_session():
        click.echo(f"Attaching to Zellij session '{CW_SESSION}'...")
        zellij.attach_session(CW_SESSION)


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


def _wait_for_handoff(workspace_path: Path, before_mtime: float) -> Path | None:
    """Poll for a new handoff file created after before_mtime.

    Returns the path to the new handoff, or None on timeout.
    """
    for _ in range(HANDOFF_POLL_TIMEOUT_S):
        time.sleep(HANDOFF_POLL_INTERVAL_S)
        new_handoffs = find_handoffs_newer_than(workspace_path, before_mtime)
        if new_handoffs:
            return new_handoffs[0]
    return None


def _notify_sibling(client_name: str, source_purpose: str, target_purpose: str) -> None:
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
    zellij_target = zellij.resolve_session_target(CW_SESSION)
    _navigate_to_pane(target, target=zellij_target)
    zellij.write_to_pane(message + "\n", session=zellij_target)
    click.echo(f"Notified {target.name}.")


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

    before_mtime = time.time()

    if session.status == SessionStatus.IDLE:
        # Claude already exited — no /session-done needed.
        latest = find_latest_handoff(session.workspace_path)
        if latest:
            session.last_handoff_path = latest
    elif zellij.in_zellij_session():
        _navigate_to_pane(session)
        zellij.write_to_pane("/session-done\n")

        click.echo("Waiting for handoff generation...")
        handoff_path = _wait_for_handoff(session.workspace_path, before_mtime)
        if handoff_path:
            session.last_handoff_path = handoff_path
            click.echo(f"Handoff saved: {handoff_path}")
        else:
            click.echo(
                f"Warning: No handoff detected within {HANDOFF_POLL_TIMEOUT_S}s."
                " Session marked as backgrounded anyway."
            )
    else:
        latest = find_latest_handoff(session.workspace_path)
        if latest:
            session.last_handoff_path = latest
        click.echo(
            "Not inside Zellij session."
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

    # Update Zellij tab name to indicate backgrounded state
    if zellij.in_zellij_session():
        zellij.rename_tab(f"{session.client} [bg]")

    if notify:
        _notify_sibling(session.client, session.purpose, notify)


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


def resume_session(session_name: str) -> None:
    """Resume a backgrounded session with its handoff context."""
    _ensure_zellij()
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

    # Ensure client tab exists
    client = get_client(session.client)

    # Prepend client identity so resumed sessions know who they are
    context = build_session_context(
        session.client,
        str(client.workspace_path),
        session.purpose,
    )
    prompt = f"{context}\n\n{prompt}" if prompt else context
    _create_session_if_needed(client)

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

    if zellij.in_zellij_session():
        # Restore tab name (remove [bg] suffix)
        zellij.rename_tab(session.client)
        _navigate_to_pane(session)

        # Launch Claude with interactive session picker
        env_prefix = _build_env_prefix(session.client, session.purpose)
        zellij.write_to_pane(f"{env_prefix} claude --resume\n")
        if prompt:
            time.sleep(CLAUDE_INIT_DELAY_S)  # Wait for Claude to initialize
            zellij.write_to_pane(prompt + "\n")
    else:
        click.echo(f"Attach with: zellij attach {CW_SESSION}")
        if prompt:
            click.echo("\nResumption prompt:")
            click.echo(prompt)


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
