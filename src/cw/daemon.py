"""Background debt runner daemon for autonomous queue processing."""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import TYPE_CHECKING

import click

from cw import zellij
from cw.config import DAEMONS_DIR, get_client, load_clients, load_state, save_state
from cw.exceptions import CwError
from cw.handoff import (
    build_daemon_workflow_prompt,
    find_handoffs_newer_than,
    parse_handoff_reason,
)
from cw.history import EventType, HistoryEvent, record_event
from cw.models import (
    ClientConfig,
    CwState,
    QueueItem,
    Session,
    SessionPurpose,
    SessionStatus,
)
from cw.notify import send_notification
from cw.queue import claim_next, complete_item, fail_item
from cw.session import CLAUDE_INIT_DELAY_S, CW_SESSION, start_session

if TYPE_CHECKING:
    from pathlib import Path

# Default timeout for a daemon-driven task (30 minutes).
_DAEMON_TASK_TIMEOUT_S = 1800


def _pid_path(client: str, purpose: str) -> Path:
    return DAEMONS_DIR / f"{client}__{purpose}.pid"


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_daemon(
    client: str,
    purpose: str = "debt",
    *,
    poll_interval: int = 30,
    review: bool = False,
    auto_bootstrap: bool = True,
) -> None:
    """Run the daemon loop — claims and processes queue items.

    This function blocks and runs in the foreground (designed to be
    spawned in a Zellij pane). It polls the queue, claims items,
    injects work into the existing session pane, and reports results.
    """
    _ensure_not_running(client, purpose)

    pid_file = _pid_path(client, purpose)
    DAEMONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    shutdown_event = threading.Event()

    def _handle_signal(_signum: int, _frame: object) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    client_config = get_client(client)
    click.echo(f"Daemon started: {client}/{purpose} (pid {os.getpid()})")
    click.echo(f"Poll interval: {poll_interval}s, Review mode: {review}")
    record_event(client, HistoryEvent(
        event_type=EventType.DAEMON_STARTED,
        client=client,
        purpose=purpose,
        metadata={"pid": str(os.getpid())},
    ))

    try:
        while not shutdown_event.is_set():
            item = claim_next(client, SessionPurpose(purpose))
            if item is None:
                shutdown_event.wait(poll_interval)
                continue

            click.echo(
                f"Processing: {item.task.description} (id: {item.id})"
            )

            try:
                before_mtime = time.time()
                _inject_into_session(
                    client_config, item, purpose,
                    auto_bootstrap=auto_bootstrap,
                )

                handoff_path = _wait_for_completion(
                    client_config.workspace_path,
                    before_mtime,
                    timeout=_DAEMON_TASK_TIMEOUT_S,
                    shutdown_event=shutdown_event,
                )

                # Re-background the session so next iteration can inject
                _rebackground_session(client_config.name, purpose)

                if handoff_path is None:
                    timeout_msg = f"Timed out after {_DAEMON_TASK_TIMEOUT_S}s"
                    fail_item(client, item.id, timeout_msg)
                    send_notification(
                        "Daemon Item Timed Out",
                        f"Queue item {item.id}: {timeout_msg}",
                        urgency="critical",
                    )
                    click.echo(f"Timed out: {item.id}")
                    continue

                reason = parse_handoff_reason(handoff_path)
                if reason:
                    click.echo(
                        f"Session ended abnormally (reason: {reason})."
                        " Pausing daemon."
                    )
                    send_notification(
                        "Daemon Paused",
                        f"Queue item {item.id}: {reason}",
                        urgency="critical",
                    )
                    fail_item(
                        client, item.id,
                        f"Session handoff: {reason}",
                    )
                    break

                complete_item(client, item.id, "Completed by daemon")
                click.echo(f"Completed: {item.id}")
            except Exception as exc:
                # Best-effort re-background on failure so session
                # is available for the next iteration.
                _rebackground_session(client_config.name, purpose)
                fail_item(client, item.id, str(exc))
                send_notification(
                    "Daemon Item Failed",
                    f"Queue item {item.id}: {exc}",
                    urgency="critical",
                )
                click.echo(f"Failed: {item.id} — {exc}")
    finally:
        if pid_file.exists():
            pid_file.unlink()
        record_event(client, HistoryEvent(
            event_type=EventType.DAEMON_STOPPED,
            client=client,
            purpose=purpose,
        ))
        click.echo("Daemon stopped.")


def start_daemon_all(
    *,
    poll_interval: int = 30,
    review: bool = False,
) -> None:
    """Run a daemon that monitors all client queues.

    Iterates over every configured client each poll cycle, claiming
    any pending item regardless of purpose.  The purpose is determined
    from the claimed item's task spec.
    """
    _ensure_not_running("_all", "_all")

    pid_file = _pid_path("_all", "_all")
    DAEMONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    shutdown_event = threading.Event()

    def _handle_signal(_signum: int, _frame: object) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    click.echo(f"Daemon started: all queues (pid {os.getpid()})")
    click.echo(f"Poll interval: {poll_interval}s, Review mode: {review}")

    try:
        while not shutdown_event.is_set():
            processed = _poll_all_queues(shutdown_event)
            if not processed:
                shutdown_event.wait(poll_interval)
    finally:
        if pid_file.exists():
            pid_file.unlink()
        click.echo("Daemon stopped.")


