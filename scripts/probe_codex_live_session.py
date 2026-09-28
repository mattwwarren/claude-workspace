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
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn, TypedDict, cast

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
CODEX_EXECUTABLE: Final = "codex"
CODEX_EXEC_SUBCOMMAND: Final = "exec"
CODEX_VERSION_FLAG: Final = "--version"
CODEX_SESSION_ARTIFACT_DIR: Final = "sessions"
STATUS_SKIPPED: Final = "skipped"
STATUS_PASSED: Final = "passed"
STATUS_FAILED: Final = "failed"
ERROR_OPT_IN_REQUIRED: Final = "opt_in_required"
ERROR_INVALID_MODEL: Final = "invalid_model"
ERROR_VERSION_UNAVAILABLE: Final = "version_unavailable"
ERROR_VERSION_INVALID: Final = "version_invalid"
ERROR_CLI_UNAVAILABLE: Final = "cli_unavailable"
ERROR_REPO_SETUP_FAILED: Final = "repo_setup_failed"
ERROR_CREATE_TIMEOUT: Final = "create_timeout"
ERROR_CREATE_NONZERO_EXIT: Final = "create_nonzero_exit"
ERROR_CREATE_MALFORMED_JSONL: Final = "create_malformed_jsonl"
ERROR_CREATE_MISSING_THREAD: Final = "create_missing_thread"
ERROR_CREATE_INVALID_THREAD_ID: Final = "create_invalid_thread_id"
ERROR_CREATE_DUPLICATE_THREAD_STARTED: Final = "create_duplicate_thread_started"
ERROR_CREATE_INVALID_TERMINAL: Final = "create_invalid_terminal"
ERROR_RESUME_TIMEOUT: Final = "resume_timeout"
ERROR_RESUME_NONZERO_EXIT: Final = "resume_nonzero_exit"
ERROR_RESUME_MALFORMED_JSONL: Final = "resume_malformed_jsonl"
ERROR_RESUME_MISSING_THREAD: Final = "resume_missing_thread"
ERROR_RESUME_INVALID_THREAD_ID: Final = "resume_invalid_thread_id"
ERROR_RESUME_DUPLICATE_THREAD_STARTED: Final = "resume_duplicate_thread_started"
ERROR_RESUME_ID_MISMATCH: Final = "resume_id_mismatch"
ERROR_RESUME_INVALID_TERMINAL: Final = "resume_invalid_terminal"
ERROR_CLEANUP_FAILED: Final = "cleanup_failed"
_VERSION_RE = re.compile(r"^codex-cli [0-9]+\.[0-9]+\.[0-9]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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

type ParseError = Literal[
    "malformed_jsonl",
    "missing_thread",
    "invalid_thread_id",
    "duplicate_thread_started",
    "invalid_terminal",
]
type TerminalEvent = Literal["turn.completed", "turn.failed"]
type ProbeStage = Literal["create", "resume"]
# Keep literals in type aliases; mypy does not expand Final string constants here.
type ErrorCode = Literal[
    "opt_in_required",
    "invalid_model",
    "version_unavailable",
    "version_invalid",
    "cli_unavailable",
    "repo_setup_failed",
    "create_timeout",
    "create_nonzero_exit",
    "create_malformed_jsonl",
    "create_missing_thread",
    "create_invalid_thread_id",
    "create_duplicate_thread_started",
    "create_invalid_terminal",
    "resume_timeout",
    "resume_nonzero_exit",
    "resume_malformed_jsonl",
    "resume_missing_thread",
    "resume_invalid_thread_id",
    "resume_duplicate_thread_started",
    "resume_id_mismatch",
    "resume_invalid_terminal",
    "cleanup_failed",
]
type ProbeStatus = Literal["skipped", "passed", "failed"]

_PARSE_ERROR_CODES: dict[tuple[ProbeStage, ParseError], ErrorCode] = {
    ("create", "malformed_jsonl"): ERROR_CREATE_MALFORMED_JSONL,
    ("create", "missing_thread"): ERROR_CREATE_MISSING_THREAD,
    ("create", "invalid_thread_id"): ERROR_CREATE_INVALID_THREAD_ID,
    ("create", "duplicate_thread_started"): ERROR_CREATE_DUPLICATE_THREAD_STARTED,
    ("create", "invalid_terminal"): ERROR_CREATE_INVALID_TERMINAL,
    ("resume", "malformed_jsonl"): ERROR_RESUME_MALFORMED_JSONL,
    ("resume", "missing_thread"): ERROR_RESUME_MISSING_THREAD,
    ("resume", "invalid_thread_id"): ERROR_RESUME_INVALID_THREAD_ID,
    ("resume", "duplicate_thread_started"): ERROR_RESUME_DUPLICATE_THREAD_STARTED,
    ("resume", "invalid_terminal"): ERROR_RESUME_INVALID_TERMINAL,
}
_TERMINAL_EVENTS: frozenset[TerminalEvent] = frozenset(
    {TURN_COMPLETED_EVENT, TURN_FAILED_EVENT}
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


@dataclass
class _OutputCapture:
    data: bytearray
    exceeded_limit: bool = False


@dataclass
class _StreamState:
    session_id: str | None = None
    terminal_event: TerminalEvent | None = None
    thread_seen: bool = False


@dataclass(frozen=True)
class _VersionSuccess:
    cli_version: str


@dataclass(frozen=True)
class _VersionFailure:
    error_code: Literal[
        "cli_unavailable",
        "version_unavailable",
        "version_invalid",
    ]


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
        return None, "malformed_jsonl"
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None, "malformed_jsonl"
    return cast("dict[str, object]", event), None


def _handle_thread(event: dict[str, object], state: _StreamState) -> ParseError | None:
    if state.thread_seen:
        return "duplicate_thread_started"
    session_id = event.get("thread_id")
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        return "invalid_thread_id"
    state.session_id = session_id
    state.thread_seen = True
    return None


def _handle_other(event_type: str, state: _StreamState) -> ParseError | None:
    error: ParseError | None = None
    if state.terminal_event is not None:
        error = "invalid_terminal"
    elif event_type in _TERMINAL_EVENTS:
        if not state.thread_seen:
            error = "missing_thread"
        else:
            state.terminal_event = event_type
    elif event_type.startswith("turn.") and event_type != TURN_STARTED_EVENT:
        error = "invalid_terminal"
    return error


def _parse_line(line: str, state: _StreamState) -> ParseError | None:
    event, error = _decode_event(line)
    if error is None and event is not None:
        event_type = cast("str", event["type"])
        error = (
            _handle_thread(event, state)
            if event_type == THREAD_STARTED_EVENT
            else _handle_other(event_type, state)
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
        return ParsedStream(state.session_id, state.terminal_event, "missing_thread")
    if state.terminal_event is None:
        return ParsedStream(state.session_id, state.terminal_event, "invalid_terminal")
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


def _start_stdout_reader(
    process: subprocess.Popen[bytes],
) -> tuple[threading.Thread, _OutputCapture]:
    capture = _OutputCapture(bytearray())

    def drain_stdout() -> None:
        if process.stdout is None:
            capture.exceeded_limit = True
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
                elif not capture.exceeded_limit:
                    capture.data.extend(chunk)
        except (OSError, ValueError):
            capture.exceeded_limit = True
            _kill_process_group(process)

    reader = threading.Thread(target=drain_stdout, daemon=True)
    reader.start()
    return reader, capture


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
        with contextlib.suppress(OSError):
            process.stdout.close()
    reader.join(timeout=PROCESS_READER_JOIN_TIMEOUT_SECONDS)
    return (
        bytes(capture.data).decode("utf-8", errors="replace"),
        reader_incomplete or reader.is_alive(),
    )


def _run_process(argv: list[str], *, cwd: Path, env: dict[str, str]) -> ProcessOutcome:
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
    except (FileNotFoundError, OSError):
        return ProcessOutcome(None, "", False, unavailable=True)

    reader, capture = _start_stdout_reader(process)
    timed_out = False
    unavailable = False
    try:
        exit_code = process.wait(timeout=PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        try:
            exit_code = process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            _kill_process_group(process)
            exit_code = None
            unavailable = True
    except OSError:
        exit_code = None
        unavailable = True
        _kill_process_group(process)
    stdout, reader_incomplete = _finish_stdout_capture(process, reader, capture)
    return ProcessOutcome(
        exit_code,
        stdout,
        timed_out,
        unavailable=unavailable,
        output_exceeded_limit=capture.exceeded_limit,
        reader_incomplete=reader_incomplete,
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
    stream = _parse_stream(outcome.stdout)
    if outcome.output_exceeded_limit or outcome.reader_incomplete:
        stream = ParsedStream(stream.session_id, None, "malformed_jsonl")
    error_code: ErrorCode | None = None
    if outcome.timed_out:
        error_code = ERROR_CREATE_TIMEOUT if stage == "create" else ERROR_RESUME_TIMEOUT
    elif outcome.unavailable:
        error_code = ERROR_CLI_UNAVAILABLE
    elif outcome.output_exceeded_limit or outcome.reader_incomplete:
        error_code = _stream_error_code(stage, "malformed_jsonl")
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


def _codex_home(checkout: Path) -> Path | None:
    configured = os.environ.get("CODEX_HOME")
    path = Path(configured).expanduser() if configured else Path.home() / ".codex"
    if not path.is_absolute():
        path = Path.cwd() / path
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
        if key.casefold() in allowed_keys or key.casefold().startswith("lc_")
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
        environment["CODEX_HOME"] = str(codex_home)
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
    if git_init.unavailable or git_init.timed_out or git_init.exit_code != 0:
        return _result(
            STATUS_FAILED,
            cli_version=cli_version,
            model=model,
            error_code=ERROR_REPO_SETUP_FAILED,
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
    if outcome.unavailable:
        return _VersionFailure(ERROR_CLI_UNAVAILABLE)
    if outcome.timed_out or outcome.exit_code != 0:
        return _VersionFailure(ERROR_VERSION_UNAVAILABLE)
    version = None if outcome.reader_incomplete else _parse_version(outcome.stdout)
    if version is None:
        return _VersionFailure(ERROR_VERSION_INVALID)
    return _VersionSuccess(version)


def _run_disposable(*, model: str) -> SmokeResult:
    try:
        checkout = _checkout_root()
        codex_home = _codex_home(checkout)
        temp_parent = (Path.home() / ".cache" / "cw-live-tests").resolve()
        if codex_home is None or _is_within(temp_parent, checkout):
            return _result(
                STATUS_FAILED,
                cli_version=None,
                model=model,
                error_code=ERROR_REPO_SETUP_FAILED,
            )
        temp_parent.mkdir(parents=True, exist_ok=True)
        temp_parent = temp_parent.resolve()
        if _is_within(temp_parent, checkout):
            return _result(
                STATUS_FAILED,
                cli_version=None,
                model=model,
                error_code=ERROR_REPO_SETUP_FAILED,
            )
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="cw-codex-live-session-", dir=str(temp_parent)
        )
    except (OSError, RuntimeError, ValueError):
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
        temp_root = Path(temporary_directory.name).resolve()
        if _is_within(temp_root, checkout) or _is_within(codex_home, temp_root):
            result = _result(
                STATUS_FAILED,
                cli_version=None,
                model=model,
                error_code=ERROR_REPO_SETUP_FAILED,
            )
        else:
            worktree = temp_root / "repo"
            scratch = temp_root / "tmp"
            worktree.mkdir()
            scratch.mkdir()
            codex_env = _child_environment(
                cwd=worktree, scratch=scratch, codex_home=codex_home
            )
            git_env = _child_environment(
                cwd=worktree,
                scratch=scratch,
                codex_home=None,
                include_codex_auth=False,
            )
            version_outcome = _version_result(cwd=worktree, env=codex_env)
            if isinstance(version_outcome, _VersionFailure):
                result = _result(
                    STATUS_FAILED,
                    cli_version=None,
                    model=model,
                    error_code=version_outcome.error_code,
                )
            else:
                result = _run_session(
                    worktree=worktree,
                    codex_env=codex_env,
                    git_env=git_env,
                    cli_version=version_outcome.cli_version,
                    model=model,
                )
    except (OSError, RuntimeError):
        result = _result(
            STATUS_FAILED,
            cli_version=None,
            model=model,
            error_code=ERROR_REPO_SETUP_FAILED,
        )
    finally:
        try:
            temporary_directory.cleanup()
        except OSError:
            result = _result(
                STATUS_FAILED,
                cli_version=result["cli_version"],
                model=result["model"],
                session_id=result["session_id"],
                create=result["create"],
                resume=result["resume"],
                error_code=ERROR_CLEANUP_FAILED,
            )
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
    result = run_probe(model)
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0 if result["status"] in {STATUS_SKIPPED, STATUS_PASSED} else 1


if __name__ == "__main__":
    raise SystemExit(main())
