"""Session lifecycle management: start, background, resume, list."""

from __future__ import annotations

import shlex
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
    build_cross_session_prompt,
    extract_resumption_prompt,
    find_handoffs_newer_than,
    find_latest_handoff,
)
from cw.history import EventType, HistoryEvent, record_event
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    QueueItem,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TaskSpec,
)
from cw.prompts import build_session_context, get_purpose_prompt
from cw.queue import add_item, claim_next, load_queue, save_queue
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
    """Build pane data for each session including a resilient claude command.

    The command tries --resume first (reconnects to existing conversation),
    falling back to --session-id (creates new session). This handles both
    fresh launches and Zellij restarts after detach/reattach.

    Args:
        sessions: Map of purpose name to Session with claude_session_id.
        client: Client config for resolving purpose prompts.
    """
    panes: dict[str, dict[str, str]] = {}
    client_overrides = client.purpose_prompts if client else None
    client_name = client.name if client else None
    workspace_path = str(client.workspace_path) if client else None
    for purpose, session in sessions.items():
        sid = str(session.claude_session_id)

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

        # Shell command: try --resume, fall back to --session-id
        # Prefix with env vars so tools can detect identity
        if client_name:
            env_prefix = f"{_build_env_prefix(client_name, purpose)} "
        else:
            env_prefix = ""
        cmd = (
            f"{env_prefix}claude --resume {sid}{extra} 2>/dev/null"
            f" || {env_prefix}claude --session-id {sid}{extra}"
        )
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
    prior_sessions: dict[str, Session] | None = None,
    worktree_path: Path | None = None,
    worktree_branch: str | None = None,
) -> dict[str, Session]:
    """Create Session objects for all purposes.

    When prior_sessions is provided, carries forward their claude_session_ids
    so that --resume can reconnect to existing Claude conversations.
    worktree_path/branch apply to impl and idea purposes.
    """
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
        # Apply worktree to impl and idea panes
        if worktree_path and purpose in WORKTREE_PURPOSES:
            session.worktree_path = worktree_path
            session.branch = worktree_branch
        # Carry forward Claude session ID for resumption
        if prior_sessions and purpose in prior_sessions:
            session.claude_session_id = prior_sessions[purpose].claude_session_id
        sessions[purpose] = session
        state.sessions.append(session)
        record_event(client_name, HistoryEvent(
            event_type=EventType.SESSION_STARTED,
            client=client_name,
            session_id=session.id,
            session_name=session.name,
            purpose=purpose,
        ))
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
                session=CW_SESSION, tab_name=client_name,
            )
            pane_name = existing.zellij_pane or existing.purpose
            if health.get(pane_name) is False:
                click.echo(f"Claude crashed in {existing.name}. Recovering...")
                existing.status = SessionStatus.COMPLETED
                existing.completed_reason = CompletionReason.CRASHED
                existing.completed_at = datetime.now(UTC)
                save_state(state)
                record_event(client_name, HistoryEvent(
                    event_type=EventType.SESSION_CRASHED,
                    client=client_name,
                    session_id=existing.id,
                    session_name=existing.name,
                    purpose=existing.purpose,
                ))
                # Fall through to fresh start / recovery below
            else:
                click.echo(f"Session already active: {existing.name}")
                if zellij.in_zellij_session():
                    _navigate_to_pane(existing)
                else:
                    click.echo(f"Attaching to Zellij session '{CW_SESSION}'...")
                    zellij.attach_session(CW_SESSION)
                return
        # Zellij session died - collect prior sessions so we can resume
        # each Claude conversation in its correct pane using --resume.
        click.echo("Zellij session gone. Recovering sessions...")
        prior_sessions: dict[str, Session] = {}
        for s in state.sessions:
            if s.client == client_name and s.status == SessionStatus.ACTIVE:
                prior_sessions[s.purpose] = s
                s.status = SessionStatus.COMPLETED

        # Create new cw sessions, carrying forward Claude session IDs
        all_sessions = _create_all_purpose_sessions(
            client_name, client, state, prior_sessions=prior_sessions,
        )
        save_state(state)

        panes = _build_pane_args(all_sessions, client=client)
        click.echo("Resuming Claude sessions in new Zellij layout...")
        _create_session_if_needed(client, panes=panes)
        return  # User is now inside Zellij

    # Create sessions for ALL purposes and build pane layout
    all_sessions = _create_all_purpose_sessions(
        client_name, client, state,
        worktree_path=worktree_path,
        worktree_branch=worktree_branch,
    )
    save_state(state)
    panes = _build_pane_args(all_sessions, client=client)

    for s in all_sessions.values():
        click.echo(f"  {s.name} (claude session: {s.claude_session_id})")

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
            f"Warning: No active {target_purpose} session"
            f" for {client_name} to notify."
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
    if auto:
        session.auto_backgrounded = True
    save_state(state)
    record_event(session.client, HistoryEvent(
        event_type=EventType.SESSION_BACKGROUNDED,
        client=session.client,
        session_id=session.id,
        session_name=session.name,
        purpose=session.purpose,
    ))
    click.echo(f"Session {session.name} backgrounded.")

    if notify:
        _notify_sibling(session.client, session.purpose, notify)


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
    handoff_path = session.last_handoff_path
    if handoff_path and handoff_path.exists():
        prompt = extract_resumption_prompt(handoff_path)
        if prompt:
            click.echo(f"Loaded resumption context from: {handoff_path}")
        else:
            click.echo("Warning: Could not extract resumption prompt from handoff.")
    else:
        click.echo("No handoff file available. Starting fresh session.")

    # Clean up cross-session handoff files after extracting prompt
    _cleanup_cross_session_handoff(session)

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
    record_event(session.client, HistoryEvent(
        event_type=EventType.SESSION_RESUMED,
        client=session.client,
        session_id=session.id,
        session_name=session.name,
        purpose=session.purpose,
    ))

    click.echo(f"Resumed session: {session.name}")

    if zellij.in_zellij_session():
        _navigate_to_pane(session)

        # Resume the exact Claude session by ID, then inject handoff context
        env_prefix = _build_env_prefix(session.client, session.purpose)
        zellij.write_to_pane(
            f"{env_prefix} claude --resume {session.claude_session_id}\n"
        )
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
    record_event(session.client, HistoryEvent(
        event_type=EventType.SESSION_COMPLETED,
        client=session.client,
        session_id=session.id,
        session_name=session.name,
        purpose=session.purpose,
    ))
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
    zellij_target = zellij.resolve_session_target(CW_SESSION)
    _navigate_to_pane(target, target=zellij_target)
    zellij.write_to_pane(message + "\n", session=zellij_target)

    click.echo(f"Delivered to {client_name}/{target_purpose}.")


