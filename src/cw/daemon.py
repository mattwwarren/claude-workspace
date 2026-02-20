"""Background debt runner daemon for autonomous queue processing."""

from __future__ import annotations

import os
import signal
import time
from typing import TYPE_CHECKING

import click

from cw import zellij
from cw.config import DAEMONS_DIR, get_client, load_state, save_state
from cw.exceptions import CwError
from cw.handoff import (
    build_daemon_workflow_prompt,
    find_handoffs_newer_than,
    parse_handoff_reason,
)
from cw.history import EventType, HistoryEvent, record_event
from cw.models import ClientConfig, QueueItem, SessionPurpose, SessionStatus
from cw.notify import send_notification
from cw.queue import claim_next, complete_item, fail_item
from cw.session import CLAUDE_INIT_DELAY_S, CW_SESSION

if TYPE_CHECKING:
    from pathlib import Path


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
) -> None:
    """Run the daemon loop — claims and processes queue items.

    This function blocks and runs in the foreground (designed to be
    spawned in a Zellij pane). It polls the queue, claims items,
    spawns Claude in sub-panes, and reports results.
    """
    _ensure_not_running(client, purpose)

    pid_file = _pid_path(client, purpose)
    DAEMONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    shutdown_requested = False

    def _handle_signal(_signum: int, _frame: object) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True

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
        while not shutdown_requested:
            item = claim_next(client, SessionPurpose(purpose))
            if item is None:
                time.sleep(poll_interval)
                continue

            click.echo(
                f"Processing: {item.task.description} (id: {item.id})"
            )

            try:
                before_mtime = time.time()
                _inject_into_session(client_config, item, purpose)

                handoff_path = _wait_for_completion(
                    client_config.workspace_path,
                    before_mtime,
                    timeout=1800,
                )

                if handoff_path is None:
                    fail_item(client, item.id, "Timed out after 1800s")
                    click.echo(f"Timed out: {item.id}")
                    continue

                reason = parse_handoff_reason(handoff_path)
                if reason:
                    click.echo(
                        f"Session ended abnormally (reason: {reason}). Pausing daemon."
                    )
                    send_notification(
                        "Daemon Paused",
                        f"Queue item {item.id}: {reason}",
                        urgency="critical",
                    )
                    fail_item(client, item.id, f"Session handoff: {reason}")
                    break

                complete_item(client, item.id, "Completed by daemon")
                click.echo(f"Completed: {item.id}")
            except Exception as exc:
                fail_item(client, item.id, str(exc))
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


def _inject_into_session(
    client_config: ClientConfig,
    item: QueueItem,
    purpose: str,
) -> None:
    """Resume the debt session and inject a workflow prompt.

    The debt pane must already exist (created by ``cw start``) and the
    session must be BACKGROUNDED.  The daemon resumes Claude in that pane
    and injects the full workflow prompt via keystroke injection.
    """
    state = load_state()
    session = state.find_session(client_config.name, purpose)

    if session is None:
        msg = f"No {purpose} session for {client_config.name}. Run `cw start` first."
        raise CwError(msg)

    if session.status != SessionStatus.BACKGROUNDED:
        msg = (
            f"Session {session.name} is not backgrounded"
            f" (status: {session.status}). Cannot inject."
        )
        raise CwError(msg)

    workflow_prompt = build_daemon_workflow_prompt(item.task)

    # Determine Zellij target: use CW_SESSION when not inside Zellij
    zellij_target = None if zellij.in_zellij_session() else CW_SESSION

    # Navigate and resume
    zellij.go_to_tab(session.zellij_tab or session.client, session=zellij_target)
    zellij.focus_pane(session.zellij_pane or purpose, session=zellij_target)
    zellij.write_to_pane(
        f"claude --resume {session.claude_session_id}\n",
        session=zellij_target,
    )
    time.sleep(CLAUDE_INIT_DELAY_S)
    zellij.write_to_pane(workflow_prompt + "\n", session=zellij_target)

    # Update session status
    session.status = SessionStatus.ACTIVE
    save_state(state)

    record_event(client_config.name, HistoryEvent(
        event_type=EventType.SESSION_RESUMED,
        client=client_config.name,
        session_id=session.id,
        session_name=session.name,
        purpose=purpose,
    ))


def _wait_for_completion(
    workspace_path: Path,
    before_mtime: float,
    *,
    timeout: int = 600,
    poll_interval: int = 10,
) -> Path | None:
    """Poll until a new handoff file appears, indicating session-done ran."""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(poll_interval)
        newer = find_handoffs_newer_than(workspace_path, before_mtime)
        if newer:
            return newer[0]  # Most recent
    return None  # Timeout


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
