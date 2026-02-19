"""Session lifecycle management: start, background, resume, list."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from cw.models import ClientConfig, CwState, Session, SessionPurpose, SessionStatus
from cw.prompts import escape_kdl_string, get_purpose_prompt
from cw.worktree import create_worktree, remove_worktree

CW_SESSION = "cw"

# Timing constants for session lifecycle
HANDOFF_POLL_TIMEOUT_S = 30
HANDOFF_POLL_INTERVAL_S = 1
CLAUDE_INIT_DELAY_S = 2


def _navigate_to_pane(session: Session, *, target: str | None = None) -> None:
    """Navigate to a session's tab and pane in Zellij."""
    zellij.go_to_tab(session.zellij_tab or session.client, session=target)
    zellij.focus_pane(session.zellij_pane or session.purpose, session=target)


def _ensure_zellij() -> None:
    """Verify zellij is installed."""
    if not zellij.is_installed():
        msg = "Zellij is not installed. Install it: https://zellij.dev/documentation/installation"
        raise CwError(msg)


def _build_pane_args(
    sessions: dict[str, Session],
    resume: bool = False,
    client: ClientConfig | None = None,
) -> dict[str, dict[str, str]]:
    """Build KDL-formatted claude args for each pane.

    Args:
        sessions: Map of purpose name to Session with claude_session_id.
        resume: If True, use --resume instead of --session-id.
        client: Client config for resolving purpose prompts.
    """
    panes: dict[str, dict[str, str]] = {}
    client_overrides = client.purpose_prompts if client else None
    for purpose, session in sessions.items():
        sid = str(session.claude_session_id)
        flag = "--resume" if resume else "--session-id"
        args_parts = [f'"{flag}" "{sid}"']

        # Add purpose-specific system prompt
        prompt = get_purpose_prompt(purpose, client_overrides)
        if prompt:
            escaped = escape_kdl_string(prompt)
            args_parts.append(f'"--append-system-prompt" "{escaped}"')

        pane_data: dict[str, str] = {"claude_args": " ".join(args_parts)}
        # Set cwd to worktree path if available, else workspace path
        cwd = str(session.worktree_path or session.workspace_path)
        pane_data["cwd"] = cwd
        panes[purpose] = pane_data
    return panes


def _create_all_purpose_sessions(
    client_name: str,
    client: ClientConfig,
    state: CwState,
    prior_sessions: dict[str, Session] | None = None,
    *,
    worktree_path: Path | None = None,
    worktree_branch: str | None = None,
) -> dict[str, Session]:
    """Create Session objects for all purposes.

    Reuses prior claude_session_ids when available for resumption.
    worktree_path/branch apply to impl and review purposes.
    """
    # Purposes that get the worktree cwd
    worktree_purposes = {"impl", "review"}

    sessions: dict[str, Session] = {}
    for purpose_enum in client.auto_purposes:
        purpose = purpose_enum.value
        session = Session(
            name=f"{client_name}/{purpose}",
            client=client_name,
            purpose=purpose_enum,
            workspace_path=client.workspace_path,
            zellij_pane=purpose,
            zellij_tab=client_name,
        )
        # Apply worktree to impl and review panes
        if worktree_path and purpose in worktree_purposes:
            session.worktree_path = worktree_path
            session.branch = worktree_branch
        # Carry forward Claude session ID from prior session for resumption
        if prior_sessions and purpose in prior_sessions:
            session.claude_session_id = prior_sessions[purpose].claude_session_id
        sessions[purpose] = session
        state.sessions.append(session)
    return sessions


def _create_session_if_needed(
    client: ClientConfig,
    panes: dict[str, dict[str, str]] | None = None,
) -> bool:
    """Create and attach to the cw Zellij session if it doesn't exist.

    Returns True if a new session was created (terminal taken over),
    False if already running.
    """
    if not zellij.session_exists(CW_SESSION):
        purposes = [p.value for p in client.auto_purposes]
        layout_path = zellij.generate_layout(client, panes=panes, purposes=purposes)
        click.echo(f"Launching Zellij session '{CW_SESSION}' for {client.name}...")
        # This will take over the terminal - user lands directly in the session
        zellij.create_and_attach(CW_SESSION, layout_path)
        return True
    return False


