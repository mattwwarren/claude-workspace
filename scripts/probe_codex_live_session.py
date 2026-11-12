#!/usr/bin/env python3
"""Opt-in operator smoke for Codex CLI create and resume (RFC 0018 D1, #3064, #2463).

Runs one create and one resume of a persistent Codex thread inside a disposable
Git repository. This does not validate cw queue or executor integration, and it
never runs in default CI.

Opt-in: set ``CW_CODEX_LIVE_SESSION_SMOKE=1``. Any other value (including unset)
prints the opt-out object and exits 0 without launching anything.

Usage::

    CW_CODEX_LIVE_SESSION_SMOKE=1 python scripts/probe_codex_live_session.py \
        --model MODEL

Contract (#2463 "Result contract v2"): exactly one compact JSON object on stdout,
nothing on stderr, and exit 0 for skipped or passed, 1 for failed. The keys are
``status``, ``cli_version``, ``model``, ``session_id``, ``create``, ``resume`` and
``error_code``.

Disclosure: create leaves one normal persistent thread in ``CODEX_HOME``. Only
its printed id is reported; an operator can inspect or delete it. The temporary
repository is removed unconditionally.

Limits: the 120-second timeout applies to each direct child process only. This is
not a supervisor, a security boundary or a race-proof sandbox; it is a
trusted-local-user opt-in tool. Stdlib only, with no ``cw`` import.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

OPT_IN_ENV: Final = "CW_CODEX_LIVE_SESSION_SMOKE"
TMP_PARENT_ENV: Final = "CW_LIVE_TEST_TMPDIR"
TIMEOUT_SECONDS: Final = 120
VERSION_MAX_LENGTH: Final = 64
MODEL_MAX_LENGTH: Final = 64
_MODEL_ARGC: Final = 2  # exactly "--model" and its VALUE
TURN_COMPLETED: Final = "turn.completed"
TURN_FAILED: Final = "turn.failed"
TERMINAL_EVENTS: Final = frozenset({TURN_COMPLETED, TURN_FAILED})
NONTERMINAL_TURN_EVENTS: Final = frozenset({"turn.started"})
CREATE_PROMPT: Final = (
    "Reply exactly cw-session-smoke-ok. Do not use tools or modify files."
)
RESUME_PROMPT: Final = (
    "Reply exactly cw-session-smoke-resumed-ok. Do not use tools or modify files."
)
_CHECKOUT_ROOT: Final = Path(__file__).resolve().parents[1]
_VERSION_RE: Final = re.compile(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+")
_MODEL_RE: Final = re.compile(rf"[A-Za-z0-9._-]{{1,{MODEL_MAX_LENGTH}}}")
_SESSION_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
STATUS_SKIPPED: Final = "skipped"
STATUS_PASSED: Final = "passed"
STATUS_FAILED: Final = "failed"


class ErrorCode(StrEnum):
    """The closed ``error_code`` enum of Result contract v2."""

    OPT_IN_REQUIRED = "opt_in_required"
    INVALID_MODEL = "invalid_model"
    VERSION_UNAVAILABLE = "version_unavailable"
    VERSION_INVALID = "version_invalid"
    CLI_UNAVAILABLE = "cli_unavailable"
    REPO_SETUP_FAILED = "repo_setup_failed"
    INTERNAL_ERROR = "internal_error"
    CREATE_TIMEOUT = "create_timeout"
    CREATE_NONZERO_EXIT = "create_nonzero_exit"
    CREATE_MALFORMED_JSONL = "create_malformed_jsonl"
    CREATE_MISSING_THREAD = "create_missing_thread"
    CREATE_INVALID_THREAD_ID = "create_invalid_thread_id"
    CREATE_DUPLICATE_THREAD_STARTED = "create_duplicate_thread_started"
    CREATE_INVALID_TERMINAL = "create_invalid_terminal"
    RESUME_TIMEOUT = "resume_timeout"
    RESUME_NONZERO_EXIT = "resume_nonzero_exit"
    RESUME_MALFORMED_JSONL = "resume_malformed_jsonl"
    RESUME_MISSING_THREAD = "resume_missing_thread"
    RESUME_INVALID_THREAD_ID = "resume_invalid_thread_id"
    RESUME_DUPLICATE_THREAD_STARTED = "resume_duplicate_thread_started"
    RESUME_ID_MISMATCH = "resume_id_mismatch"
    RESUME_INVALID_TERMINAL = "resume_invalid_terminal"
    CLEANUP_FAILED = "cleanup_failed"


class ParseFault(StrEnum):
    """A stream fault. The value is the suffix appended to the phase label."""

    MALFORMED = "malformed_jsonl"
    MISSING_THREAD = "missing_thread"
    INVALID_THREAD_ID = "invalid_thread_id"
    DUPLICATE_THREAD_STARTED = "duplicate_thread_started"
    INVALID_TERMINAL = "invalid_terminal"


@dataclass(frozen=True)
class Parsed:
    thread_id: str | None
    terminal: str | None
    fault: ParseFault | None


@dataclass
class _StreamState:
    """Accumulates one JSONL stream; ``feed`` returns the first fault seen."""

    thread_id: str | None = None
    terminal: str | None = None

    def feed(self, event: dict[str, object]) -> ParseFault | None:
        kind = event["type"]
        if kind == "thread.started":
            return self._thread_started(event)
        if isinstance(kind, str) and kind in NONTERMINAL_TURN_EVENTS:
            return None
        if isinstance(kind, str) and kind in TERMINAL_EVENTS:
            return self._terminal(kind)
        if isinstance(kind, str) and kind.startswith("turn."):
            return self._terminal(kind)
        return None

    def _thread_started(self, event: dict[str, object]) -> ParseFault | None:
        if self.terminal is not None:
            return ParseFault.INVALID_TERMINAL
        if self.thread_id is not None:
            return ParseFault.DUPLICATE_THREAD_STARTED
        candidate = event.get("thread_id")
        if not isinstance(candidate, str) or not _SESSION_ID_RE.fullmatch(candidate):
            return ParseFault.INVALID_THREAD_ID
        self.thread_id = candidate
        return None

    def _terminal(self, kind: str) -> ParseFault | None:
        if kind not in TERMINAL_EVENTS:
            return ParseFault.INVALID_TERMINAL
        if self.terminal is not None:
            return ParseFault.INVALID_TERMINAL
        self.terminal = kind
        return None

    def finish(self) -> Parsed:
        if self.thread_id is None:
            return Parsed(None, self.terminal, ParseFault.MISSING_THREAD)
        if self.terminal is None:
            return Parsed(self.thread_id, None, ParseFault.INVALID_TERMINAL)
        return Parsed(self.thread_id, self.terminal, None)


def _decode_line(line: str) -> dict[str, object] | None:
    """A JSON object with a string ``type``, or None."""
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        return None
    return value


def parse_stream(stdout: str) -> Parsed:
    """Strictly parse one ``codex exec --json`` stream, in memory and pure.

    Blank lines are ignored. Every other line must be a JSON object with a string
    ``type``. Unrelated types are ignored. Exactly one valid ``thread.started``
    must precede exactly one terminal event; ``turn.completed`` is the only
    success.
    """
    state = _StreamState()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = _decode_line(line)
        if event is None:
            return Parsed(state.thread_id, state.terminal, ParseFault.MALFORMED)
        fault = state.feed(event)
        if fault is not None:
            return Parsed(state.thread_id, state.terminal, fault)
    return state.finish()


@dataclass(frozen=True)
class PhaseOutcome:
    exit_code: int | None = None
    terminal: str | None = None
    thread_id: str | None = None
    id_matches: bool = False
    error: ErrorCode | None = None


@dataclass(frozen=True)
class Outcome:
    cli_version: str | None = None
    session_id: str | None = None
    create: PhaseOutcome | None = None
    resume: PhaseOutcome | None = None
    error: ErrorCode | None = None


def _phase_error(label: str, parsed: Parsed, expected: str | None) -> ErrorCode | None:
    if parsed.fault is not None:
        return ErrorCode(f"{label}_{parsed.fault.value}")
    if expected is not None and parsed.thread_id != expected:
        return ErrorCode.RESUME_ID_MISMATCH
    if parsed.terminal != TURN_COMPLETED:
        return ErrorCode(f"{label}_invalid_terminal")
    return None


def _run_phase(
    label: str, argv: list[str], repo: Path, expected: str | None
) -> PhaseOutcome:
    """Run one create or resume. ``expected`` is the create id for a resume."""
    try:
        proc = subprocess.run(
            argv,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return PhaseOutcome(error=ErrorCode(f"{label}_timeout"))
    except OSError:
        return PhaseOutcome(error=ErrorCode.CLI_UNAVAILABLE)
    if proc.returncode != 0:
        return PhaseOutcome(
            exit_code=proc.returncode, error=ErrorCode(f"{label}_nonzero_exit")
        )
    parsed = parse_stream(proc.stdout)
    return PhaseOutcome(
        exit_code=proc.returncode,
        terminal=parsed.terminal,
        thread_id=parsed.thread_id,
        id_matches=(
            parsed.fault is None
            and expected is not None
            and parsed.thread_id == expected
        ),
        error=_phase_error(label, parsed, expected),
    )


def _create_argv(model: str) -> list[str]:
    return [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--model",
        model,
        CREATE_PROMPT,
    ]


def _resume_argv(model: str, session_id: str) -> list[str]:
    return [
        "codex",
        "exec",
        "resume",
        session_id,
        "--json",
        "--ignore-user-config",
        "--model",
        model,
        RESUME_PROMPT,
    ]


def _read_version(repo: Path) -> tuple[str | None, ErrorCode | None]:
    try:
        proc = subprocess.run(
            ["codex", "--version"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, ErrorCode.VERSION_UNAVAILABLE
    except OSError:
        return None, ErrorCode.CLI_UNAVAILABLE
    if proc.returncode != 0:
        return None, ErrorCode.VERSION_UNAVAILABLE
    version = proc.stdout.strip()
    if len(version) > VERSION_MAX_LENGTH or not _VERSION_RE.fullmatch(version):
        return None, ErrorCode.VERSION_INVALID
    return version, None


def _git_init(repo: Path) -> bool:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    try:
        proc = subprocess.run(
            ["git", "init", "--quiet"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _run_in_repo(model: str, repo: Path) -> Outcome:
    if not _git_init(repo):
        return Outcome(error=ErrorCode.REPO_SETUP_FAILED)
    version, version_error = _read_version(repo)
    if version_error is not None:
        return Outcome(error=version_error)
    create = _run_phase("create", _create_argv(model), repo, expected=None)
    session_id = create.thread_id
    if create.error is not None or session_id is None:
        return Outcome(
            cli_version=version,
            session_id=session_id,
            create=create,
            error=create.error or ErrorCode.CREATE_MISSING_THREAD,
        )
    resume = _run_phase(
        "resume", _resume_argv(model, session_id), repo, expected=session_id
    )
    return Outcome(
        cli_version=version,
        session_id=session_id,
        create=create,
        resume=resume,
        error=resume.error,
    )


def _remove_repo(repo: Path) -> bool:
    try:
        shutil.rmtree(repo)
    except OSError:
        return False
    return True


def _probe(model: str, temp_parent: Path) -> Outcome:
    try:
        repo = Path(tempfile.mkdtemp(prefix="cw-live-session-", dir=temp_parent))
    except OSError:
        return Outcome(error=ErrorCode.REPO_SETUP_FAILED)
    try:
        outcome = _run_in_repo(model, repo)
    finally:
        removed = _remove_repo(repo)
    if not removed:
        return replace(outcome, error=ErrorCode.CLEANUP_FAILED)
    return outcome


def _temp_parent() -> Path:
    override = os.environ.get(TMP_PARENT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".cache" / "cw-live-tests").resolve()


def _codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    base = Path(override) if override else Path.home() / ".codex"
    return base.expanduser().resolve()


def _preflight() -> Path | None:
    """The temp parent when no path resolves into the checkout, else None.

    Retains the owner-approved check against paths already resolving into the
    checkout; no replacement auth home and no TOCTOU machinery.
    """
    temp_parent = _temp_parent()
    home = _codex_home()
    guarded = (home, (home / "sessions").resolve(), temp_parent)
    if any(path.is_relative_to(_CHECKOUT_ROOT) for path in guarded):
        return None
    try:
        temp_parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return temp_parent


def _parse_model(args: Sequence[str]) -> str | None:
    """Hand-parse exactly ``--model VALUE``; a leading dash is rejected as a flag."""
    if len(args) != _MODEL_ARGC or args[0] != "--model":
        return None
    value = args[1]
    if value.startswith("-") or not _MODEL_RE.fullmatch(value):
        return None
    return value


def _result(
    status: str,
    error: ErrorCode | None,
    *,
    cli_version: str | None = None,
    model: str | None = None,
    session_id: str | None = None,
    create: dict[str, object] | None = None,
    resume: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "cli_version": cli_version,
        "model": model,
        "session_id": session_id,
        "create": create,
        "resume": resume,
        "error_code": error.value if error is not None else None,
    }


def _outcome_result(model: str, outcome: Outcome) -> dict[str, object]:
    create = outcome.create
    resume = outcome.resume
    create_json: dict[str, object] | None = (
        None
        if create is None
        else {"exit_code": create.exit_code, "terminal_event": create.terminal}
    )
    resume_json: dict[str, object] | None = (
        None
        if resume is None
        else {
            "exit_code": resume.exit_code,
            "terminal_event": resume.terminal,
            "id_matches": resume.id_matches,
        }
    )
    status = STATUS_FAILED if outcome.error is not None else STATUS_PASSED
    return _result(
        status,
        outcome.error,
        cli_version=outcome.cli_version,
        model=model,
        session_id=outcome.session_id,
        create=create_json,
        resume=resume_json,
    )


def _emit(result: dict[str, object]) -> int:
    """Write the one JSON line and return the exit code."""
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] in (STATUS_SKIPPED, STATUS_PASSED) else 1


def main(args: Sequence[str] | None = None) -> int:
    if os.environ.get(OPT_IN_ENV) != "1":
        return _emit(_result(STATUS_SKIPPED, ErrorCode.OPT_IN_REQUIRED))
    argv = list(sys.argv[1:] if args is None else args)
    model = _parse_model(argv)
    if model is None:
        return _emit(_result(STATUS_FAILED, ErrorCode.INVALID_MODEL))
    temp_parent = _preflight()
    if temp_parent is None:
        return _emit(_result(STATUS_FAILED, ErrorCode.REPO_SETUP_FAILED, model=model))
    return _emit(_outcome_result(model, _probe(model, temp_parent)))


def _sanitized_excepthook(
    _exc_type: type[BaseException],
    _exc: BaseException,
    _tb: TracebackType | None,
) -> None:
    """Map an unexpected defect to the fixed ``internal_error`` object.

    Nothing from the exception, its text or its traceback reaches stdout or
    stderr.
    """
    _emit(_result(STATUS_FAILED, ErrorCode.INTERNAL_ERROR))


def _entrypoint() -> int:
    sys.excepthook = _sanitized_excepthook
    return main()


if __name__ == "__main__":
    sys.exit(_entrypoint())
