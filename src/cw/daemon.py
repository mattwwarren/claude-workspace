"""Background debt runner daemon for autonomous queue processing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import threading
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import click

from cw import zellij
from cw.config import (
    DAEMONS_DIR,
    EVENTS_DIR,
    get_client,
    load_clients,
    load_state,
    save_state,
)
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
from cw.wrapper import _idle_signal_path, _trigger_path

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
    click.echo(f"Poll interval: {poll_interval}s")
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
) -> None:
    """Run a daemon that monitors all client queues.

    Items for different (client, purpose) pairs are processed concurrently
    via asyncio tasks, so debt and impl sessions can work in parallel.
    """
    asyncio.run(_start_daemon_all_async(poll_interval=poll_interval))


async def _start_daemon_all_async(
    *,
    poll_interval: int = 30,
) -> None:
    """Async main loop for the all-queues daemon."""
    _ensure_not_running("_all", "_all")

    pid_file = _pid_path("_all", "_all")
    DAEMONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    click.echo(f"Daemon started: all queues (pid {os.getpid()})")
    click.echo(f"Poll interval: {poll_interval}s")

    # Track one task per (client, purpose) pair.
    active_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    try:
        while not shutdown_event.is_set():
            _reap_done_tasks(active_tasks)
            _spawn_new_tasks(active_tasks, shutdown_event)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=poll_interval,
                )
    finally:
        # Cancel remaining tasks and wait for them to finish.
        for task in active_tasks.values():
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks.values(), return_exceptions=True)
        if pid_file.exists():
            pid_file.unlink()
        click.echo("Daemon stopped.")


def _reap_done_tasks(
    active_tasks: dict[tuple[str, str], asyncio.Task[None]],
) -> None:
    """Remove completed tasks and log any unexpected exceptions."""
    done_keys = [k for k, t in active_tasks.items() if t.done()]
    for key in done_keys:
        task = active_tasks.pop(key)
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            click.echo(f"Task {key} crashed: {exc}")


def _spawn_new_tasks(
    active_tasks: dict[tuple[str, str], asyncio.Task[None]],
    shutdown_event: asyncio.Event,
) -> None:
    """Claim pending items and spawn async tasks for new purposes."""
    clients = load_clients()
    for client_name in clients:
        if shutdown_event.is_set():
            return

        item = claim_next(client_name, purpose=None)
        if item is None:
            continue

        purpose = item.task.purpose
        key = (client_name, purpose)

        if key in active_tasks:
            # Purpose already has an active task — don't double-inject.
            # Put the item back by failing it so it can be retried.
            fail_item(client_name, item.id, "Purpose busy, will retry")
            click.echo(
                f"Skipped: {client_name}/{purpose}"
                f" — already processing (id: {item.id})"
            )
            continue

        client_config = get_client(client_name)
        click.echo(
            f"Processing: {client_name}/{purpose}"
            f" — {item.task.description} (id: {item.id})"
        )
        active_tasks[key] = asyncio.create_task(
            _async_process_item(
                client_config, item, purpose, shutdown_event,
            ),
            name=f"daemon-{client_name}-{purpose}",
        )


async def _async_process_item(
    client_config: ClientConfig,
    item: QueueItem,
    purpose: str,
    shutdown_event: asyncio.Event,
) -> None:
    """Process a single queue item asynchronously.

    Injects the prompt into the session, then waits for completion.
    For IDLE sessions (wrapper-managed), waits for the idle signal file.
    For BACKGROUNDED sessions (legacy), polls for handoff files.
    """
    client_name = client_config.name

    # Check session status before injection to choose completion strategy
    state = load_state()
    session = state.find_session(client_name, purpose)
    use_idle_events = session is not None and session.status == SessionStatus.IDLE

    try:
        before_mtime = time.time()
        _inject_into_session(
            client_config, item, purpose, auto_bootstrap=True,
        )

        if use_idle_events:
            # Event-driven: wait for wrapper to signal IDLE
            idle_payload = await _wait_for_idle_event(
                client_name, purpose,
                timeout=_DAEMON_TASK_TIMEOUT_S,
                shutdown_event=shutdown_event,
            )

            if shutdown_event.is_set():
                fail_item(client_name, item.id, "Shutdown requested")
                return

            if idle_payload is None:
                timeout_msg = f"Timed out after {_DAEMON_TASK_TIMEOUT_S}s"
                fail_item(client_name, item.id, timeout_msg)
                send_notification(
                    "Daemon Item Timed Out",
                    f"{client_name}/{purpose} {item.id}: {timeout_msg}",
                    urgency="critical",
                )
                click.echo(f"Timed out: {item.id}")
                return

            # Wrapper already transitioned to IDLE — no rebackground needed
            exit_code = idle_payload.get("exit_code", 0)
            if exit_code != 0:
                fail_msg = f"Claude exited with code {exit_code}"
                fail_item(client_name, item.id, fail_msg)
                click.echo(f"Failed: {item.id} — {fail_msg}")
                return

            complete_item(client_name, item.id, "Completed by daemon")
            click.echo(f"Completed: {item.id}")
        else:
            # Legacy: poll for handoff files
            handoff_path = await _async_wait_for_completion(
                client_config.workspace_path,
                before_mtime,
                timeout=_DAEMON_TASK_TIMEOUT_S,
                shutdown_event=shutdown_event,
            )

            _rebackground_session(client_name, purpose)

            if shutdown_event.is_set():
                fail_item(client_name, item.id, "Shutdown requested")
                return

            if handoff_path is None:
                timeout_msg = f"Timed out after {_DAEMON_TASK_TIMEOUT_S}s"
                fail_item(client_name, item.id, timeout_msg)
                send_notification(
                    "Daemon Item Timed Out",
                    f"{client_name}/{purpose} {item.id}: {timeout_msg}",
                    urgency="critical",
                )
                click.echo(f"Timed out: {item.id}")
                return

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
                return

            complete_item(client_name, item.id, "Completed by daemon")
            click.echo(f"Completed: {item.id}")
    except Exception as exc:
        if not use_idle_events:
            _rebackground_session(client_name, purpose)
        fail_item(client_name, item.id, str(exc))
        send_notification(
            "Daemon Item Failed",
            f"{client_name}/{purpose} {item.id}: {exc}",
            urgency="critical",
        )
        click.echo(f"Failed: {item.id} — {exc}")


async def _async_wait_for_completion(
    workspace_path: Path,
    before_mtime: float,
    *,
    timeout: int = 600,
    poll_interval: int = 10,
    shutdown_event: asyncio.Event | None = None,
) -> Path | None:
    """Async version of :func:`_wait_for_completion`.

    Uses ``asyncio.sleep`` so other tasks can run while polling.
    """
    start = time.time()
    while time.time() - start < timeout:
        if shutdown_event is not None and shutdown_event.is_set():
            return None
        newer = find_handoffs_newer_than(workspace_path, before_mtime)
        if newer:
            return newer[0]
        await asyncio.sleep(poll_interval)
    return None


# Bootstrap polling constants
_BOOTSTRAP_POLL_INTERVAL_S = 2
_BOOTSTRAP_TIMEOUT_S = 60


def _get_injectable_session(
    client_name: str,
    purpose: str,
    *,
    auto_bootstrap: bool = False,
) -> tuple[Session, CwState]:
    """Load state and find an IDLE or BACKGROUNDED session.

    Returns ``(session, state)`` so callers can mutate and save.
    When *auto_bootstrap* is ``True`` and no session exists, starts
    a new session and polls until it reaches an injectable status.
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
        session = _poll_for_injectable(client_name, purpose)
        state = load_state()
        return session, state

    if session.status not in (SessionStatus.IDLE, SessionStatus.BACKGROUNDED):
        msg = (
            f"Session {session.name} is not injectable"
            f" (status: {session.status}). Need IDLE or BACKGROUNDED."
        )
        raise CwError(msg)

    return session, state


