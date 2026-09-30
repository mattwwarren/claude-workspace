"""Opt-in Codex CLI create/resume smoke probe."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, TypedDict, cast

if TYPE_CHECKING:
    from types import FrameType, TracebackType

type _SignalHandler = (
    Callable[[int, FrameType | None], Any] | int | signal.Handlers | None
)

FIXED_PROMPT = "Reply exactly cw-session-smoke-ok. Do not use tools or modify files."
FIXED_RESUME_PROMPT = (
    "Reply exactly cw-session-smoke-resumed-ok. Do not use tools or modify files."
)
PROCESS_TIMEOUT_SECONDS = 120
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1
PROCESS_READER_JOIN_TIMEOUT_SECONDS = 1
MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024
PROCESS_OUTPUT_CHUNK_BYTES = 64 * 1024
MAX_VERSION_LENGTH = 64
MAX_SESSION_ARTIFACT_SCAN_ENTRIES = 50_000
MAX_SESSION_ARTIFACT_SCAN_SECONDS = 1.0
MAX_SESSION_ARTIFACT_SCAN_DEPTH = 32
CODEX_EXECUTABLE: Final = "codex"
CODEX_EXEC_SUBCOMMAND: Final = "exec"
CODEX_VERSION_FLAG: Final = "--version"
CODEX_SESSION_ARTIFACT_DIR: Final = "sessions"
CODEX_HOME_ENV_KEY: Final = "CODEX_HOME"


class ErrorCode(StrEnum):
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


STATUS_SKIPPED: Final = "skipped"
STATUS_PASSED: Final = "passed"
STATUS_FAILED: Final = "failed"
ERROR_OPT_IN_REQUIRED: Final = ErrorCode.OPT_IN_REQUIRED
ERROR_INVALID_MODEL: Final = ErrorCode.INVALID_MODEL
ERROR_VERSION_UNAVAILABLE: Final = ErrorCode.VERSION_UNAVAILABLE
ERROR_VERSION_INVALID: Final = ErrorCode.VERSION_INVALID
ERROR_CLI_UNAVAILABLE: Final = ErrorCode.CLI_UNAVAILABLE
ERROR_REPO_SETUP_FAILED: Final = ErrorCode.REPO_SETUP_FAILED
ERROR_INTERNAL_ERROR: Final = ErrorCode.INTERNAL_ERROR
ERROR_CREATE_TIMEOUT: Final = ErrorCode.CREATE_TIMEOUT
ERROR_CREATE_NONZERO_EXIT: Final = ErrorCode.CREATE_NONZERO_EXIT
ERROR_CREATE_MALFORMED_JSONL: Final = ErrorCode.CREATE_MALFORMED_JSONL
ERROR_CREATE_MISSING_THREAD: Final = ErrorCode.CREATE_MISSING_THREAD
ERROR_CREATE_INVALID_THREAD_ID: Final = ErrorCode.CREATE_INVALID_THREAD_ID
ERROR_CREATE_DUPLICATE_THREAD_STARTED: Final = ErrorCode.CREATE_DUPLICATE_THREAD_STARTED
ERROR_CREATE_INVALID_TERMINAL: Final = ErrorCode.CREATE_INVALID_TERMINAL
ERROR_RESUME_TIMEOUT: Final = ErrorCode.RESUME_TIMEOUT
ERROR_RESUME_NONZERO_EXIT: Final = ErrorCode.RESUME_NONZERO_EXIT
ERROR_RESUME_MALFORMED_JSONL: Final = ErrorCode.RESUME_MALFORMED_JSONL
ERROR_RESUME_MISSING_THREAD: Final = ErrorCode.RESUME_MISSING_THREAD
ERROR_RESUME_INVALID_THREAD_ID: Final = ErrorCode.RESUME_INVALID_THREAD_ID
ERROR_RESUME_DUPLICATE_THREAD_STARTED: Final = ErrorCode.RESUME_DUPLICATE_THREAD_STARTED
ERROR_RESUME_ID_MISMATCH: Final = ErrorCode.RESUME_ID_MISMATCH
ERROR_RESUME_INVALID_TERMINAL: Final = ErrorCode.RESUME_INVALID_TERMINAL
ERROR_CLEANUP_FAILED: Final = ErrorCode.CLEANUP_FAILED
_VERSION_RE = re.compile(r"^codex-cli [0-9]+\.[0-9]+\.[0-9]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORBIDDEN_MODEL_VALUES = frozenset(
    {
        "--last",
        "--ephemeral",
        "--approve-for-me",
        "--dangerously-bypass-approvals-and-sandbox",
        "--sandbox",
        "-m",
        "-o",
        "read-only",
        "workspace-write",
    }
)
_CHILD_ENV_KEYS = frozenset(
    {
        "path",
        "home",
        "userprofile",
        "systemroot",
        "windir",
        "openai_api_key",
        "codex_api_key",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "ssl_cert_file",
        "ssl_cert_dir",
        "requests_ca_bundle",
        "curl_ca_bundle",
        "lang",
        "lc_all",
        "lc_ctype",
    }
)
_CODEX_AUTH_ENV_KEYS = frozenset({"openai_api_key", "codex_api_key"})

THREAD_STARTED_EVENT: Final = "thread.started"
TURN_STARTED_EVENT: Final = "turn.started"
TURN_COMPLETED_EVENT: Final = "turn.completed"
TURN_FAILED_EVENT: Final = "turn.failed"
ITEM_STARTED_EVENT: Final = "item.started"
ITEM_UPDATED_EVENT: Final = "item.updated"
ITEM_COMPLETED_EVENT: Final = "item.completed"
ERROR_EVENT: Final = "error"


class ParseError(StrEnum):
    MALFORMED_JSONL = "malformed_jsonl"
    MISSING_THREAD = "missing_thread"
    INVALID_THREAD_ID = "invalid_thread_id"
    DUPLICATE_THREAD_STARTED = "duplicate_thread_started"
    INVALID_TERMINAL = "invalid_terminal"


type TerminalEvent = Literal["turn.completed", "turn.failed"]
type ProbeStage = Literal["create", "resume"]
type ProbeStatus = Literal["skipped", "passed", "failed"]

_PARSE_ERROR_CODES: dict[tuple[ProbeStage, ParseError], ErrorCode] = {
    ("create", ParseError.MALFORMED_JSONL): ERROR_CREATE_MALFORMED_JSONL,
    ("create", ParseError.MISSING_THREAD): ERROR_CREATE_MISSING_THREAD,
    ("create", ParseError.INVALID_THREAD_ID): ERROR_CREATE_INVALID_THREAD_ID,
    ("create", ParseError.DUPLICATE_THREAD_STARTED): (
        ERROR_CREATE_DUPLICATE_THREAD_STARTED
    ),
    ("create", ParseError.INVALID_TERMINAL): ERROR_CREATE_INVALID_TERMINAL,
    ("resume", ParseError.MALFORMED_JSONL): ERROR_RESUME_MALFORMED_JSONL,
    ("resume", ParseError.MISSING_THREAD): ERROR_RESUME_MISSING_THREAD,
    ("resume", ParseError.INVALID_THREAD_ID): ERROR_RESUME_INVALID_THREAD_ID,
    ("resume", ParseError.DUPLICATE_THREAD_STARTED): (
        ERROR_RESUME_DUPLICATE_THREAD_STARTED
    ),
    ("resume", ParseError.INVALID_TERMINAL): ERROR_RESUME_INVALID_TERMINAL,
}
_TERMINAL_EVENTS: frozenset[TerminalEvent] = frozenset(
    {TURN_COMPLETED_EVENT, TURN_FAILED_EVENT}
)
_KNOWN_NONTERMINAL_EVENTS: frozenset[str] = frozenset(
    {
        TURN_STARTED_EVENT,
        ITEM_STARTED_EVENT,
        ITEM_UPDATED_EVENT,
        ITEM_COMPLETED_EVENT,
        ERROR_EVENT,
    }
)


class CreateSummary(TypedDict):
    exit_code: int | None
    terminal_event: TerminalEvent | None


class ResumeSummary(TypedDict):
    exit_code: int | None
    terminal_event: TerminalEvent | None
    id_matches: bool


class SmokeResult(TypedDict):
    status: ProbeStatus
    cli_version: str | None
    model: str | None
    session_id: str | None
    create: CreateSummary | None
    resume: ResumeSummary | None
    error_code: ErrorCode | None


@dataclass(frozen=True)
class ParsedStream:
    session_id: str | None
    terminal_event: TerminalEvent | None
    error: ParseError | None


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    stdout: str
    timed_out: bool
    unavailable: bool = False
    output_exceeded_limit: bool = False
    reader_incomplete: bool = False
    reader_failed: bool = False


@dataclass
class _OutputCapture:
    data: bytearray
    exceeded_limit: bool = False
    reader_failed: bool = False


class _UnexpectedReaderError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("unexpected probe stdout reader failure")


@dataclass
class _StreamState:
    session_id: str | None = None
    terminal_event: TerminalEvent | None = None
    thread_seen: bool = False
    error_event_seen: bool = False


@dataclass(frozen=True)
class _VersionSuccess:
    cli_version: str


@dataclass(frozen=True)
class _VersionFailure:
    error_code: ErrorCode


type _VersionOutcome = _VersionSuccess | _VersionFailure


class _ArgumentParseError(Exception):
    """A CLI argument error that must be rendered through the JSON contract."""


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _ArgumentParseError


def _decode_event(line: str) -> tuple[dict[str, object] | None, ParseError | None]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None, ParseError.MALFORMED_JSONL
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None, ParseError.MALFORMED_JSONL
    return cast("dict[str, object]", event), None


def _handle_thread(event: dict[str, object], state: _StreamState) -> ParseError | None:
    if state.thread_seen:
        return ParseError.DUPLICATE_THREAD_STARTED
    session_id = event.get("thread_id")
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        return ParseError.INVALID_THREAD_ID
    state.session_id = session_id
    state.thread_seen = True
    return None


def _handle_other(event: dict[str, object], state: _StreamState) -> ParseError | None:
    event_type = cast("str", event["type"])
    error: ParseError | None = None
    if state.terminal_event is not None:
        error = ParseError.INVALID_TERMINAL
    elif event_type in _TERMINAL_EVENTS:
        if not state.thread_seen:
            error = ParseError.MISSING_THREAD
        else:
            state.terminal_event = event_type
    elif not state.thread_seen:
        error = ParseError.MALFORMED_JSONL
    elif event_type == ERROR_EVENT:
        if state.error_event_seen or not isinstance(event.get("message"), str):
            error = ParseError.MALFORMED_JSONL
        else:
            state.error_event_seen = True
    elif event_type not in _KNOWN_NONTERMINAL_EVENTS and (
        event_type.startswith("turn.")
        or event_type.endswith((".completed", ".failed", ".error"))
    ):
        error = ParseError.MALFORMED_JSONL
    return error


def _parse_line(line: str, state: _StreamState) -> ParseError | None:
    event, error = _decode_event(line)
    if error is None and event is not None:
        event_type = cast("str", event["type"])
        error = (
            _handle_thread(event, state)
            if event_type == THREAD_STARTED_EVENT
            else _handle_other(event, state)
        )
    return error


def _parse_stream(stdout: str) -> ParsedStream:
    """Strictly validate a Codex JSONL stream without retaining payloads."""
    state = _StreamState()
    for line in io.StringIO(stdout):
        if line.strip():
            error = _parse_line(line, state)
            if error is not None:
                return ParsedStream(state.session_id, state.terminal_event, error)
    if not state.thread_seen:
        return ParsedStream(
            state.session_id, state.terminal_event, ParseError.MISSING_THREAD
        )
    if state.terminal_event is None:
        return ParsedStream(
            state.session_id, state.terminal_event, ParseError.INVALID_TERMINAL
        )
    if state.error_event_seen and state.terminal_event == TURN_COMPLETED_EVENT:
        return ParsedStream(
            state.session_id, state.terminal_event, ParseError.INVALID_TERMINAL
        )
    return ParsedStream(state.session_id, state.terminal_event, None)


def _parse_version(stdout: str) -> str | None:
    if stdout.endswith("\r\n"):
        version = stdout[:-2]
    elif stdout.endswith(("\r", "\n")):
        version = stdout[:-1]
    else:
        version = stdout
    return (
        version
        if len(version) <= MAX_VERSION_LENGTH and _VERSION_RE.fullmatch(version)
        else None
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the command and descendants which may keep stdout open."""
    if os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        else:
            return
    with contextlib.suppress(OSError):
        process.kill()


