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
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cw.exceptions import (
    USAGE_LIMIT_RE,
    CwError,
    DisclaimerNotAcceptedError,
    UsageLimitError,
)

_log = logging.getLogger(__name__)

# Length of the short Claude session id printed by ``claude --bg`` and
# used as the worker key in roster.json (first 8 hex chars of the UUID).
SHORT_SESSION_ID_LEN = 8
SHORT_SESSION_ID_RE = re.compile(rf"^[0-9a-f]{{{SHORT_SESSION_ID_LEN}}}$")

# Default permission mode for dispatched workers — non-interactive, so a
# permission prompt would deadlock the session. ``auto`` matches the
# behavior the issue documents.
_DEFAULT_PERMISSION_MODE = "auto"

# Conservative allowlist of model-id prefixes confirmed to support
# ``--permission-mode auto`` per the Claude Code docs
# (code.claude.com/docs/en/permission-modes): Opus 4.6+, Sonnet 4.6+/
# Sonnet 5, Opus 4.7/4.8 (gateway). worker_model is a forwarded opaque
# string that in practice carries dated/suffixed ids (e.g.
# "claude-sonnet-4-6-20251015"), so this is matched as a prefix, not an
# exact set — see #1111.
_AUTO_CAPABLE_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
)

# Permission mode for workers whose pinned model does not support
# ``auto`` (#1111) — the same non-interactive posture already-supported
# Sonnet/Opus workers run under. Requires the bypass-permissions
# disclaimer to already be accepted (`claude --dangerously-skip-permissions`
# once, interactively); an unaccepted disclaimer raises
# DisclaimerNotAcceptedError (see RealNativeDaemonClient.spawn_bg).
SKIP_PERMISSIONS_MODE = "bypassPermissions"


def model_supports_auto(worker_model: str | None) -> bool:
    """Return True if *worker_model* supports ``--permission-mode auto``.

    ``None`` (no pin) is treated as auto-capable so unpinned workers keep
    today's behavior unchanged. A non-``None`` value is matched via prefix
    against a conservative allowlist of known auto-capable model families;
    an unrecognized non-``None`` id is treated as NOT auto-capable — a hard
    ``--bg`` hang that holds a lane slot is worse than a worker running with
    fewer guardrails in an isolated, review-gated worktree. See #1111.
    """
    if not worker_model:
        return True
    normalized = worker_model.strip().lower()
    return normalized.startswith(_AUTO_CAPABLE_MODEL_PREFIXES)


def resolve_permission_mode(
    worker_model: str | None, *, explicit: str | None = None
) -> str | None:
    """Derive the effective ``--permission-mode`` for a DAEMON-origin spawn.

    Shared by both DAEMON-origin spawn chokepoints (``spawn_create_impl`` and
    ``resume_session``) so the ``model_supports_auto`` fallback rule can't
    drift between them (#1111). *explicit* — a caller-supplied
    ``permission_mode`` — always wins. Otherwise falls back to
    :data:`SKIP_PERMISSIONS_MODE` when *worker_model* does not support
    ``auto``; returns ``None`` (letting ``spawn_bg`` apply
    ``_DEFAULT_PERMISSION_MODE``) when it does.
    """
    if explicit is not None:
        return explicit
    if model_supports_auto(worker_model):
        return None
    _log.warning(
        "worker_model %r does not support --permission-mode auto; "
        "falling back to %s (#1111)",
        worker_model,
        SKIP_PERMISSIONS_MODE,
    )
    return SKIP_PERMISSIONS_MODE


# Path to the daemon's roster file. Keys under ``workers`` are short
# session ids; the daemon updates this file synchronously when a worker
# spawns or stops, so it's a reliable liveness oracle.
_ROSTER_PATH = Path.home() / ".claude" / "daemon" / "roster.json"

# Base path for per-session supervisor state files. Each background session
# has a ``<short_id>/state.json`` under this directory containing
# ``resumeSessionId`` (the full UUID the supervisor associates with the
# session). See RFC 0001 Row 8 and GitHub issue #519.
_JOBS_PATH = Path.home() / ".claude" / "jobs"

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