def _resolve_client_for_handoff(
    state: CwState,
    client_name: str | None,
) -> str:
    """Determine the client name for a handoff operation."""
    if client_name:
        return client_name

    active = state.active_sessions()
    bg = state.backgrounded_sessions()
    all_live = active + bg
    if not all_live:
        msg = "No active or backgrounded sessions."
        raise CwError(msg)

    clients = {s.client for s in all_live}
    if len(clients) > 1:
        names = ", ".join(sorted(clients))
        msg = f"Multiple clients have sessions. Specify --client: {names}"
        raise CwError(msg)
    return clients.pop()


def handoff_session(
    source_purpose: str,
    target_purpose: str,
    *,
    client_name: str | None = None,
) -> None:
    """Background source session and deliver cross-session context to target."""
    if source_purpose == target_purpose:
        msg = f"Source and target cannot be the same: {source_purpose}"
        raise CwError(msg)

    state = load_state()
    resolved_client = _resolve_client_for_handoff(state, client_name)

    source = state.find_session(resolved_client, source_purpose)
    if source is None or source.status not in (
        SessionStatus.ACTIVE,
        SessionStatus.BACKGROUNDED,
    ):
        msg = f"No active/backgrounded {source_purpose} session for {resolved_client}."
        raise CwError(msg)

    target = state.find_session(resolved_client, target_purpose)
    if target is None or target.status == SessionStatus.COMPLETED:
        msg = f"No active/backgrounded {target_purpose} session for {resolved_client}."
        raise CwError(msg)

    # Background source if active
    raw_prompt: str | None = None
    if source.status == SessionStatus.ACTIVE:
        click.echo(f"Backgrounding {source.name}...")
        background_session(source.name)
        # Reload state after background_session saved
        state = load_state()
        source = state.find_by_name_or_id(source.id)
        if source is None:
            msg = "Source session lost after backgrounding."
            raise CwError(msg)

    # Extract resumption prompt from source handoff
    if source.last_handoff_path and source.last_handoff_path.exists():
        raw_prompt = extract_resumption_prompt(source.last_handoff_path)

    # Build cross-session prompt before modifying state
    prompt = build_cross_session_prompt(
        source_purpose,
        target_purpose,
        source.branch,
        raw_prompt,
    )

    # Mark source as completed with HANDOFF reason
    source.status = SessionStatus.COMPLETED
    source.completed_reason = CompletionReason.HANDOFF
    source.completed_at = datetime.now(UTC)
    record_event(resolved_client, HistoryEvent(
        event_type=EventType.SESSION_HANDOFF,
        client=resolved_client,
        session_id=source.id,
        session_name=source.name,
        purpose=source.purpose,
        detail=f"{source_purpose} -> {target_purpose}",
    ))

    # For backgrounded targets, write handoff file and set path
    # before saving — avoids a second save_state round-trip
    if target.status == SessionStatus.BACKGROUNDED:
        target.last_handoff_path = _write_cross_session_handoff(
            target.workspace_path, source_purpose, target_purpose, prompt,
        )

    save_state(state)

    # Deliver to target
    if target.status == SessionStatus.ACTIVE:
        click.echo(f"Injecting context into {target.name}...")
        zellij_target = zellij.resolve_session_target(CW_SESSION)
        _navigate_to_pane(target, target=zellij_target)
        zellij.write_to_pane(prompt + "\n", session=zellij_target)
    elif target.status == SessionStatus.BACKGROUNDED:
        click.echo(f"Resuming {target.name} with cross-session context...")
        resume_session(target.name)

    click.echo(
        f"Handoff complete: {source_purpose} → {target_purpose}"
        f" ({resolved_client})"
    )