def _poll_for_injectable(
    client_name: str,
    purpose: str,
) -> Session:
    """Poll state until a session becomes IDLE or BACKGROUNDED, or timeout."""
    elapsed = 0
    while elapsed < _BOOTSTRAP_TIMEOUT_S:
        time.sleep(_BOOTSTRAP_POLL_INTERVAL_S)
        elapsed += _BOOTSTRAP_POLL_INTERVAL_S
        state = load_state()
        session = state.find_session(client_name, purpose)
        if session is not None and session.status in (
            SessionStatus.IDLE,
            SessionStatus.BACKGROUNDED,
        ):
            return session
    msg = (
        f"Timed out waiting for {client_name}/{purpose}"
        f" to reach injectable state ({_BOOTSTRAP_TIMEOUT_S}s)."
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


def _inject_via_trigger(
    client: str,
    purpose: str,
    workflow_prompt: str,
    *,
    session: Session | None = None,
) -> None:
    """Write a trigger file so the wrapper launches Claude with the workflow.

    The wrapper loop picks up the trigger and runs
    ``claude --session-id <uuid> --append-system-prompt <prompt>`` — a fresh
    context with a known session ID.  When *session* is provided, the
    generated UUID is stored on the session object (caller must persist).
    """
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    trigger = _trigger_path(client, purpose)
    new_id = str(uuid4())
    claude_args = [
        "--session-id", new_id,
        "--append-system-prompt", workflow_prompt,
    ]
    if session is not None:
        session.claude_session_id = new_id
    payload = {
        "claude_args": claude_args,
    }
    trigger.write_text(json.dumps(payload))


async def _wait_for_idle_event(
    client: str,
    purpose: str,
    *,
    timeout: int = _DAEMON_TASK_TIMEOUT_S,
    poll_interval: float = 2.0,
    shutdown_event: asyncio.Event | None = None,
) -> dict[str, object] | None:
    """Poll for an idle signal file from the wrapper.

    Returns the parsed JSON payload on success, or ``None`` on timeout
    or shutdown.  Consumes (deletes) the signal file after reading.
    """
    signal_file = _idle_signal_path(client, purpose)
    start = time.time()
    while time.time() - start < timeout:
        if shutdown_event is not None and shutdown_event.is_set():
            return None
        if signal_file.exists():
            try:
                data: dict[str, object] = json.loads(signal_file.read_text())
                signal_file.unlink(missing_ok=True)
                return data
            except (json.JSONDecodeError, OSError):
                signal_file.unlink(missing_ok=True)
                return {}
        await asyncio.sleep(poll_interval)
    return None


def _inject_into_session(
    client_config: ClientConfig,
    item: QueueItem,
    purpose: str,
    *,
    auto_bootstrap: bool = False,
) -> None:
    """Inject a workflow prompt into the session.

    Supports two injection paths:

    - **IDLE** (wrapper running): writes a trigger file so the wrapper
      launches a fresh ``claude --append-system-prompt <prompt>``.
    - **BACKGROUNDED** (no wrapper): falls back to keystroke injection
      via ``_resume_claude_in_pane``.

    When *auto_bootstrap* is ``True``, automatically starts a session if
    none exists.

    On success the session status is set to ACTIVE.
    """
    session, state = _get_injectable_session(
        client_config.name, purpose, auto_bootstrap=auto_bootstrap,
    )
    workflow_prompt = build_daemon_workflow_prompt(item.task)

    if session.status == SessionStatus.IDLE:
        # Write trigger for the wrapper loop — it will launch Claude
        _inject_via_trigger(
            client_config.name, purpose, workflow_prompt,
            session=session,
        )
        session.status = SessionStatus.ACTIVE
        save_state(state)
    else:
        # Legacy keystroke injection for BACKGROUNDED sessions
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
