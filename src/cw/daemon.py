"""Background debt runner daemon for autonomous queue processing."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import time
from typing import TYPE_CHECKING

import click

from cw import zellij
from cw.config import DAEMONS_DIR, get_client
from cw.exceptions import CwError
from cw.handoff import build_task_prompt
from cw.history import EventType, HistoryEvent, record_event
from cw.models import ClientConfig, QueueItem, SessionPurpose
from cw.queue import claim_next, complete_item, fail_item

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
                _run_delegated_task(client_config, item, review=review)
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


def _run_delegated_task(
    client_config: ClientConfig,
    item: QueueItem,
    *,
    review: bool = False,
) -> None:
    """Spawn Claude in a sub-pane and wait for completion.

    Uses --print mode for non-interactive processing.
    """
    prompt = build_task_prompt(item.task)
    escaped = shlex.quote(prompt)
    cmd = f"claude --prompt {escaped} --print"
    pane_name = f"daemon-{item.id}"
    cwd = str(client_config.workspace_path)

    zellij.new_pane(
        cmd,
        name=pane_name,
        cwd=cwd,
        close_on_exit=True,
    )

    # Poll for pane completion
    _wait_for_pane_exit(pane_name, timeout=600)

    if review:
        click.echo(f"Review required for {item.id}. Pausing...")
        click.echo("Press Enter to continue...")
        with contextlib.suppress(EOFError):
            input()


_PANE_INIT_DELAY_S = 0.5


def _wait_for_pane_exit(pane_name: str, timeout: int = 600) -> None:
    """Poll Zellij health to detect when a pane's command exits."""
    start = time.time()
    # Give the pane a moment to appear
    time.sleep(_PANE_INIT_DELAY_S)
    while time.time() - start < timeout:
        health = zellij.check_pane_health()
        if pane_name not in health or not health[pane_name]:
            return
        time.sleep(5)
    msg = f"Pane '{pane_name}' did not exit within {timeout}s"
    raise TimeoutError(msg)


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
