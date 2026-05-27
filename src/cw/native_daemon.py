"""Native Claude daemon client for daemon-origin worker spawn.

Dispatched workers no longer run inside tmux/cmux panes; cw spawns them
directly into the Claude background daemon via ``claude --bg``. The
:class:`NativeDaemonClient` protocol abstracts that call surface so the
spawn and reconcile paths can be exercised in tests without shelling out.

Sessions are identified by the **short** Claude session id — the 8-char
hex prefix Claude prints on stdout (``backgrounded · <short>``) and uses
as the key in ``~/.claude/daemon/roster.json``. Storing the short id as
``Session.surface_ref`` lets reconcile compare against the roster in O(1)
and lets ``claude stop`` accept the same reference directly.

See GitHub issue #150 for the migration rationale.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cw.exceptions import CwError, DisclaimerNotAcceptedError

_log = logging.getLogger(__name__)

# Length of the short Claude session id printed by ``claude --bg`` and
# used as the worker key in roster.json (first 8 hex chars of the UUID).
SHORT_SESSION_ID_LEN = 8
SHORT_SESSION_ID_RE = re.compile(rf"^[0-9a-f]{{{SHORT_SESSION_ID_LEN}}}$")

# Default permission mode for dispatched workers — non-interactive, so a
# permission prompt would deadlock the session. ``auto`` matches the
# behavior the issue documents.
_DEFAULT_PERMISSION_MODE = "auto"

# Path to the daemon's roster file. Keys under ``workers`` are short
# session ids; the daemon updates this file synchronously when a worker
# spawns or stops, so it's a reliable liveness oracle.
_ROSTER_PATH = Path.home() / ".claude" / "daemon" / "roster.json"

# Regex that matches the short session id Claude prints on a successful
# ``claude --bg`` invocation: ``backgrounded · <8 hex chars>``.
_BG_STDOUT_PATTERN = re.compile(
    rf"backgrounded\s*·\s*([0-9a-f]{{{SHORT_SESSION_ID_LEN}}})"
)

# Claude Code 2.1.150 wraps the short id in ANSI color escapes
# (``backgrounded · \x1b[36m<id>\x1b[39m``). Strip CSI SGR sequences before
# matching so the parser tolerates terminal-formatted output.
_ANSI_CSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

# Substring verified against claude binary 2.1.150 via ``strings``; full stderr is:
# "--bg with bypassPermissions requires accepting the disclaimer first.
#  Run `claude --dangerously-skip-permissions` once interactively."
_DISCLAIMER_REJECTION_PATTERN = "requires accepting the disclaimer first"


@runtime_checkable
class NativeDaemonClient(Protocol):
    """Protocol for interacting with the Claude background daemon."""

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        """Spawn a backgrounded Claude session in *cwd* running *prompt*.

        *extra_args* are inserted before the prompt in the ``claude --bg``
        invocation — use to pass ``--resume <uuid>`` for transcript-resume
        or ``--append-system-prompt <text>`` for system-prompt injection.
        An empty or falsy *prompt* is omitted from the command so the
        session starts idle (tempo=blocked), ready for the user's first
        message.

        *permission_mode* overrides ``_DEFAULT_PERMISSION_MODE`` for the
        spawned session. Pass ``None`` to use the default (``"auto"``).

        Returns the 8-char short session id. Raises :class:`CwError` on
        spawn failure or unparseable stdout.
        """
        ...

    def list_live_session_short_ids(self) -> set[str]:
        """Return the set of short session ids the daemon considers live.

        Reads ``roster.json``; an unreadable or malformed roster yields an
        empty set so the caller can fall back to outage-safe behavior.
        """
        ...

    def stop(self, short_id: str) -> None:
        """Stop a backgrounded Claude session. Best-effort, no raise."""
        ...


class RealNativeDaemonClient:
    """Real client that drives the ``claude`` CLI via subprocess.

    All three operations are intentionally simple shell-outs: the native
    daemon already provides synchronous roster updates, so we don't need
    a long-lived connection or JSON-RPC channel here.
    """

    def __init__(self, *, roster_path: Path | None = None) -> None:
        self._roster_path: Path = roster_path or _ROSTER_PATH

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        """Invoke ``claude --bg --permission-mode <mode>`` and parse short id.

        *extra_args* are inserted after ``--permission-mode <mode>`` and
        before *prompt*. An empty *prompt* is omitted so the session starts
        idle (tempo=blocked), ready for the user's first message.

        *permission_mode* overrides the default (``"auto"``). Pass ``None``
        to use the default.
        """
        mode = _DEFAULT_PERMISSION_MODE if permission_mode is None else permission_mode
        cmd = ["claude", "--bg", "--permission-mode", mode]
        if extra_args:
            cmd.extend(extra_args)
        if prompt:
            cmd.append(prompt)
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            msg = "claude binary not on PATH; cannot spawn background session"
            raise CwError(msg) from exc
        except subprocess.CalledProcessError as exc:
            stderr_text = (exc.stderr or exc.stdout or "").strip()
            if _DISCLAIMER_REJECTION_PATTERN in stderr_text:
                msg = (
                    "claude --bg failed: bypassPermissions disclaimer not accepted."
                    " To fix, run `claude --dangerously-skip-permissions`"
                    " once interactively."
                )
                raise DisclaimerNotAcceptedError(msg) from exc
            msg = (
                f"claude --bg exited {exc.returncode}: "
                f"{(exc.stderr or exc.stdout or '').strip()}"
            )
            raise CwError(msg) from exc

        stdout_clean = _ANSI_CSI_PATTERN.sub("", proc.stdout or "")
        match = _BG_STDOUT_PATTERN.search(stdout_clean)
        if match is None:
            msg = (
                "claude --bg succeeded but stdout did not contain a "
                f"recognizable session id: {proc.stdout!r}"
            )
            raise CwError(msg)
        return match.group(1)

    def list_live_session_short_ids(self) -> set[str]:
        """Read ``roster.json`` and return the set of worker short ids."""
        try:
            raw = self._roster_path.read_text(encoding="utf-8")
        except OSError:
            return set()
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            _log.warning(
                "native daemon roster at %s is not valid JSON", self._roster_path
            )
            return set()
        workers = data.get("workers")
        if not isinstance(workers, dict):
            return set()
        return {key for key in workers if isinstance(key, str)}

    def stop(self, short_id: str) -> None:
        """Run ``claude stop <short_id>`` swallowing failures.

        Cleanup is best-effort — a missing or already-stopped worker is
        not actionable, and we don't want signal-stop's hook fire to bubble
        an error back into Claude. Failures are logged at WARNING.
        """
        try:
            subprocess.run(
                ["claude", "stop", short_id],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _log.warning("native daemon: claude stop %s timed out or missing", short_id)


class FakeNativeDaemonClient:
    """In-memory adapter for tests. Records all calls; no real I/O."""

    def __init__(self) -> None:
        self._counter = 0
        self.spawn_calls: list[tuple[Path, str]] = []
        self.spawn_extra_args: list[list[str] | None] = []
        self.spawn_permission_modes: list[str | None] = []
        self.stop_calls: list[str] = []
        self._live: set[str] = set()

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        """Record call, register a deterministic short id, return it."""
        self._counter += 1
        short_id = f"{self._counter:08x}"
        self.spawn_calls.append((cwd, prompt))
        self.spawn_extra_args.append(extra_args)
        self.spawn_permission_modes.append(permission_mode)
        self._live.add(short_id)
        return short_id

    def list_live_session_short_ids(self) -> set[str]:
        """Return a copy of the in-memory live set."""
        return set(self._live)

    def stop(self, short_id: str) -> None:
        """Record call and drop from live set (idempotent)."""
        self.stop_calls.append(short_id)
        self._live.discard(short_id)


def get_native_daemon_client() -> NativeDaemonClient:
    """Return the active native-daemon client.

    Always returns a :class:`RealNativeDaemonClient` in production. Tests
    inject :class:`FakeNativeDaemonClient` directly via the parameter on
    spawn/reconcile entry points.
    """
    return RealNativeDaemonClient()