def start_session(
    client_name: str,
    purpose: str,
    *,
    worktree: str | None = None,
) -> None:
    """Start or resume a Claude Code session for a client."""
    _ensure_zellij()
    client = get_client(client_name)
    state = load_state()

    # Create worktree if requested
    worktree_path: Path | None = None
    if worktree:
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
            health = zellij.check_pane_health(session=CW_SESSION)
            pane_name = existing.zellij_pane or existing.purpose
            if health.get(pane_name) is False:
                click.echo(f"Claude crashed in {existing.name}. Recovering...")
                existing.status = SessionStatus.COMPLETED
                save_state(state)
                # Fall through to fresh start / recovery below
            else:
                click.echo(f"Session already active: {existing.name}")
                if zellij.in_zellij_session():
                    _navigate_to_pane(existing)
                else:
                    click.echo(f"Attaching to Zellij session '{CW_SESSION}'...")
                    zellij.attach_session(CW_SESSION)
                return
        # Zellij session died - collect prior sessions for all purposes so we can
        # resume each Claude session in its correct pane
        click.echo("Zellij session gone. Recovering sessions...")
        prior_sessions: dict[str, Session] = {}
        for s in state.sessions:
            if s.client == client_name and s.status == SessionStatus.ACTIVE:
                prior_sessions[s.purpose] = s
                s.status = SessionStatus.COMPLETED
        save_state(state)

        # Create new sessions for all purposes, carrying forward Claude session IDs
        all_sessions = _create_all_purpose_sessions(
            client_name, client, state, prior_sessions=prior_sessions,
        )
        save_state(state)

        # Build layout with --resume for panes that had prior sessions,
        # --session-id for fresh ones
        panes: dict[str, dict[str, str]] = {}
        for p, s in all_sessions.items():
            sid = str(s.claude_session_id)
            if p in prior_sessions:
                args_parts = [f'"--resume" "{sid}"']
            else:
                args_parts = [f'"--session-id" "{sid}"']
            prompt = get_purpose_prompt(p, client.purpose_prompts or None)
            if prompt:
                escaped = escape_kdl_string(prompt)
                args_parts.append(f'"--append-system-prompt" "{escaped}"')
            panes[p] = {"claude_args": " ".join(args_parts)}

        click.echo("Resuming Claude sessions in new Zellij layout...")
        _create_session_if_needed(client, panes=panes)
        return  # User is now inside Zellij

    # Fresh start - no existing session for this client
    if not zellij.session_exists(CW_SESSION):
        # Create sessions for ALL purposes and bake claude into the layout
        all_sessions = _create_all_purpose_sessions(
            client_name, client, state,
            worktree_path=worktree_path,
            worktree_branch=worktree,
        )
        save_state(state)

        panes = _build_pane_args(all_sessions, resume=False, client=client)
        click.echo(f"Launching Zellij session '{CW_SESSION}' for {client_name}...")
        for s in all_sessions.values():
            click.echo(f"  {s.name} (claude session: {s.claude_session_id})")
        _create_session_if_needed(client, panes=panes)
        return  # User is now inside Zellij

    # Zellij already running - inject claude into panes
    # Create a single session for the requested purpose
    session = Session(
        name=f"{client_name}/{purpose}",
        client=client_name,
        purpose=SessionPurpose(purpose),
        workspace_path=client.workspace_path,
        zellij_pane=purpose,
        zellij_tab=client_name,
    )
    state.sessions.append(session)
    save_state(state)

    # Build claude command with optional system prompt
    cmd_parts = [f"claude --session-id {session.claude_session_id}"]
    prompt = get_purpose_prompt(purpose, client.purpose_prompts or None)
    if prompt:
        # Shell-escape the prompt for injection
        escaped_prompt = prompt.replace("'", "'\\''")
        cmd_parts.append(f"--append-system-prompt '{escaped_prompt}'")
    claude_cmd = " ".join(cmd_parts) + "\n"
    click.echo(f"Started session: {session.name} (id: {session.id})")

    # Inject claude into the pane - works both inside and outside Zellij
    # by targeting the session explicitly when outside
    target = None if zellij.in_zellij_session() else CW_SESSION
    _navigate_to_pane(session, target=target)
    zellij.write_to_pane(claude_cmd, session=target)

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


