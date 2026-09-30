"""Opt-in Codex CLI create/resume smoke probe."""

from __future__ import annotations

import argparse
import contextlib
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
from concurrent.futures import Future, wait
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
PROCESS_CLEANUP_ATTEMPTS = 3
PROCESS_READER_WAIT_TIMEOUT_SECONDS = 1
MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024
PROCESS_OUTPUT_CHUNK_BYTES = 64 * 1024
MAX_VERSION_LENGTH = 64
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
_CODEX_AUTH_ENV_KEYS = frozenset({"openai_api_key", "codex_api_key"})
_BASE_CHILD_ENV_KEYS = frozenset(
    {
        "path",
        "home",
        "userprofile",
        "systemroot",
        "windir",
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
_CHILD_ENV_KEYS = _BASE_CHILD_ENV_KEYS | _CODEX_AUTH_ENV_KEYS

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


class ProbeStage(StrEnum):
    CREATE = "create"
    RESUME = "resume"


type TerminalEvent = Literal["turn.completed", "turn.failed"]
type ProbeStatus = Literal["skipped", "passed", "failed"]

_PARSE_ERROR_CODES: dict[tuple[ProbeStage, ParseError], ErrorCode] = {
    (ProbeStage.CREATE, ParseError.MALFORMED_JSONL): ERROR_CREATE_MALFORMED_JSONL,
    (ProbeStage.CREATE, ParseError.MISSING_THREAD): ERROR_CREATE_MISSING_THREAD,
    (ProbeStage.CREATE, ParseError.INVALID_THREAD_ID): ERROR_CREATE_INVALID_THREAD_ID,
    (ProbeStage.CREATE, ParseError.DUPLICATE_THREAD_STARTED): (
        ERROR_CREATE_DUPLICATE_THREAD_STARTED
    ),
    (ProbeStage.CREATE, ParseError.INVALID_TERMINAL): ERROR_CREATE_INVALID_TERMINAL,
    (ProbeStage.RESUME, ParseError.MALFORMED_JSONL): ERROR_RESUME_MALFORMED_JSONL,
    (ProbeStage.RESUME, ParseError.MISSING_THREAD): ERROR_RESUME_MISSING_THREAD,
    (ProbeStage.RESUME, ParseError.INVALID_THREAD_ID): ERROR_RESUME_INVALID_THREAD_ID,
    (ProbeStage.RESUME, ParseError.DUPLICATE_THREAD_STARTED): (
        ERROR_RESUME_DUPLICATE_THREAD_STARTED
    ),
    (ProbeStage.RESUME, ParseError.INVALID_TERMINAL): ERROR_RESUME_INVALID_TERMINAL,
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
    cleanup_failed: bool = False


@dataclass
class _OutputCapture:
    data: bytearray
    exceeded_limit: bool = False
    reader_failed: bool = False


class _UnexpectedReaderError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("unexpected probe stdout reader failure")


class _StdoutPipeUnavailableError(RuntimeError):
    pass


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
        except ProcessLookupError:
            pass
        else:
            return
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def _make_stdout_reader(
    process: subprocess.Popen[bytes], capture: _OutputCapture
) -> Callable[[], None]:
    def drain_stdout() -> None:
        if process.stdout is None:
            raise _StdoutPipeUnavailableError
        stdout = cast("io.BufferedReader", process.stdout)
        try:
            while chunk := stdout.read1(PROCESS_OUTPUT_CHUNK_BYTES):
                remaining = MAX_PROCESS_OUTPUT_BYTES - len(capture.data)
                if len(chunk) > remaining:
                    if remaining > 0:
                        capture.data.extend(chunk[:remaining])
                    capture.exceeded_limit = True
                    return
                capture.data.extend(chunk)
        finally:
            with contextlib.suppress(OSError, ValueError):
                stdout.close()

    return drain_stdout


class _FutureResultCapture:
    def __init__(self, future: Future[None]) -> None:
        self.future = future

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        if exc_value is None:
            self.future.set_result(None)
            return False
        self.future.set_exception(exc_value)
        return True


def _start_stdout_reader(
    process: subprocess.Popen[bytes], capture: _OutputCapture
) -> Future[None]:
    reader_future: Future[None] = Future()
    reader = _make_stdout_reader(process, capture)

    def drain_and_report() -> None:
        if not reader_future.set_running_or_notify_cancel():
            return
        with _FutureResultCapture(reader_future):
            reader()

    threading.Thread(
        target=drain_and_report,
        name="cw-codex-stdout",
        daemon=True,
    ).start()
    return reader_future


def _finish_stdout_capture(
    process: subprocess.Popen[bytes],
    reader: Future[None],
    capture: _OutputCapture,
) -> tuple[str, bool]:
    _, pending = wait((reader,), timeout=PROCESS_READER_WAIT_TIMEOUT_SECONDS)
    reader_incomplete = bool(pending)
    if reader_incomplete:
        _kill_process_group(process)
        _, pending = wait((reader,), timeout=PROCESS_READER_WAIT_TIMEOUT_SECONDS)
    if pending:
        _, pending = wait((reader,), timeout=PROCESS_READER_WAIT_TIMEOUT_SECONDS)
    if reader.done():
        reader_error = reader.exception()
        if isinstance(reader_error, Exception):
            capture.reader_failed = True
            _kill_process_group(process)
        elif reader_error is not None:
            raise reader_error
    return (
        bytes(capture.data).decode("utf-8", errors="replace"),
        reader_incomplete or bool(pending),
    )


def _stop_process(
    process: subprocess.Popen[bytes], reader: Future[None] | None = None
) -> tuple[int | None, bool]:
    exit_code, _timed_out, _unavailable, cleanup_failed = _kill_and_wait(
        process, timed_out=False
    )
    if process.stdout is not None and reader is None:
        with contextlib.suppress(OSError, ValueError, KeyboardInterrupt):
            process.stdout.close()
    return exit_code, cleanup_failed


@contextlib.contextmanager
def _install_parent_signal_handlers(
    handler: _SignalHandler,
) -> Iterator[None]:
    previous_handlers: dict[signal.Signals, _SignalHandler] = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, handler)
        yield
    finally:
        restoration_errors: list[Exception] = []
        for signum, previous_handler in previous_handlers.items():
            try:
                signal.signal(signum, previous_handler)
            except (OSError, ValueError) as error:
                restoration_errors.append(error)
        if restoration_errors:
            message = "failed to restore parent signal handlers"
            raise ExceptionGroup(message, restoration_errors)


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
        raise_if_signal_received()
    except BaseException as error:
        cleanup_exit_code: int | None = None
        cleanup_failed = False
        if process is not None:
            cleanup_exit_code, cleanup_failed = _stop_process(process)
        if isinstance(error, KeyboardInterrupt):
            return ProcessOutcome(
                cleanup_exit_code,
                "",
                True,
                cleanup_failed=cleanup_failed,
            )
        raise
    if launch_failed or process is None:
        return None
    return process


def _kill_and_wait(
    process: subprocess.Popen[bytes], *, timed_out: bool
) -> tuple[int | None, bool, bool, bool]:
    _kill_process_group(process)
    for attempt in range(PROCESS_CLEANUP_ATTEMPTS):
        try:
            return (
                process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS),
                timed_out,
                False,
                False,
            )
        except (OSError, subprocess.TimeoutExpired):
            if attempt + 1 < PROCESS_CLEANUP_ATTEMPTS:
                _kill_process_group(process)
    return None, timed_out, False, True


def _wait_for_process(
    process: subprocess.Popen[bytes],
    reader: Future[None],
    capture: _OutputCapture,
) -> tuple[int | None, bool, bool, bool]:
    deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
    while True:
        if capture.exceeded_limit:
            return _kill_and_wait(process, timed_out=False)
        if reader.done():
            reader_error = reader.exception()
            if isinstance(reader_error, Exception):
                capture.reader_failed = True
            elif reader_error is not None:
                raise reader_error
            if capture.reader_failed:
                return _kill_and_wait(process, timed_out=False)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _kill_and_wait(process, timed_out=True)
        try:
            return process.wait(timeout=min(remaining, 0.05)), False, False, False
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            exit_code, _timed_out, _unavailable, cleanup_failed = _kill_and_wait(
                process, timed_out=False
            )
            return exit_code, False, True, cleanup_failed


def _cleanup_interrupted_process(
    process: subprocess.Popen[bytes],
    reader: Future[None] | None,
    capture: _OutputCapture | None,
) -> ProcessOutcome:
    exit_code, cleanup_failed = _stop_process(process, reader)
    stdout = ""
    reader_incomplete = False
    if reader is not None and capture is not None:
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
        cleanup_failed=cleanup_failed,
    )