def _contains_symlink(path: Path) -> bool:
    """Fail closed on symlinks or when the bounded session scan is inconclusive."""
    deadline = time.monotonic() + MAX_SESSION_ARTIFACT_SCAN_SECONDS
    entries_seen = 0
    try:
        scanners = [(os.scandir(path), 0)]
    except OSError:
        return True
    has_symlink_or_error = False
    try:
        while scanners:
            if time.monotonic() > deadline:
                has_symlink_or_error = True
                break
            scanner, depth = scanners[-1]
            try:
                entry = next(scanner)
            except StopIteration:
                with contextlib.suppress(OSError):
                    scanner.close()
                scanners.pop()
                continue
            entries_seen += 1
            if (
                entries_seen > MAX_SESSION_ARTIFACT_SCAN_ENTRIES
                or time.monotonic() > deadline
            ):
                has_symlink_or_error = True
                break
            if entry.is_symlink():
                has_symlink_or_error = True
                break
            if entry.is_dir(follow_symlinks=False):
                if depth >= MAX_SESSION_ARTIFACT_SCAN_DEPTH:
                    has_symlink_or_error = True
                    break
                scanners.append((os.scandir(entry.path), depth + 1))
    except OSError:
        has_symlink_or_error = True
    finally:
        for scanner, _depth in scanners:
            with contextlib.suppress(OSError):
                scanner.close()
    return has_symlink_or_error


