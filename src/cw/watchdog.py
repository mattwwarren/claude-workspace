"""Mainstream watchdog: one-shot detect+notify tick + install/status (#1015).

RFC 0008 capstone deliverable 3. ``cw watchdog tick`` is meant to run
independently of the dispatch loop (via a per-user systemd timer on Linux or
a launchd agent on macOS) so an operator gets paged even when the daemon
dispatch loop itself has stalled or was never started — the loop being dead
is exactly one of the conditions this module exists to detect.

``run_tick`` performs two checks, in order:

1. **Parked-row ages** — calls :func:`cw.reconcile.escalation.run_escalation_sweep`
   directly (it is explicitly standalone-callable, no ``sessions_lock``
   needed) and fires a desktop notification for every ticket newly escalated
   this call.
2. **Dispatch-loop liveness** — a dead-man's-switch: how long since the last
   ``dispatch.tick`` event was recorded on the orchestrator event bus. No
   evidence (zero ``dispatch.tick`` events ever) is treated as "nothing to
   alarm on" rather than "dead" — a machine that has simply never run
   ``cw dev-queue dispatch`` is not a failure.
(A third check — park-marker cycling — was removed along with the
process-kill timeouts: nothing increments ``consecutive_salvage_skips``
anymore, so the counter it watched can no longer move.)

Every detection appends one line to ``watchdog.log`` (state_dir) and fires
:func:`cw.notify.send_desktop_notification` — log-on-detection only, never
once per tick, so a healthy system produces an empty log.

``install``/``uninstall``/``status`` are pure functions that read/write the
per-user, no-root unit-file locations (Q6): systemd user timer
(``$XDG_CONFIG_HOME/systemd/user/cw-watchdog.{service,timer}``, falling back
to ``~/.config`` when unset) on Linux, a launchd agent
(``~/Library/LaunchAgents/com.cw.watchdog.plist``) on macOS. They only
read/write files — activating the unit (``systemctl --user daemon-reload
--now`` / ``launchctl load``) is left to the operator, printed by the CLI
layer, since no other module in this codebase shells out to
systemctl/launchctl and a bare file write keeps this module deterministic
and side-effect-free to test.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cw.config import load_orchestrator_config, state_dir
from cw.events import read_events
from cw.exceptions import CwError
from cw.models import OrchestratorEventType
from cw.notify import send_desktop_notification
from cw.reconcile.escalation import ESCALATION_PARK_MINUTES, run_escalation_sweep

if TYPE_CHECKING:
    from cw.models import OrchestratorConfig

# Dead-man's-switch thresholds for the dispatch-loop-liveness check. The
# larger of (a fixed floor) and (a multiple of the configured tick interval)
# — a slow-but-alive loop with a long tick_interval_seconds must not trip a
# false alarm just because it's naturally slower than the floor.
_DISPATCH_LIVENESS_MIN_THRESHOLD_SECONDS = 600
_DISPATCH_LIVENESS_TICK_MULTIPLIER = 4

_WATCHDOG_LOG_FILENAME = "watchdog.log"

# Per Q6: a systemd user timer firing every 15 minutes / a launchd
# StartInterval of 900 seconds.
_WATCHDOG_TICK_INTERVAL_SECONDS = 900

_CW_COMMAND_NAME = "cw"


@dataclass(frozen=True)
class WatchdogTickResult:
    """Outcome of one :func:`run_tick` call — used by tests and the CLI."""

    escalated_ticket_ids: list[str] = field(default_factory=list)
    dispatch_loop_dead: bool = False


@dataclass(frozen=True)
class WatchdogStatus:
    """Outcome of :func:`status` — used by tests and the CLI."""

    platform: str
    installed: bool
    paths: list[str]


def _log_line(now: datetime, check: str, ticket_id: str | None, message: str) -> str:
    """Serialize one detection as a single JSON line for watchdog.log."""
    return json.dumps(
        {
            "ts": now.isoformat(),
            "check": check,
            "ticket_id": ticket_id,
            "message": message,
        }
    )


def _append_watchdog_log(lines: list[str]) -> None:
    """Append *lines* to watchdog.log. Called only when there is something to log."""
    path = state_dir() / _WATCHDOG_LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for line in lines:
            f.write(line + "\n")


def _check_dispatch_loop_liveness(
    now: datetime, config: OrchestratorConfig
) -> str | None:
    """Return a human-readable alarm message, or None if nothing to report.

    No ``dispatch.tick`` events at all is "no evidence" (a machine that never
    ran the dispatch loop), not "dead" — returns None rather than alarming.
    """
    events = read_events(event_types=[OrchestratorEventType.DISPATCH_TICK])
    if not events:
        return None
    newest = events[-1]
    age_seconds = (now - newest.created_at).total_seconds()
    threshold_seconds = max(
        config.tick_interval_seconds * _DISPATCH_LIVENESS_TICK_MULTIPLIER,
        _DISPATCH_LIVENESS_MIN_THRESHOLD_SECONDS,
    )
    if age_seconds < threshold_seconds:
        return None
    return (
        f"No dispatch.tick event in {age_seconds / 60:.1f}m"
        f" (threshold {threshold_seconds / 60:.1f}m)"
        " — the dispatch loop may be down."
    )


def run_tick(
    *,
    now: datetime | None = None,
    config: OrchestratorConfig | None = None,
) -> WatchdogTickResult:
    """Run the one-shot detect+notify tick. Safe to call standalone (no locks).

    Log-on-detection only: ``watchdog.log`` gets zero new lines on a
    healthy tick.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    resolved_config = config if config is not None else load_orchestrator_config()
    log_lines: list[str] = []

    escalated = run_escalation_sweep(now=resolved_now)
    for ticket_id in escalated:
        message = (
            f"{ticket_id} has been parked past {ESCALATION_PARK_MINUTES} minutes"
            " without operator action."
        )
        send_desktop_notification("cw watchdog: gate escalated", message)
        log_lines.append(_log_line(resolved_now, "escalation", ticket_id, message))

    dispatch_dead_message = _check_dispatch_loop_liveness(resolved_now, resolved_config)
    if dispatch_dead_message is not None:
        send_desktop_notification(
            "cw watchdog: dispatch loop unresponsive", dispatch_dead_message
        )
        log_lines.append(
            _log_line(resolved_now, "dispatch_liveness", None, dispatch_dead_message)
        )

    if log_lines:
        _append_watchdog_log(log_lines)

    return WatchdogTickResult(
        escalated_ticket_ids=escalated,
        dispatch_loop_dead=dispatch_dead_message is not None,
    )