def _run_process(argv: list[str], *, cwd: Path, env: dict[str, str]) -> ProcessOutcome:
    launched = _launch_process(argv, cwd=cwd, env=env)
    if isinstance(launched, ProcessOutcome):
        return launched
    if launched is None:
        return ProcessOutcome(None, "", False, unavailable=True)
    process = launched

    reader: Future[None] | None = None
    capture: _OutputCapture | None = None
    try:
        capture = _OutputCapture(bytearray())
        reader = _start_stdout_reader(process, capture)
        exit_code, timed_out, unavailable, cleanup_failed = _wait_for_process(
            process, reader, capture
        )
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
        cleanup_failed=cleanup_failed,
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


@dataclass
class _UnhandledExceptionDiagnostic:
    result: SmokeResult


_UNHANDLED_EXCEPTION_DIAGNOSTIC = _UnhandledExceptionDiagnostic(
    _result(
        STATUS_FAILED,
        cli_version=None,
        model=None,
        error_code=ERROR_INTERNAL_ERROR,
    )
)


def _reset_unhandled_exception_result() -> None:
    _UNHANDLED_EXCEPTION_DIAGNOSTIC.result = _result(
        STATUS_FAILED,
        cli_version=None,
        model=None,
        error_code=ERROR_INTERNAL_ERROR,
    )