def _make_stdout_reader(
    process: subprocess.Popen[bytes], capture: _OutputCapture
) -> threading.Thread:
    def fail_reader() -> None:
        capture.reader_failed = True
        # Keep cleanup failures from escaping the reader thread as well.
        with contextlib.suppress(Exception):
            _kill_process_group(process)

    def drain_stdout() -> None:
        if process.stdout is None:
            fail_reader()
            return
        try:
            stdout = cast("io.BufferedReader", process.stdout)
            while chunk := stdout.read1(PROCESS_OUTPUT_CHUNK_BYTES):
                remaining = MAX_PROCESS_OUTPUT_BYTES - len(capture.data)
                if len(chunk) > remaining:
                    if remaining > 0:
                        capture.data.extend(chunk[:remaining])
                    capture.exceeded_limit = True
                    _kill_process_group(process)
                    with contextlib.suppress(OSError, ValueError):
                        stdout.close()
                    return
                capture.data.extend(chunk)
        except (OSError, ValueError, RuntimeError, TypeError):
            # Route pipe and reader failures through the sanitized main-thread path.
            fail_reader()

    return threading.Thread(target=drain_stdout, daemon=True)


def _finish_stdout_capture(
    process: subprocess.Popen[bytes],
    reader: threading.Thread,
    capture: _OutputCapture,
) -> tuple[str, bool]:
    reader.join(timeout=PROCESS_READER_JOIN_TIMEOUT_SECONDS)
    reader_incomplete = reader.is_alive()
    if reader_incomplete:
        _kill_process_group(process)
        reader.join(timeout=PROCESS_READER_JOIN_TIMEOUT_SECONDS)
    if process.stdout is not None:
        with contextlib.suppress(OSError, ValueError):
            process.stdout.close()
    reader.join(timeout=PROCESS_READER_JOIN_TIMEOUT_SECONDS)
    return (
        bytes(capture.data).decode("utf-8", errors="replace"),
        reader_incomplete or reader.is_alive(),
    )


