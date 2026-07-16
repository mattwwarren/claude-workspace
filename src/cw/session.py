"""Session lifecycle management: start, background, resume, list."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from pathlib import Path

from cw.config import (
    get_client,
    load_orchestrator_config,
    load_state,
    mutate_state,
    save_state,
    sessions_lock,
)
from cw.exceptions import CwError
from cw.history import EventType, HistoryEvent, record_event
from cw.models import (
    ClientConfig,
    CompletionReason,
    CwState,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
)
from cw.native_daemon import (
    NativeDaemonClient,
    get_native_daemon_client,
    resolve_permission_mode,
)
from cw.prompts import build_session_context, get_purpose_prompt
from cw.reconcile import reconcile
from cw.spawn import (
    _ROSTER_POLL_INTERVAL_SECS,
    _ROSTER_POLL_TIMEOUT_SECS,
    _verify_roster_registration,
    _write_hook_context,
    build_disallowed_tools_arg,
)
from cw.worktree import (
    _git_dir,
    check_not_main_checkout,
    create_worktree,
    remove_worktree,
)

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


def _resolve_start_worktree(
    client: ClientConfig, worktree: str | None
) -> tuple[ClientConfig, Path | None, str | None]:
    """Resolve the worktree for a starting session.

    Returns the (possibly updated) client, the worktree path (or None), and the
    worktree branch (or None). For worktree-mode clients the branch is taken
    from the client config and the client's workspace_path is rebound to the new
    worktree; otherwise an explicit ``worktree`` branch is honored.
    """
    worktree_path: Path | None = None
    worktree_branch: str | None = worktree
    if client.is_worktree_client:
        branch = client.branch
        if branch is None:
            msg = "Worktree client must have branch set"
            raise CwError(msg)
        click.echo(f"Creating worktree for branch '{branch}'...")
        worktree_path = create_worktree(client, branch)
        check_not_main_checkout(worktree_path, client)
        worktree_branch = branch
        client = client.model_copy(update={"workspace_path": worktree_path})
        click.echo(f"Worktree ready: {worktree_path}")
    elif worktree:
        click.echo(f"Creating worktree for branch '{worktree}'...")
        worktree_path = create_worktree(client, worktree)
        check_not_main_checkout(worktree_path, client)
        click.echo(f"Worktree ready: {worktree_path}")
    return client, worktree_path, worktree_branch


def _resume_or_skip_existing(
    existing: Session | None,
    *,
    parent: str | None,
    daemon: NativeDaemonClient,
) -> bool:
    """Handle an existing backgrounded/active session for ``start_session``.

    Returns True if the caller should return early (the existing session was
    resumed or is already active); False if a new session should be spawned.
    Raises CwError if ``--parent`` is applied to an existing session.
    """
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
        return True

    if existing and existing.status == SessionStatus.ACTIVE:
        if parent is not None:
            msg = (
                f"Cannot apply --parent — session {existing.name} is already "
                f"active. Complete or background it first."
            )
            raise CwError(msg)
        click.echo(f"Session already active: {existing.name}")
        return True

    return False


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
    client, worktree_path, worktree_branch = _resolve_start_worktree(client, worktree)

    # Check for existing backgrounded session — resume it rather than spawning.
    existing = state.find_session(client_name, purpose)
    if _resume_or_skip_existing(existing, parent=parent, daemon=daemon):
        return

    # Determine cwd: worktree-eligible purposes use worktree_path when available.
    is_worktree_homed = bool(worktree_path) and purpose in WORKTREE_PURPOSES
    session_cwd: Path = (
        worktree_path if worktree_path and is_worktree_homed else client.workspace_path
    )

    # Build the new session object.
    session = Session(
        name=f"{client_name}/{purpose}",
        client=client_name,
        purpose=SessionPurpose(purpose),
        workspace_path=client.workspace_path,
        origin=SessionOrigin.USER,
    )
    if is_worktree_homed:
        session.worktree_path = worktree_path
        session.branch = worktree_branch

    # Write Stop hook + correlation context before spawning. Raises
    # HookContextConflictError if a USER-origin worktree already has
    # settings.local.json (gate-behind-worktree strategy from #165).
    #
    # workspace_path (below) is set only when this session is genuinely
    # worktree-homed (is_worktree_homed, same condition as session_cwd above)
    # — cw guard-cwd (#940 R5) blocks a Bash call when cwd resolves to
    # workspace_path, so setting it for a legitimately main-homed session
    # (debt/explore, or impl/idea without a worktree) would wedge every one
    # of that session's Bash calls. For a worktree-mode client,
    # `client.workspace_path` was already rebound to the new worktree path
    # above (_resolve_start_worktree), so it can't be used here — `_git_dir`
    # (the same helper `check_not_main_checkout` uses) resolves the real
    # main checkout regardless.
    _write_hook_context(
        session_cwd,
        session_id=session.id,
        session_name=session.name,
        client=client_name,
        purpose=purpose,
        ticket_id=None,
        origin=SessionOrigin.USER,
        workspace_path=_git_dir(client) if is_worktree_homed else None,
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

    def _append(state: CwState) -> None:
        # Reload under lock to pick up any mutations since the post-reconcile
        # load; append the new session and save atomically.
        if parent_session is not None:
            # Re-resolve parent under lock so the worker_session_ids append
            # lands on the freshest copy.
            live_parent = state.find_by_name_or_id(parent_session.id)
            if live_parent is not None:
                session.parent_session_id = live_parent.id
                live_parent.worker_session_ids.append(session.id)
        state.sessions.append(session)

    mutate_state(_append)
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
    captured: list[Session] = []

    def _bg(state: CwState) -> None:
        s = _resolve_session(state, session_name)
        if s.status not in (SessionStatus.ACTIVE, SessionStatus.IDLE):
            msg = f"Session {s.name} is not active or idle (status: {s.status})"
            raise CwError(msg)
        click.echo(f"Backgrounding session: {s.name}...")
        if s.status == SessionStatus.ACTIVE:
            click.echo("Marking as backgrounded without /session-done injection.")
        s.status = SessionStatus.BACKGROUNDED
        s.backgrounded_at = datetime.now(UTC)
        if auto:
            s.auto_backgrounded = True
        captured.append(s)

    mutate_state(_bg)
    session = captured[0]
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


def _resolve_resume_cwd(session: Session, client: ClientConfig) -> Path:
    """Resolve the respawn cwd for a dead resume surface, guarding #940.

    Implements the R2 decision table so a re-spawned worker never lands on the
    operator's main checkout (the #925/#766 isolation breach):

    - (i)  DAEMON-origin with ``worktree_path is None`` — corrupted state; raise
           rather than silently respawn into the main checkout.
    - (ii) USER-origin with a worktree-homed purpose AND a set ``worktree_path``
           — verify that worktree does not degenerately resolve to the main
           checkout (the #300 shape) before respawn. A USER worktree purpose with
           ``worktree_path is None`` (e.g. an ``impl`` session started on a
           non-worktree client) is legitimately main-homed and needs no guard.
    - (iii) USER-origin with a non-worktree purpose (``debt`` etc.) — legitimately
            main-homed; no guard.
    """
    if session.origin is SessionOrigin.DAEMON and session.worktree_path is None:
        msg = (
            f"Refusing to resume DAEMON session {session.name}: worktree_path is"
            " unset (corrupted state). A daemon worker must never respawn into"
            " the operator main checkout."
        )
        raise CwError(msg)
    session_cwd: Path = session.worktree_path or session.workspace_path
    if (
        session.origin is SessionOrigin.USER
        and session.worktree_path is not None
        and session.purpose in WORKTREE_PURPOSES
    ):
        check_not_main_checkout(session_cwd, client)
    return session_cwd


def _resume_spawn_args(
    session: Session, client: ClientConfig
) -> tuple[list[str], str | None]:
    """Compute (extra_args, permission_mode) for a dead-surface respawn.

    Mirrors the ``spawn_create_impl`` chokepoint. ``--resume <uuid>`` re-enters
    the Claude transcript when available. For DAEMON-origin sessions only
    (USER-origin inherits the operator's interactive defaults): forward
    ``--model`` from ``client.worker_model`` (#248), forward any
    ``OrchestratorConfig.disallowed_mcp_tools`` restriction (replaces the
    former tracker-gated Linear block, #726), and fall back to
    ``bypassPermissions`` when the pinned model lacks ``--permission-mode
    auto`` (#1111).
    """
    extra_args: list[str] = []
    if session.claude_session_id:
        extra_args = ["--resume", session.claude_session_id]

    if session.origin != SessionOrigin.DAEMON:
        return extra_args, None

    if client.worker_model:
        extra_args = [*extra_args, "--model", client.worker_model]
    extra_args = [
        *extra_args,
        *build_disallowed_tools_arg(load_orchestrator_config().disallowed_mcp_tools),
    ]

    permission_mode = resolve_permission_mode(client.worker_model)
    return extra_args, permission_mode


def resume_session(
    session_name: str,
    *,
    native_daemon: NativeDaemonClient | None = None,
    _roster_poll_timeout: float = _ROSTER_POLL_TIMEOUT_SECS,
    _roster_poll_interval: float = _ROSTER_POLL_INTERVAL_SECS,
) -> None:
    """Resume a backgrounded session by attaching to its daemon session.

    Args:
        session_name: Session name or ID to resume.
        native_daemon: NativeDaemonClient instance. Defaults to
                       get_native_daemon_client(). Inject FakeNativeDaemonClient
                       for tests.

    The underscore-prefixed poll parameters are injectable for testing only.
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

    client = get_client(session.client)
    context = build_session_context(
        session.client,
        str(client.workspace_path),
        session.purpose,
    )
    full_prompt = context

    surface = session.surface_ref
    live_ids = daemon.list_live_session_short_ids()

    if surface and _is_native_surface_ref(surface) and surface in live_ids:
        # Happy path: session still alive in daemon — attach directly.
        def _update_live(state: CwState) -> None:
            live = state.find_by_name_or_id(session_name)
            if live is not None:
                live.status = SessionStatus.ACTIVE
                live.resumed_at = datetime.now(UTC)

        mutate_state(_update_live)
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
        # to re-enter the Claude transcript. Guard the respawn cwd (#940) so a
        # worktree worker never re-lands on the operator main checkout.
        session_cwd = _resolve_resume_cwd(session, client)

        extra_args, resume_permission_mode = _resume_spawn_args(session, client)

        click.echo(
            f"Session {session.name} not live in daemon;"
            " spawning new background session..."
        )
        new_short_id = daemon.spawn_bg(
            cwd=session_cwd,
            prompt=full_prompt,
            extra_args=extra_args or None,
            permission_mode=resume_permission_mode,
        )
        _verify_roster_registration(
            daemon,
            new_short_id,
            timeout=_roster_poll_timeout,
            interval=_roster_poll_interval,
        )

        def _update_dead(state: CwState) -> None:
            dead = state.find_by_name_or_id(session_name)
            if dead is not None:
                dead.surface_ref = new_short_id
                dead.status = SessionStatus.ACTIVE
                dead.resumed_at = datetime.now(UTC)

        mutate_state(_update_dead)
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
    # Why not mutate_state: remove_worktree (git subprocess) runs inside the
    # lock window on the --cleanup path (criterion 1: no subprocess in lock).
    with sessions_lock():
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