def _spawn_clean_env(cwd: Path) -> dict[str, str]:
    """Return os.environ with GIT_* vars stripped and PWD set to cwd.

    Prevents the spawned ``claude --bg`` worker from inheriting GIT_DIR,
    GIT_WORK_TREE, or GIT_INDEX_FILE from the orchestrator's environment.
    Without this, worker git operations are misdirected to the orchestrator's
    ``.git`` / index file — leaking commits and uncommitted changes into the
    main checkout instead of staying in the worker's worktree (#766).

    ``subprocess.run(cwd=...)`` changes the OS-level CWD but does NOT update
    the ``$PWD`` environment variable.  If ``$PWD`` still points to the
    orchestrator's main checkout, the Claude daemon uses it as the project
    root, causing git ops to commit into the main checkout instead of the
    worker's worktree (#766).  Explicitly setting ``env["PWD"] = str(cwd)``
    ensures the worker sees the worktree as its project root.

    Mirrors the identical helper in ``spawn.py:_git_clean_env`` and
    ``worktree.py:_run_git``. The duplication is intentional for now:
    importing from ``spawn`` would create a circular import
    (``spawn`` already imports ``native_daemon``). A future shared util
    (e.g. ``cw._git``) can consolidate all three.

    Also unconditionally overrides (not setdefault) ``GH_PROMPT_DISABLED``,
    ``GH_PAGER``, ``GH_NO_UPDATE_NOTIFIER``, and ``GIT_TERMINAL_PROMPT`` so a
    headless daemon worker can never inherit a value that re-enables an
    interactive prompt — a `gh`/`git` call blocking on stdin with no human to
    answer it hangs the worker indefinitely with no error and no sentinel
    (#979). Note ``GH_PROMPT=disabled`` is not a real ``gh`` env var; the
    documented knob is ``GH_PROMPT_DISABLED``. ``CI`` is deliberately excluded
    — it silently reshapes the behavior of other tools (pytest plugins, etc.)
    beyond just prompt suppression.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PWD"] = str(cwd)
    # Unconditional overrides (not setdefault) — a worker's inherited
    # environment must not be able to re-enable interactive prompts.
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_PAGER"] = "cat"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


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


def _build_spawn_argv(
    *,
    mode: str,
    extra_args: list[str] | None,
    prompt: str,
) -> list[str]:
    """Assemble the full ``claude --bg`` argv for a spawn call.

    The prompt is always appended AFTER extra_args so no preceding
    value-taking or variadic flag can consume it as a value.
    See GitHub issue #733 (``--disallowed-tools`` variadic regression).
    """
    cmd = ["claude", "--bg", "--permission-mode", mode]
    if extra_args:
        cmd.extend(extra_args)
    if prompt:
        cmd.append(prompt)
    return cmd


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
        cmd = _build_spawn_argv(mode=mode, extra_args=extra_args, prompt=prompt)
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=_spawn_clean_env(cwd),
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            msg = "claude binary not on PATH; cannot spawn background session"
            raise CwError(msg) from exc
        except subprocess.CalledProcessError as exc:
            stderr_text = (exc.stderr or exc.stdout or "").strip()
            if USAGE_LIMIT_RE.search(stderr_text):
                msg = f"claude --bg failed: usage limit active. {stderr_text}"
                raise UsageLimitError(msg) from exc
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
            if USAGE_LIMIT_RE.search(stdout_clean):
                msg = (
                    "claude --bg succeeded but usage limit detected"
                    f" in output: {proc.stdout!r}"
                )
                raise UsageLimitError(msg)
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
        self.raise_usage_limit: bool = False
        self.raise_unregistered: bool = False

    def spawn_bg(
        self,
        *,
        cwd: Path,
        prompt: str,
        extra_args: list[str] | None = None,
        permission_mode: str | None = None,
    ) -> str:
        """Record call, register a deterministic short id, return it.

        When ``raise_usage_limit`` is True, raises :class:`UsageLimitError`
        before incrementing the counter — so no slot is consumed.

        When ``raise_unregistered`` is True, returns the short id without
        adding it to the live set — simulating the intermittent flake where
        ``claude --bg`` returns a short id but the supervisor never registers
        the worker in ``roster.json`` (see GitHub issue #520).
        """
        if self.raise_usage_limit:
            msg = "fake: usage limit"
            raise UsageLimitError(msg)
        self._counter += 1
        short_id = f"{self._counter:08x}"
        self.spawn_calls.append((cwd, prompt))
        self.spawn_extra_args.append(extra_args)
        self.spawn_permission_modes.append(permission_mode)
        if not self.raise_unregistered:
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


def read_supervisor_resume_session_id(
    short_id: str, *, jobs_path: Path | None = None
) -> str | None:
    """Return the supervisor's resumeSessionId for *short_id*, or None.

    Reads ``~/.claude/jobs/<short_id>/state.json`` (or *jobs_path*) and
    extracts the ``resumeSessionId`` field. Returns ``None`` when the file
    is absent, unreadable, not valid JSON, or the key is missing — treat
    any of these as "no continuity claim from the supervisor" rather than
    an error. See RFC 0001 Row 8 and GitHub issue #519.
    """
    base = jobs_path if jobs_path is not None else _JOBS_PATH
    state_path = base / short_id / "state.json"
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("resumeSessionId")
    return value if isinstance(value, str) else None