def _stop_process(process: subprocess.Popen[bytes]) -> int | None:
    _kill_process_group(process)
    exit_code: int | None = None
    with contextlib.suppress(OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
        exit_code = process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
    if process.stdout is not None:
        with contextlib.suppress(OSError, ValueError, KeyboardInterrupt):
            process.stdout.close()
    return exit_code


@contextlib.contextmanager
def _install_parent_signal_handlers(
    handler: _SignalHandler, *, ignore_restore_errors: bool = False
) -> Iterator[None]:
    previous_handlers: dict[signal.Signals, _SignalHandler] = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, handler)
        yield
    finally:
        for signum, previous_handler in previous_handlers.items():
            if ignore_restore_errors:
                with contextlib.suppress(Exception):
                    signal.signal(signum, previous_handler)
            else:
                signal.signal(signum, previous_handler)


def _launch_process(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.Popen[bytes] | ProcessOutcome | None:
    process: subprocess.Popen[bytes] | None = None
    signal_received = False
    launch_failed = False

    def defer_signal(_signum: int, _frame: FrameType | None) -> None:
        nonlocal signal_received
        signal_received = True

    def raise_if_signal_received() -> None:
        if signal_received:
            raise KeyboardInterrupt

    try:
        with _install_parent_signal_handlers(defer_signal):
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                )
            except OSError:
                launch_failed = True
            raise_if_signal_received()
    except BaseException as error:
        cleanup_exit_code: int | None = None
        if process is not None:
            cleanup_exit_code = _stop_process(process)
        if isinstance(error, KeyboardInterrupt):
            return ProcessOutcome(cleanup_exit_code, "", True)
        raise
    if launch_failed or process is None:
        return None
    return process