def background_session(session_name: str | None = None) -> None:
    """Background a session by triggering /session-done and recording the handoff."""
    state = load_state()
    session = _resolve_session(state, session_name)

    if session.status != SessionStatus.ACTIVE:
        msg = f"Session {session.name} is not active (status: {session.status})"
        raise CwError(msg)

    click.echo(f"Backgrounding session: {session.name}...")

    before_mtime = time.time()

    if zellij.in_zellij_session():
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
    save_state(state)
    click.echo(f"Session {session.name} backgrounded.")


def resume_session(session_name: str) -> None:
    """Resume a backgrounded session with its handoff context."""
    _ensure_zellij()
    state = load_state()

    session = state.find_by_name_or_id(session_name)
    if session is None:
        msg = f"Session not found: {session_name}"
        raise CwError(msg)

    if session.status != SessionStatus.BACKGROUNDED:
        msg = f"Session {session.name} is not backgrounded (status: {session.status})"
        raise CwError(msg)

    # Extract resumption prompt from handoff
    prompt = None
    if session.last_handoff_path and session.last_handoff_path.exists():
        prompt = extract_resumption_prompt(session.last_handoff_path)
        if prompt:
            click.echo(f"Loaded resumption context from: {session.last_handoff_path}")
        else:
            click.echo("Warning: Could not extract resumption prompt from handoff.")
    else:
        click.echo("No handoff file available. Starting fresh session.")

    # Ensure client tab exists
    client = get_client(session.client)
    _create_session_if_needed(client)

    session.status = SessionStatus.ACTIVE
    session.resumed_at = datetime.now(UTC)
    save_state(state)

    click.echo(f"Resumed session: {session.name}")

    if zellij.in_zellij_session():
        _navigate_to_pane(session)

        # Resume the exact Claude session by ID, then inject handoff context
        zellij.write_to_pane(f"claude --resume {session.claude_session_id}\n")
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
    save_state(state)
    click.echo(f"Session {session.name} marked as completed.")

    if cleanup and session.worktree_path and session.branch:
        client = get_client(session.client)
        click.echo(f"Removing worktree for branch '{session.branch}'...")
        remove_worktree(client, session.branch, force=force)
        click.echo("Worktree removed.")


def hand_to_session(
    target_purpose: str,
    message: str,
    source_purpose: str | None = None,
) -> None:
    """Hand off a message to another session's Claude instance.

    Writes the message to a shared file and injects it into the
    target pane via keystroke injection.
    """
    state = load_state()

    active = state.active_sessions()
    if not active:
        msg = "No active sessions."
        raise CwError(msg)

    # All active sessions should be the same client
    client_name = active[0].client

    target = state.find_session(client_name, target_purpose)
    if target is None or target.status != SessionStatus.ACTIVE:
        msg = f"No active {target_purpose} session for {client_name}."
        raise CwError(msg)

    from_label = source_purpose or "user"

    # Persist to shared location for auditability
    messages_dir = target.workspace_path / ".cw" / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    msg_file = messages_dir / f"{ts}-{from_label}-to-{target_purpose}.md"
    msg_file.write_text(
        f"# Handoff: {from_label} -> {target_purpose}\n\n"
        f"{message}\n"
    )
    click.echo(f"Message saved: {msg_file.name}")

    # Inject into the target pane
    zellij_target = (
        None if zellij.in_zellij_session() else CW_SESSION
    )
    _navigate_to_pane(target, target=zellij_target)
    zellij.write_to_pane(message + "\n", session=zellij_target)

    click.echo(f"Delivered to {client_name}/{target_purpose}.")