def _record_unhandled_exception_progress(
    *,
    cli_version: str,
    model: str,
    session_id: str | None = None,
    create: CreateSummary | None = None,
    resume: ResumeSummary | None = None,
) -> None:
    _UNHANDLED_EXCEPTION_DIAGNOSTIC.result = _result(
        STATUS_FAILED,
        cli_version=cli_version,
        model=model,
        session_id=session_id,
        create=create,
        resume=resume,
        error_code=ERROR_INTERNAL_ERROR,
    )


def _set_unhandled_exception_code(error_code: ErrorCode) -> None:
    previous = _UNHANDLED_EXCEPTION_DIAGNOSTIC.result
    _UNHANDLED_EXCEPTION_DIAGNOSTIC.result = _result(
        STATUS_FAILED,
        cli_version=previous["cli_version"],
        model=previous["model"],
        session_id=previous["session_id"],
        create=previous["create"],
        resume=previous["resume"],
        error_code=error_code,
    )


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
    if outcome.cleanup_failed:
        error_code = ERROR_INTERNAL_ERROR
    elif outcome.timed_out:
        error_code = (
            ERROR_CREATE_TIMEOUT if stage is ProbeStage.CREATE else ERROR_RESUME_TIMEOUT
        )
    elif outcome.unavailable:
        error_code = ERROR_CLI_UNAVAILABLE
    elif outcome.output_exceeded_limit or outcome.reader_incomplete:
        error_code = _stream_error_code(stage, ParseError.MALFORMED_JSONL)
    elif outcome.exit_code != 0:
        error_code = (
            ERROR_CREATE_NONZERO_EXIT
            if stage is ProbeStage.CREATE
            else ERROR_RESUME_NONZERO_EXIT
        )
    elif stream.error is not None:
        error_code = _stream_error_code(stage, stream.error)
    elif stream.terminal_event != TURN_COMPLETED_EVENT:
        error_code = (
            ERROR_CREATE_INVALID_TERMINAL
            if stage is ProbeStage.CREATE
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
        cleanup_failed=outcome.cleanup_failed,
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


def _configured_codex_home() -> Path:
    """Mirror runtime_paths.codex_home without executing a script by path."""
    override = os.environ.get(CODEX_HOME_ENV_KEY)
    return Path(override).expanduser() if override else Path.home() / ".codex"


def _shared_codex_home() -> Path:
    path = _configured_codex_home()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _codex_home(checkout: Path) -> Path | None:
    try:
        path = _shared_codex_home()
        resolved = path.resolve()
        resolved_session_artifacts = (resolved / CODEX_SESSION_ARTIFACT_DIR).resolve()
    except (OSError, RuntimeError):
        return None
    if _is_within(resolved, checkout) or _is_within(
        resolved_session_artifacts, checkout
    ):
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
    allowed_keys = _CHILD_ENV_KEYS if include_codex_auth else _BASE_CHILD_ENV_KEYS
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
            "GIT_CONFIG_NOSYSTEM": "1",
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
    _record_unhandled_exception_progress(cli_version=cli_version, model=model)
    git_init = _run_process(["git", "init", "--quiet"], cwd=worktree, env=git_env)
    if git_init.reader_failed or git_init.cleanup_failed:
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
        stage=ProbeStage.CREATE,
    )
    create_summary = _create_summary(create)
    _record_unhandled_exception_progress(
        cli_version=cli_version,
        model=model,
        session_id=create.stream.session_id,
        create=create_summary,
    )
    if create.error_code is not None:
        return _result(
            STATUS_FAILED,
            cli_version=cli_version,
            model=model,
            session_id=create.stream.session_id,
            create=create_summary,
            error_code=create.error_code,
        )
    # _attempt maps missing IDs to create_missing_thread before returning success.
    session_id = cast("str", create.stream.session_id)

    resume = _attempt(
        _resume_argv(session_id, model),
        cwd=worktree,
        env=codex_env,
        stage=ProbeStage.RESUME,
    )
    resume_summary = _resume_summary(resume, session_id)
    _record_unhandled_exception_progress(
        cli_version=cli_version,
        model=model,
        session_id=session_id,
        create=create_summary,
        resume=resume_summary,
    )
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
    if outcome.cleanup_failed:
        return _VersionFailure(ERROR_INTERNAL_ERROR)
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
    sys.stdout.write(
        json.dumps(_UNHANDLED_EXCEPTION_DIAGNOSTIC.result, separators=(",", ":")) + "\n"
    )


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
        _set_unhandled_exception_code(error_code)
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
    _reset_unhandled_exception_result()
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
    parser = _SanitizedArgumentParser(
        description="Run the Codex session smoke probe.", add_help=False
    )
    parser.add_argument("--model", help="Explicit Codex model identifier.")
    try:
        model = parser.parse_args(argv).model
    except _ArgumentParseError:
        model = None
    _reset_unhandled_exception_result()
    failure_result = _result(
        STATUS_FAILED,
        cli_version=None,
        model=None,
        error_code=ERROR_REPO_SETUP_FAILED,
    )
    result = failure_result
    with _install_parent_signal_handlers(_raise_for_parent_signal):
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