def _wait_for_process(
    process: subprocess.Popen[bytes],
) -> tuple[int | None, bool, bool]:
    try:
        return process.wait(timeout=PROCESS_TIMEOUT_SECONDS), False, False
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        try:
            return (
                process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS),
                True,
                False,
            )
        except (OSError, subprocess.TimeoutExpired):
            _kill_process_group(process)
            return None, True, True
    except OSError:
        _kill_process_group(process)
        return None, False, True


def _cleanup_interrupted_process(
    process: subprocess.Popen[bytes],
    reader: threading.Thread | None,
    capture: _OutputCapture | None,
) -> ProcessOutcome:
    exit_code = _stop_process(process)
    stdout = ""
    reader_incomplete = False
    if reader is not None and reader.ident is not None and capture is not None:
        try:
            stdout, reader_incomplete = _finish_stdout_capture(process, reader, capture)
        except (OSError, ValueError):
            reader_incomplete = True
        except KeyboardInterrupt:
            _kill_process_group(process)
            reader_incomplete = True
    return ProcessOutcome(
        exit_code,
        stdout,
        True,
        output_exceeded_limit=capture.exceeded_limit if capture is not None else False,
        reader_incomplete=reader_incomplete,
        reader_failed=capture.reader_failed if capture is not None else False,
    )


def _run_process(argv: list[str], *, cwd: Path, env: dict[str, str]) -> ProcessOutcome:
    launched = _launch_process(argv, cwd=cwd, env=env)
    if isinstance(launched, ProcessOutcome):
        return launched
    if launched is None:
        return ProcessOutcome(None, "", False, unavailable=True)
    process = launched

    reader: threading.Thread | None = None
    capture: _OutputCapture | None = None
    try:
        capture = _OutputCapture(bytearray())
        reader = _make_stdout_reader(process, capture)
        reader.start()
        exit_code, timed_out, unavailable = _wait_for_process(process)
        stdout, reader_incomplete = _finish_stdout_capture(process, reader, capture)
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            return _cleanup_interrupted_process(process, reader, capture)
        _cleanup_interrupted_process(process, reader, capture)
        raise
    return ProcessOutcome(
        exit_code,
        stdout,
        timed_out,
        unavailable=unavailable,
        output_exceeded_limit=capture.exceeded_limit,
        reader_incomplete=reader_incomplete,
        reader_failed=capture.reader_failed,
    )


def _result(
    status: ProbeStatus,
    *,
    cli_version: str | None,
    model: str | None,
    session_id: str | None = None,
    create: CreateSummary | None = None,
    resume: ResumeSummary | None = None,
    error_code: ErrorCode | None,
) -> SmokeResult:
    return {
        "status": status,
        "cli_version": cli_version,
        "model": model,
        "session_id": session_id,
        "create": create,
        "resume": resume,
        "error_code": error_code,
    }


@dataclass(frozen=True)
class _Attempt:
    outcome: ProcessOutcome
    stream: ParsedStream
    error_code: ErrorCode | None


def _attempt(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stage: ProbeStage,
) -> _Attempt:
    outcome = _run_process(argv, cwd=cwd, env=env)
    if outcome.reader_failed:
        raise _UnexpectedReaderError
    stream = _parse_stream(outcome.stdout)
    if outcome.output_exceeded_limit or outcome.reader_incomplete:
        stream = ParsedStream(stream.session_id, None, ParseError.MALFORMED_JSONL)
    error_code: ErrorCode | None = None
    if outcome.timed_out:
        error_code = ERROR_CREATE_TIMEOUT if stage == "create" else ERROR_RESUME_TIMEOUT
    elif outcome.unavailable:
        error_code = ERROR_CLI_UNAVAILABLE
    elif outcome.output_exceeded_limit or outcome.reader_incomplete:
        error_code = _stream_error_code(stage, ParseError.MALFORMED_JSONL)
    elif outcome.exit_code != 0:
        error_code = (
            ERROR_CREATE_NONZERO_EXIT
            if stage == "create"
            else ERROR_RESUME_NONZERO_EXIT
        )
    elif stream.error is not None:
        error_code = _stream_error_code(stage, stream.error)
    elif stream.terminal_event != TURN_COMPLETED_EVENT:
        error_code = (
            ERROR_CREATE_INVALID_TERMINAL
            if stage == "create"
            else ERROR_RESUME_INVALID_TERMINAL
        )
    # Retain only the parsed summary: the create transcript must be gone before
    # the resume subprocess starts.
    compact_outcome = ProcessOutcome(
        outcome.exit_code,
        "",
        outcome.timed_out,
        unavailable=outcome.unavailable,
        output_exceeded_limit=outcome.output_exceeded_limit,
        reader_incomplete=outcome.reader_incomplete,
    )
    return _Attempt(compact_outcome, stream, error_code)