def _persist_queue_assignment(client: str, item: QueueItem) -> None:
    """Save the assigned_session_id back to the queue file."""
    store = load_queue(client)
    stored = store.find_item(item.id)
    if stored is not None:
        stored.assigned_session_id = item.assigned_session_id
        save_queue(client, store)


def delegate_task(
    client_name: str,
    description: str,
    *,
    purpose: str = "debt",
    prompt: str | None = None,
    context_files: list[str] | None = None,
    interactive: bool = False,
) -> Session:
    """Delegate a task to a new Zellij pane running Claude.

    Creates a queue item for tracking, claims it immediately,
    spawns a new session in a dynamic Zellij pane.
    """
    _ensure_zellij()
    if not zellij.in_zellij_session():
        msg = "Must be inside a Zellij session to delegate."
        raise CwError(msg)

    client = get_client(client_name)
    state = load_state()

    task_prompt = prompt or description
    task = TaskSpec(
        description=description,
        purpose=SessionPurpose(purpose),
        prompt=task_prompt,
        context_files=context_files or [],
    )

    # Add to queue and claim immediately
    add_item(client_name, task)
    claimed = claim_next(client_name, SessionPurpose(purpose))
    if claimed is None:
        msg = "Failed to claim queue item."
        raise CwError(msg)

    # Create session
    session = Session(
        name=f"{client_name}/delegate-{claimed.id}",
        client=client_name,
        purpose=SessionPurpose(purpose),
        origin=SessionOrigin.DELEGATE,
        workspace_path=client.workspace_path,
        zellij_pane=f"delegate-{claimed.id}",
    )
    claimed.assigned_session_id = session.id
    _persist_queue_assignment(client_name, claimed)
    state.sessions.append(session)
    save_state(state)

    # Build claude command with identity env vars
    escaped_prompt = shlex.quote(task_prompt)
    sid = str(session.claude_session_id)
    env_prefix = _build_env_prefix(client_name, purpose)
    if interactive:
        cmd = (
            f"{env_prefix} claude --session-id {sid}"
            f" --append-system-prompt {escaped_prompt}"
        )
    else:
        cmd = (
            f"{env_prefix} claude --session-id {sid}"
            f" --append-system-prompt {escaped_prompt}"
            f" --print"
        )

    cwd = str(client.workspace_path)
    pane_name = f"delegate-{claimed.id}"
    zellij.new_pane(cmd, name=pane_name, cwd=cwd, close_on_exit=not interactive)

    click.echo(f"Delegated: {description} (pane: {pane_name}, queue: {claimed.id})")
    return session


CROSS_SESSION_HANDOFF_PREFIX = "session-handoff-"


def _cleanup_cross_session_handoff(session: Session) -> None:
    """Delete a cross-session handoff file after resume_session consumes it.

    Only deletes files with the session-handoff-* prefix (created by
    _write_cross_session_handoff). Regular /session-done handoffs are
    preserved for audit.

    Note: Mutates ``session.last_handoff_path`` but does **not** call
    ``save_state`` itself.  Caller must persist state afterward.
    """
    path = session.last_handoff_path
    if path is None or not path.exists():
        return
    if not path.name.startswith(CROSS_SESSION_HANDOFF_PREFIX):
        return
    try:
        path.unlink()
        session.last_handoff_path = None
    except OSError as exc:
        click.echo(f"Warning: failed to clean up handoff file {path}: {exc}", err=True)


def _write_cross_session_handoff(
    workspace_path: Path,
    source_purpose: str,
    target_purpose: str,
    prompt: str,
) -> Path:
    """Write a cross-session handoff file for resume_session to pick up.

    Cleaned up by _cleanup_cross_session_handoff after consumption.
    """
    handoffs_dir = workspace_path / ".handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"session-handoff-{source_purpose}-to-{target_purpose}-{ts}.md"
    path = handoffs_dir / filename
    path.write_text(
        f"# Cross-Session Handoff: {source_purpose} → {target_purpose}\n\n"
        f"## Resumption Prompt\n\n"
        f"```\n{prompt}\n```\n"
    )
    return path