def _poll_all_queues(shutdown_event: threading.Event) -> bool:
    """Scan all clients for pending work, process one item if found.

    Returns ``True`` if an item was processed (success or failure),
    ``False`` if no work was available.
    """
    clients = load_clients()
    for client_name in clients:
        if shutdown_event.is_set():
            return False

        item = claim_next(client_name, purpose=None)
        if item is None:
            continue

        purpose = item.task.purpose
        client_config = get_client(client_name)

        click.echo(
            f"Processing: {client_name}/{purpose}"
            f" — {item.task.description} (id: {item.id})"
        )

        try:
            before_mtime = time.time()
            _inject_into_session(client_config, item, purpose)

            handoff_path = _wait_for_completion(
                client_config.workspace_path,
                before_mtime,
                timeout=_DAEMON_TASK_TIMEOUT_S,
                shutdown_event=shutdown_event,
            )

            _rebackground_session(client_config.name, purpose)

            if shutdown_event.is_set():
                fail_item(client_name, item.id, "Shutdown requested")
                return True

            if handoff_path is None:
                timeout_msg = f"Timed out after {_DAEMON_TASK_TIMEOUT_S}s"
                fail_item(client_name, item.id, timeout_msg)
                send_notification(
                    "Daemon Item Timed Out",
                    f"{client_name}/{purpose} {item.id}: {timeout_msg}",
                    urgency="critical",
                )
                click.echo(f"Timed out: {item.id}")
                return True

            reason = parse_handoff_reason(handoff_path)
            if reason:
                click.echo(
                    f"Session ended abnormally (reason: {reason})."
                    " Skipping item."
                )
                send_notification(
                    "Daemon Item Failed",
                    f"Queue item {item.id}: {reason}",
                    urgency="critical",
                )
                fail_item(
                    client_name, item.id,
                    f"Session handoff: {reason}",
                )
                return True

            complete_item(client_name, item.id, "Completed by daemon")
            click.echo(f"Completed: {item.id}")
        except Exception as exc:
            _rebackground_session(client_config.name, purpose)
            fail_item(client_name, item.id, str(exc))
            send_notification(
                "Daemon Item Failed",
                f"{client_name}/{purpose} {item.id}: {exc}",
                urgency="critical",
            )
            click.echo(f"Failed: {item.id} — {exc}")

        return True

    return False


# Bootstrap polling constants
_BOOTSTRAP_POLL_INTERVAL_S = 2
_BOOTSTRAP_TIMEOUT_S = 60


def _get_backgrounded_session(
    client_name: str,
    purpose: str,
    *,
    auto_bootstrap: bool = False,
) -> tuple[Session, CwState]:
    """Load state and find a BACKGROUNDED session.

    Returns ``(session, state)`` so callers can mutate and save.
    When *auto_bootstrap* is ``True`` and no session exists, starts
    a new session and polls until it reaches BACKGROUNDED status.
    Raises :class:`CwError` if no suitable session exists (or
    bootstrap times out).
    """
    state = load_state()
    session = state.find_session(client_name, purpose)

    if session is None:
        if not auto_bootstrap:
            msg = (
                f"No {purpose} session for {client_name}."
                " Run `cw start` first."
            )
            raise CwError(msg)
        # Bootstrap a new session
        click.echo(f"Bootstrapping {purpose} session for {client_name}...")
        start_session(client_name, purpose)
        session = _poll_for_backgrounded(client_name, purpose)
        state = load_state()
        return session, state

    if session.status != SessionStatus.BACKGROUNDED:
        msg = (
            f"Session {session.name} is not backgrounded"
            f" (status: {session.status}). Cannot inject."
        )
        raise CwError(msg)

    return session, state


def _poll_for_backgrounded(
    client_name: str,
    purpose: str,
) -> Session:
    """Poll state until a session becomes BACKGROUNDED or timeout."""
    elapsed = 0
    while elapsed < _BOOTSTRAP_TIMEOUT_S:
        time.sleep(_BOOTSTRAP_POLL_INTERVAL_S)
        elapsed += _BOOTSTRAP_POLL_INTERVAL_S
        state = load_state()
        session = state.find_session(client_name, purpose)
        if session is not None and session.status == SessionStatus.BACKGROUNDED:
            return session
    msg = (
        f"Timed out waiting for {client_name}/{purpose}"
        f" to reach BACKGROUNDED state ({_BOOTSTRAP_TIMEOUT_S}s)."
    )
    raise CwError(msg)