def _stream_error_code(stage: ProbeStage, error: ParseError) -> ErrorCode:
    return _PARSE_ERROR_CODES[(stage, error)]


def _create_summary(attempt: _Attempt) -> CreateSummary:
    return {
        "exit_code": attempt.outcome.exit_code,
        "terminal_event": attempt.stream.terminal_event,
    }


def _resume_summary(attempt: _Attempt, session_id: str) -> ResumeSummary:
    return {
        "exit_code": attempt.outcome.exit_code,
        "terminal_event": attempt.stream.terminal_event,
        "id_matches": (
            attempt.stream.session_id == session_id and attempt.stream.error is None
        ),
    }


def _common_exec_flags(model: str, *, after_json: tuple[str, ...] = ()) -> list[str]:
    """Build the shared JSON/config/model flags for create and resume."""
    return [
        "--json",
        *after_json,
        "--ignore-user-config",
        "--model",
        model,
    ]


def _create_argv(model: str) -> list[str]:
    return [
        CODEX_EXECUTABLE,
        CODEX_EXEC_SUBCOMMAND,
        *_common_exec_flags(model, after_json=("--sandbox", "read-only")),
        FIXED_PROMPT,
    ]


def _resume_argv(session_id: str, model: str) -> list[str]:
    return [
        CODEX_EXECUTABLE,
        CODEX_EXEC_SUBCOMMAND,
        "resume",
        session_id,
        *_common_exec_flags(model),
        FIXED_RESUME_PROMPT,
    ]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _checkout_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _shared_codex_home(checkout: Path) -> Path | None:
    resolver_path = checkout / ".claude" / "scripts" / "utils" / "runtime_paths.py"
    spec = importlib.util.spec_from_file_location(
        "cw_probe_runtime_paths", resolver_path
    )
    if spec is None or spec.loader is None:
        return None
    try:
        resolver_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver_module)
        resolver = getattr(resolver_module, "codex_home", None)
        if not callable(resolver):
            return None
        path = cast("Callable[[], Path]", resolver)()
    except (ImportError, OSError, RuntimeError):
        return None
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _codex_home(checkout: Path) -> Path | None:
    path = _shared_codex_home(checkout)
    if path is None:
        return None
    try:
        resolved = path.resolve()
        session_artifacts = resolved / CODEX_SESSION_ARTIFACT_DIR
        resolved_session_artifacts = session_artifacts.resolve()
    except (OSError, RuntimeError):
        return None
    if session_artifacts.is_symlink():
        return None
    if _is_within(resolved, checkout) or _is_within(
        resolved_session_artifacts, checkout
    ):
        return None
    if session_artifacts.exists() and _contains_symlink(session_artifacts):
        return None
    return resolved


def _child_environment(
    *,
    cwd: Path,
    scratch: Path,
    codex_home: Path | None,
    include_codex_auth: bool = True,
) -> dict[str, str]:
    """Build an allowlisted environment for Codex or non-credentialed setup."""
    allowed_keys = (
        _CHILD_ENV_KEYS
        if include_codex_auth
        else _CHILD_ENV_KEYS - _CODEX_AUTH_ENV_KEYS
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in allowed_keys
    }
    isolated_home = str(scratch.parent)
    environment.update(
        {
            "HOME": isolated_home,
            "USERPROFILE": isolated_home,
            "PWD": str(cwd),
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
        }
    )
    if include_codex_auth and codex_home is not None:
        environment[CODEX_HOME_ENV_KEY] = str(codex_home)
    return environment