# ---------------------------------------------------------------------------
# install / uninstall / status — per-user, no-root unit-file management (Q6)
# ---------------------------------------------------------------------------


def _xdg_config_home() -> Path:
    """Return $XDG_CONFIG_HOME, falling back to ~/.config when unset."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(xdg) if xdg else Path.home() / ".config"


def systemd_service_path() -> Path:
    return _xdg_config_home() / "systemd" / "user" / "cw-watchdog.service"


def systemd_timer_path() -> Path:
    return _xdg_config_home() / "systemd" / "user" / "cw-watchdog.timer"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.cw.watchdog.plist"


def generate_systemd_service_text(cw_path: str) -> str:
    return (
        "[Unit]\n"
        "Description=cw watchdog — mechanical recovery + escalation"
        " dead-man's switch\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={cw_path} watchdog tick\n"
    )


def generate_systemd_timer_text() -> str:
    minutes = _WATCHDOG_TICK_INTERVAL_SECONDS // 60
    return (
        "[Unit]\n"
        f"Description=cw watchdog timer — runs every {minutes} minutes\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        f"OnUnitActiveSec={minutes}min\n"
        "Unit=cw-watchdog.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def generate_launchd_plist_text(cw_path: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        "    <string>com.cw.watchdog</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{cw_path}</string>\n"
        "        <string>watchdog</string>\n"
        "        <string>tick</string>\n"
        "    </array>\n"
        "    <key>StartInterval</key>\n"
        f"    <integer>{_WATCHDOG_TICK_INTERVAL_SECONDS}</integer>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _resolve_cw_executable_path() -> str:
    """Absolute path of the running ``cw`` executable, for the unit's ExecStart.

    systemd user services / launchd agents do not inherit the login-shell PATH,
    so a bare ``cw`` fails 203/EXEC (#1027). Resolution order: (1) prefer
    ``Path(sys.argv[0]).resolve()`` when it points at an existing file named
    ``cw``; (2) otherwise fall back to ``shutil.which(_CW_COMMAND_NAME)``. This
    covers both cases where argv[0] isn't usable as-is — a relative or
    non-``cw``-named argv[0] (e.g. a ``python -m`` invocation) falls straight
    through to the ``which()`` lookup. ``sys.executable`` is NOT used — under a
    uv tool install it is the venv interpreter, not the ``cw`` shim. An
    ``Environment=PATH=...`` line in the unit file was considered and
    rejected — it has no equivalent on launchd's ``ProgramArguments`` (no
    PATH-injection mechanism there), so absolute-path resolution is simpler and
    gives Linux/macOS parity.
    """
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.is_file() and argv0.name == _CW_COMMAND_NAME:
        return str(argv0)
    found = shutil.which(_CW_COMMAND_NAME)
    if found is not None:
        return str(Path(found).resolve())
    msg = (
        f"Cannot resolve the absolute path of the {_CW_COMMAND_NAME!r} executable"
        f" (argv[0]={sys.argv[0]!r}, not on PATH). Reinstall with"
        f" 'uv tool install' or ensure {_CW_COMMAND_NAME!r} is on PATH."
    )
    raise CwError(msg)


def install() -> list[Path]:
    """Write the platform-appropriate unit file(s); return the paths written.

    Does not invoke ``systemctl``/``launchctl`` — the CLI layer prints the
    activation command for the operator to run themselves.
    """
    cw_path = _resolve_cw_executable_path()
    if _is_macos():
        path = launchd_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generate_launchd_plist_text(cw_path))
        return [path]
    service_path = systemd_service_path()
    timer_path = systemd_timer_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(generate_systemd_service_text(cw_path))
    timer_path.write_text(generate_systemd_timer_text())
    return [service_path, timer_path]


def uninstall() -> list[Path]:
    """Remove the platform-appropriate unit file(s); return the paths removed."""
    if _is_macos():
        path = launchd_plist_path()
        if not path.exists():
            return []
        path.unlink()
        return [path]
    removed: list[Path] = []
    for path in (systemd_service_path(), systemd_timer_path()):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def status() -> WatchdogStatus:
    """Return install status: platform, whether the unit file(s) exist, paths."""
    if _is_macos():
        path = launchd_plist_path()
        return WatchdogStatus(
            platform="darwin", installed=path.exists(), paths=[str(path)]
        )
    service_path = systemd_service_path()
    timer_path = systemd_timer_path()
    return WatchdogStatus(
        platform="linux",
        installed=service_path.exists() and timer_path.exists(),
        paths=[str(service_path), str(timer_path)],
    )