def _resume_claude_in_pane(
    session: Session,
    workflow_prompt: str,
    purpose: str,
) -> None:
    """Navigate to the session pane and inject resume + workflow prompt."""
    zellij_target = zellij.resolve_session_target(CW_SESSION)

    tab = session.zellij_tab or session.client
    zellij.go_to_tab(tab, session=zellij_target)
    zellij.focus_pane(
        session.zellij_pane or purpose,
        session=zellij_target,
        tab_name=tab,
    )
    zellij.write_to_pane(
        "claude --resume\n",
        session=zellij_target,
    )

    # Mark ACTIVE immediately after resume command is sent so that
    # a failure during workflow prompt injection leaves an accurate
    # status (Claude *is* running even if we fail to inject the prompt).
    # Callers must re-background on error.

    time.sleep(CLAUDE_INIT_DELAY_S)
    zellij.write_to_pane(workflow_prompt + "\n", session=zellij_target)


def _inject_into_session(
    client_config: ClientConfig,
    item: QueueItem,
    purpose: str,
    *,
    auto_bootstrap: bool = False,
) -> None:
    """Resume the debt session and inject a workflow prompt.

    The debt pane must already exist (created by ``cw start``) and the
    session must be BACKGROUNDED.  The daemon resumes Claude in that pane
    and injects the full workflow prompt via keystroke injection.

    When *auto_bootstrap* is ``True``, automatically starts a session if
    none exists.

    On success the session status is set to ACTIVE.  On failure after
    the resume command has been sent, the session is still marked ACTIVE
    (Claude is running) — the caller is responsible for re-backgrounding.
    """
    session, state = _get_backgrounded_session(
        client_config.name, purpose, auto_bootstrap=auto_bootstrap,
    )
    workflow_prompt = build_daemon_workflow_prompt(item.task)

    # Mark ACTIVE before Zellij IO — after the resume command is sent
    # Claude is running regardless of whether the prompt injection succeeds.
    session.status = SessionStatus.ACTIVE
    save_state(state)

    _resume_claude_in_pane(session, workflow_prompt, purpose)

    record_event(client_config.name, HistoryEvent(
        event_type=EventType.SESSION_RESUMED,
        client=client_config.name,
        session_id=session.id,
        session_name=session.name,
        purpose=purpose,
    ))


def _rebackground_session(client_name: str, purpose: str) -> None:
    """Set the session back to BACKGROUNDED so the daemon can reuse it.

    No-op if the session is not ACTIVE (e.g. already backgrounded by
    Claude running /session-done).
    """
    state = load_state()
    session = state.find_session(client_name, purpose)
    if session is None or session.status != SessionStatus.ACTIVE:
        return
    session.status = SessionStatus.BACKGROUNDED
    save_state(state)


def _wait_for_completion(
    workspace_path: Path,
    before_mtime: float,
    *,
    timeout: int = 600,
    poll_interval: int = 10,
    shutdown_event: threading.Event | None = None,
) -> Path | None:
    """Poll until a new handoff file appears, indicating session-done ran.

    Returns the most recent handoff file newer than *before_mtime*,
    or ``None`` on timeout or if *shutdown_event* is set.
    """
    start = time.time()
    while time.time() - start < timeout:
        if shutdown_event is not None and shutdown_event.is_set():
            return None
        newer = find_handoffs_newer_than(workspace_path, before_mtime)
        if newer:
            return newer[0]
        if shutdown_event is not None:
            shutdown_event.wait(poll_interval)
        else:
            time.sleep(poll_interval)
    return None


def _ensure_not_running(client: str, purpose: str) -> None:
    """Check that a daemon isn't already running for this client+purpose."""
    pid_file = _pid_path(client, purpose)
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return
    if _is_process_alive(pid):
        msg = f"Daemon already running for {client}/{purpose} (pid {pid})"
        raise CwError(msg)
    pid_file.unlink(missing_ok=True)


def stop_daemon(client: str, purpose: str = "debt") -> None:
    """Stop a running daemon by sending SIGTERM via PID file."""
    pid_file = _pid_path(client, purpose)
    if not pid_file.exists():
        msg = f"No daemon running for {client}/{purpose}."
        raise CwError(msg)
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError) as exc:
        msg = f"Invalid PID file: {pid_file}"
        raise CwError(msg) from exc

    if not _is_process_alive(pid):
        pid_file.unlink(missing_ok=True)
        click.echo(
            f"Daemon for {client}/{purpose} was not running (stale PID)."
        )
        return

    os.kill(pid, signal.SIGTERM)
    click.echo(
        f"Sent SIGTERM to daemon {client}/{purpose} (pid {pid})."
    )


def daemon_status(client: str | None = None) -> list[dict[str, object]]:
    """Check running daemons. If client is None, check all."""
    DAEMONS_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    for pid_file in sorted(DAEMONS_DIR.glob("*.pid")):
        parts = pid_file.stem.split("__", 1)
        if len(parts) != 2:
            continue
        d_client, d_purpose = parts
        if client is not None and d_client != client:
            continue
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            continue
        alive = _is_process_alive(pid)
        if not alive:
            pid_file.unlink(missing_ok=True)
        results.append({
            "client": d_client,
            "purpose": d_purpose,
            "pid": pid,
            "alive": alive,
        })
    return results