def _run_session(
    *,
    worktree: Path,
    codex_env: dict[str, str],
    git_env: dict[str, str],
    cli_version: str,
    model: str,
) -> SmokeResult:
    git_init = _run_process(["git", "init", "--quiet"], cwd=worktree, env=git_env)
    if git_init.reader_failed:
        error_code = ERROR_INTERNAL_ERROR
    elif (
        git_init.unavailable
        or git_init.timed_out
        or git_init.output_exceeded_limit
        or git_init.reader_incomplete
        or git_init.exit_code != 0
    ):
        error_code = ERROR_REPO_SETUP_FAILED
    else:
        error_code = None
    if error_code is not None:
        return _result(
            STATUS_FAILED,
            cli_version=cli_version,
            model=model,
            error_code=error_code,
        )

    create = _attempt(
        _create_argv(model),
        cwd=worktree,
        env=codex_env,
        stage="create",
    )
    create_summary = _create_summary(create)
    if create.error_code is not None:
        return _result(
            STATUS_FAILED,
            cli_version=cli_version,
            model=model,
            session_id=create.stream.session_id,
            create=create_summary,
            error_code=create.error_code,
        )
    session_id = create.stream.session_id
    if session_id is None:
        return _result(
            STATUS_FAILED,
            cli_version=cli_version,
            model=model,
            create=create_summary,
            error_code=ERROR_CREATE_MISSING_THREAD,
        )

    resume = _attempt(
        _resume_argv(session_id, model),
        cwd=worktree,
        env=codex_env,
        stage="resume",
    )
    resume_summary = _resume_summary(resume, session_id)
    if resume.error_code is not None:
        return _result(
            STATUS_FAILED,
            cli_version=cli_version,
            model=model,
            session_id=session_id,
            create=create_summary,
            resume=resume_summary,
            error_code=resume.error_code,
        )
    if not resume_summary["id_matches"]:
        return _result(
            STATUS_FAILED,
            cli_version=cli_version,
            model=model,
            session_id=session_id,
            create=create_summary,
            resume=resume_summary,
            error_code=ERROR_RESUME_ID_MISMATCH,
        )
    return _result(
        STATUS_PASSED,
        cli_version=cli_version,
        model=model,
        session_id=session_id,
        create=create_summary,
        resume=resume_summary,
        error_code=None,
    )


def _version_result(*, cwd: Path, env: dict[str, str]) -> _VersionOutcome:
    outcome = _run_process([CODEX_EXECUTABLE, CODEX_VERSION_FLAG], cwd=cwd, env=env)
    if outcome.reader_failed:
        raise _UnexpectedReaderError
    if outcome.unavailable:
        return _VersionFailure(ERROR_CLI_UNAVAILABLE)
    if outcome.timed_out or outcome.exit_code != 0:
        return _VersionFailure(ERROR_VERSION_UNAVAILABLE)
    version = None if outcome.reader_incomplete else _parse_version(outcome.stdout)
    if version is None:
        return _VersionFailure(ERROR_VERSION_INVALID)
    return _VersionSuccess(version)


def _raise_for_parent_signal(_signum: int, _frame: FrameType | None) -> NoReturn:
    raise KeyboardInterrupt


def _handle_unhandled_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: TracebackType | None,
) -> None:
    del exc_type, exc_value, traceback
    result = _result(
        STATUS_FAILED,
        cli_version=None,
        model=None,
        error_code=ERROR_INTERNAL_ERROR,
    )
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")


def _new_temporary_directory(
    checkout: Path,
) -> tempfile.TemporaryDirectory[str] | None:
    try:
        temp_parent = (Path.home() / ".cache" / "cw-live-tests").resolve()
        if _is_within(temp_parent, checkout):
            return None
        temp_parent.mkdir(parents=True, exist_ok=True)
        temp_parent = temp_parent.resolve()
        if _is_within(temp_parent, checkout):
            return None
        return tempfile.TemporaryDirectory(
            prefix="cw-codex-live-session-", dir=str(temp_parent)
        )
    except (OSError, RuntimeError):
        return None


def _run_in_disposable_directory(
    *, temp_root: Path, checkout: Path, codex_home: Path, model: str
) -> SmokeResult:
    if _is_within(temp_root, checkout) or _is_within(codex_home, temp_root):
        return _result(
            STATUS_FAILED,
            cli_version=None,
            model=model,
            error_code=ERROR_REPO_SETUP_FAILED,
        )
    worktree = temp_root / "repo"
    scratch = temp_root / "tmp"
    try:
        worktree.mkdir()
        scratch.mkdir()
    except OSError:
        return _result(
            STATUS_FAILED,
            cli_version=None,
            model=model,
            error_code=ERROR_REPO_SETUP_FAILED,
        )
    codex_env = _child_environment(cwd=worktree, scratch=scratch, codex_home=codex_home)
    setup_env = _child_environment(
        cwd=worktree,
        scratch=scratch,
        codex_home=None,
        include_codex_auth=False,
    )
    version_outcome = _version_result(cwd=worktree, env=setup_env)
    if isinstance(version_outcome, _VersionFailure):
        return _result(
            STATUS_FAILED,
            cli_version=None,
            model=model,
            error_code=version_outcome.error_code,
        )
    return _run_session(
        worktree=worktree,
        codex_env=codex_env,
        git_env=setup_env,
        cli_version=version_outcome.cli_version,
        model=model,
    )


