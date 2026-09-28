"""Opt-in Codex CLI create/resume smoke probe."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypedDict, cast

FIXED_PROMPT = "Reply exactly cw-session-smoke-ok. Do not use tools or modify files."
FIXED_RESUME_PROMPT = (
    "Reply exactly cw-session-smoke-resumed-ok. Do not use tools or modify files."
)
PROCESS_TIMEOUT_SECONDS = 120
MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024
PROCESS_OUTPUT_CHUNK_BYTES = 64 * 1024
MAX_VERSION_LENGTH = 64
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

_PARSE_ERROR_CODES: dict[tuple[ProbeStage, ParseError], ErrorCode] = {
    ("create", "malformed_jsonl"): "create_malformed_jsonl",
    ("create", "missing_thread"): "create_missing_thread",
    ("create", "invalid_thread_id"): "create_invalid_thread_id",
    ("create", "duplicate_thread_started"): "create_duplicate_thread_started",
    ("create", "invalid_terminal"): "create_invalid_terminal",
    ("resume", "malformed_jsonl"): "resume_malformed_jsonl",
    ("resume", "missing_thread"): "resume_missing_thread",
    ("resume", "invalid_thread_id"): "resume_invalid_thread_id",
    ("resume", "duplicate_thread_started"): "resume_duplicate_thread_started",
    ("resume", "invalid_terminal"): "resume_invalid_terminal",
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
    status: Literal["skipped", "passed", "failed"]
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


@dataclass
class _OutputCapture:
    data: bytearray
    exceeded_limit: bool = False


@dataclass
class _StreamState:
    session_id: str | None = None
    terminal_event: TerminalEvent | None = None
    thread_seen: bool = False


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


def _kill_process(process: subprocess.Popen[bytes]) -> None:
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
                    _kill_process(process)
                elif not capture.exceeded_limit:
                    capture.data.extend(chunk)
        except OSError:
            capture.exceeded_limit = True
            _kill_process(process)

    reader = threading.Thread(target=drain_stdout, daemon=True)
    reader.start()
    return reader, capture


def _finish_stdout_capture(
    process: subprocess.Popen[bytes],
    reader: threading.Thread,
    capture: _OutputCapture,
) -> str:
    reader.join()
    if process.stdout is not None:
        with contextlib.suppress(OSError):
            process.stdout.close()
    return bytes(capture.data).decode("utf-8", errors="replace")


def _run_process(argv: list[str], *, cwd: Path, env: dict[str, str]) -> ProcessOutcome:
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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
        _kill_process(process)
        try:
            exit_code = process.wait()
        except OSError:
            exit_code = None
            unavailable = True
    except OSError:
        exit_code = None
        unavailable = True
        _kill_process(process)
    stdout = _finish_stdout_capture(process, reader, capture)
    return ProcessOutcome(
        exit_code,
        stdout,
        timed_out,
        unavailable=unavailable,
        output_exceeded_limit=capture.exceeded_limit,
    )


def _result(
    status: Literal["skipped", "passed", "failed"],
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
    if outcome.output_exceeded_limit:
        stream = ParsedStream(stream.session_id, None, "malformed_jsonl")
    error_code: ErrorCode | None = None
    if outcome.timed_out:
        error_code = "create_timeout" if stage == "create" else "resume_timeout"
    elif outcome.unavailable:
        error_code = "cli_unavailable"
    elif outcome.output_exceeded_limit:
        error_code = _stream_error_code(stage, "malformed_jsonl")
    elif outcome.exit_code != 0:
        error_code = (
            "create_nonzero_exit" if stage == "create" else "resume_nonzero_exit"
        )
    elif stream.error is not None:
        error_code = _stream_error_code(stage, stream.error)
    elif stream.terminal_event != TURN_COMPLETED_EVENT:
        error_code = (
            "create_invalid_terminal"
            if stage == "create"
            else "resume_invalid_terminal"
        )
    # Retain only the parsed summary: the create transcript must be gone before
    # the resume subprocess starts.
    compact_outcome = ProcessOutcome(
        outcome.exit_code,
        "",
        outcome.timed_out,
        unavailable=outcome.unavailable,
        output_exceeded_limit=outcome.output_exceeded_limit,
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
        "codex",
        "exec",
        *_common_exec_flags(model, after_json=("--sandbox", "read-only")),
        FIXED_PROMPT,
    ]


def _resume_argv(session_id: str, model: str) -> list[str]:
    return [
        "codex",
        "exec",
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
    except (OSError, RuntimeError):
        return None
    return None if _is_within(resolved, checkout) else resolved


def _child_environment(*, cwd: Path, scratch: Path, codex_home: Path) -> dict[str, str]:
    """Preserve auth/config while removing ambient Git and temp path routing."""
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("GIT_"):
            del environment[key]
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "PWD": str(cwd),
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
        }
    )
    return environment


def _run_session(
    *, worktree: Path, env: dict[str, str], cli_version: str, model: str
) -> SmokeResult:
    git_init = _run_process(["git", "init", "--quiet"], cwd=worktree, env=env)
    if git_init.unavailable or git_init.timed_out or git_init.exit_code != 0:
        return _result(
            "failed",
            cli_version=cli_version,
            model=model,
            error_code="repo_setup_failed",
        )

    create = _attempt(
        _create_argv(model),
        cwd=worktree,
        env=env,
        stage="create",
    )
    create_summary = _create_summary(create)
    if create.error_code is not None:
        return _result(
            "failed",
            cli_version=cli_version,
            model=model,
            session_id=create.stream.session_id,
            create=create_summary,
            error_code=create.error_code,
        )
    session_id = create.stream.session_id
    if session_id is None:
        return _result(
            "failed",
            cli_version=cli_version,
            model=model,
            create=create_summary,
            error_code="create_missing_thread",
        )

    resume = _attempt(
        _resume_argv(session_id, model),
        cwd=worktree,
        env=env,
        stage="resume",
    )
    resume_summary = _resume_summary(resume, session_id)
    if resume.error_code is not None:
        return _result(
            "failed",
            cli_version=cli_version,
            model=model,
            session_id=session_id,
            create=create_summary,
            resume=resume_summary,
            error_code=resume.error_code,
        )
    if not resume_summary["id_matches"]:
        return _result(
            "failed",
            cli_version=cli_version,
            model=model,
            session_id=session_id,
            create=create_summary,
            resume=resume_summary,
            error_code="resume_id_mismatch",
        )
    return _result(
        "passed",
        cli_version=cli_version,
        model=model,
        session_id=session_id,
        create=create_summary,
        resume=resume_summary,
        error_code=None,
    )


def _version_result(
    model: str, *, cwd: Path, env: dict[str, str]
) -> tuple[str | None, SmokeResult | None]:
    outcome = _run_process(["codex", "--version"], cwd=cwd, env=env)
    if outcome.unavailable:
        return None, _result(
            "failed", cli_version=None, model=model, error_code="cli_unavailable"
        )
    if outcome.timed_out or outcome.exit_code != 0:
        return None, _result(
            "failed", cli_version=None, model=model, error_code="version_unavailable"
        )
    version = _parse_version(outcome.stdout)
    if version is None:
        return None, _result(
            "failed", cli_version=None, model=model, error_code="version_invalid"
        )
    return version, None


def _run_disposable(*, model: str) -> SmokeResult:
    try:
        checkout = _checkout_root()
        codex_home = _codex_home(checkout)
        temp_parent = (Path.home() / ".cache" / "cw-live-tests").resolve()
        if codex_home is None or _is_within(temp_parent, checkout):
            return _result(
                "failed",
                cli_version=None,
                model=model,
                error_code="repo_setup_failed",
            )
        temp_parent.mkdir(parents=True, exist_ok=True)
        temp_parent = temp_parent.resolve()
        if _is_within(temp_parent, checkout):
            return _result(
                "failed",
                cli_version=None,
                model=model,
                error_code="repo_setup_failed",
            )
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="cw-codex-live-session-", dir=str(temp_parent)
        )
    except (OSError, RuntimeError, ValueError):
        return _result(
            "failed",
            cli_version=None,
            model=model,
            error_code="repo_setup_failed",
        )
    result = _result(
        "failed", cli_version=None, model=model, error_code="repo_setup_failed"
    )
    cli_version: str | None = None
    try:
        temp_root = Path(temporary_directory.name).resolve()
        if _is_within(temp_root, checkout) or _is_within(codex_home, temp_root):
            result = _result(
                "failed",
                cli_version=None,
                model=model,
                error_code="repo_setup_failed",
            )
        else:
            worktree = temp_root / "repo"
            scratch = temp_root / "tmp"
            worktree.mkdir()
            scratch.mkdir()
            env = _child_environment(
                cwd=worktree, scratch=scratch, codex_home=codex_home
            )
            cli_version, failure = _version_result(model, cwd=worktree, env=env)
            if failure is not None:
                result = failure
            elif cli_version is None:
                result = _result(
                    "failed",
                    cli_version=None,
                    model=model,
                    error_code="version_unavailable",
                )
            else:
                result = _run_session(
                    worktree=worktree,
                    env=env,
                    cli_version=cli_version,
                    model=model,
                )
    except (OSError, RuntimeError):
        result = _result(
            "failed",
            cli_version=cli_version,
            model=model,
            error_code="repo_setup_failed",
        )
    finally:
        try:
            temporary_directory.cleanup()
        except OSError:
            result = _result(
                "failed",
                cli_version=result["cli_version"],
                model=result["model"],
                session_id=result["session_id"],
                create=result["create"],
                resume=result["resume"],
                error_code="cleanup_failed",
            )
    return result


def run_probe(model: str | None) -> SmokeResult:
    """Run the opt-in probe and return its sanitized result."""
    if os.environ.get("CW_CODEX_LIVE_SESSION_SMOKE") != "1":
        return _result(
            "skipped",
            cli_version=None,
            model=None,
            error_code="opt_in_required",
        )
    if (
        model is None
        or model.startswith("-")
        or model in _FORBIDDEN_MODEL_VALUES
        or _MODEL_RE.fullmatch(model) is None
    ):
        return _result(
            "failed", cli_version=None, model=None, error_code="invalid_model"
        )
    return _run_disposable(model=model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex session smoke probe.")
    parser.add_argument("--model", help="Explicit Codex model identifier.")
    result = run_probe(parser.parse_args(argv).model)
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0 if result["status"] in {"skipped", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