def _cleanup_temporary_directory(
    temporary_directory: tempfile.TemporaryDirectory[str], result: SmokeResult
) -> SmokeResult:
    signal_received = False
    cleanup_failed = False

    def defer_signal(_signum: int, _frame: FrameType | None) -> None:
        nonlocal signal_received
        signal_received = True

    with _install_parent_signal_handlers(defer_signal):
        try:
            temporary_directory.cleanup()
        except OSError:
            cleanup_failed = True

    if cleanup_failed:
        error_code: ErrorCode = ERROR_CLEANUP_FAILED
    elif signal_received:
        error_code = ERROR_REPO_SETUP_FAILED
    else:
        return result
    return _result(
        STATUS_FAILED,
        cli_version=result["cli_version"],
        model=result["model"],
        session_id=result["session_id"],
        create=result["create"],
        resume=result["resume"],
        error_code=error_code,
    )


def _run_disposable(*, model: str) -> SmokeResult:
    try:
        checkout = _checkout_root()
        codex_home = _codex_home(checkout)
    except (OSError, RuntimeError):
        return _result(
            STATUS_FAILED,
            cli_version=None,
            model=model,
            error_code=ERROR_REPO_SETUP_FAILED,
        )
    if codex_home is None:
        return _result(
            STATUS_FAILED,
            cli_version=None,
            model=model,
            error_code=ERROR_REPO_SETUP_FAILED,
        )
    temporary_directory = _new_temporary_directory(checkout)
    if temporary_directory is None:
        return _result(
            STATUS_FAILED,
            cli_version=None,
            model=model,
            error_code=ERROR_REPO_SETUP_FAILED,
        )
    result = _result(
        STATUS_FAILED,
        cli_version=None,
        model=model,
        error_code=ERROR_REPO_SETUP_FAILED,
    )
    try:
        temp_root = Path(temporary_directory.name)
        result = _run_in_disposable_directory(
            temp_root=temp_root,
            checkout=checkout,
            codex_home=codex_home,
            model=model,
        )
    finally:
        result = _cleanup_temporary_directory(temporary_directory, result)
    return result


def run_probe(model: str | None) -> SmokeResult:
    """Run the opt-in probe and return its sanitized result."""
    if os.environ.get("CW_CODEX_LIVE_SESSION_SMOKE") != "1":
        return _result(
            STATUS_SKIPPED,
            cli_version=None,
            model=None,
            error_code=ERROR_OPT_IN_REQUIRED,
        )
    if (
        model is None
        or model.startswith("-")
        or model in _FORBIDDEN_MODEL_VALUES
        or _MODEL_RE.fullmatch(model) is None
    ):
        return _result(
            STATUS_FAILED,
            cli_version=None,
            model=None,
            error_code=ERROR_INVALID_MODEL,
        )
    return _run_disposable(model=model)


def main(argv: list[str] | None = None) -> int:
    parser = _SanitizedArgumentParser(description="Run the Codex session smoke probe.")
    parser.add_argument("--model", help="Explicit Codex model identifier.")
    try:
        model = parser.parse_args(argv).model
    except _ArgumentParseError:
        model = None
    failure_result = _result(
        STATUS_FAILED,
        cli_version=None,
        model=None,
        error_code=ERROR_REPO_SETUP_FAILED,
    )
    result = failure_result
    with _install_parent_signal_handlers(
        _raise_for_parent_signal, ignore_restore_errors=True
    ):
        try:
            result = run_probe(model)
        except KeyboardInterrupt:
            result = failure_result
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0 if result["status"] in {STATUS_SKIPPED, STATUS_PASSED} else 1


def _entrypoint() -> NoReturn:
    sys.excepthook = _handle_unhandled_exception
    raise SystemExit(main())


if __name__ == "__main__":
    _entrypoint()
