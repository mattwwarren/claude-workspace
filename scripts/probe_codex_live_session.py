"""Opt-in Codex CLI create/resume smoke probe."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

FIXED_PROMPT = "Reply exactly cw-session-smoke-ok. Do not use tools or modify files."
FIXED_RESUME_PROMPT = (
    "Reply exactly cw-session-smoke-resumed-ok. Do not use tools or modify files."
)
PROCESS_TIMEOUT_SECONDS = 120
MAX_VERSION_LENGTH = 64
_VERSION_RE = re.compile(r"^codex-cli [0-9]+\.[0-9]+\.[0-9]+$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

type ParseError = Literal[
    "malformed_jsonl",
    "missing_thread",
    "invalid_thread_id",
    "duplicate_thread_started",
    "invalid_terminal",
]
type TerminalEvent = Literal["turn.completed", "turn.failed"]
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
    elif event_type in ("turn.completed", "turn.failed"):
        if not state.thread_seen:
            error = "missing_thread"
        else:
            state.terminal_event = cast("TerminalEvent", event_type)
    elif event_type.startswith("turn.") and event_type != "turn.started":
        error = "invalid_terminal"
    return error


def _parse_line(line: str, state: _StreamState) -> ParseError | None:
    event, error = _decode_event(line)
    if error is None and event is not None:
        event_type = cast("str", event["type"])
        error = (
            _handle_thread(event, state)
            if event_type == "thread.started"
            else _handle_other(event_type, state)
        )
    return error


def _parse_stream(stdout: str) -> ParsedStream:
    """Strictly validate a Codex JSONL stream without retaining payloads."""
    state = _StreamState()
    for line in stdout.splitlines():
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


def _run_process(argv: list[str], *, cwd: Path | None) -> ProcessOutcome:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ProcessOutcome(None, "", True)
    except (FileNotFoundError, OSError):
        return ProcessOutcome(None, "", False, unavailable=True)
    return ProcessOutcome(completed.returncode, completed.stdout, False)


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
    argv: list[str], *, cwd: Path, stage: Literal["create", "resume"]
) -> _Attempt:
    outcome = _run_process(argv, cwd=cwd)
    stream = _parse_stream(outcome.stdout)
    error_code: ErrorCode | None = None
    if outcome.timed_out:
        error_code = "create_timeout" if stage == "create" else "resume_timeout"
    elif outcome.exit_code != 0:
        error_code = (
            "create_nonzero_exit" if stage == "create" else "resume_nonzero_exit"
        )
    elif stream.error is not None:
        error_code = _stream_error_code(stage, stream.error)
    elif stream.terminal_event != "turn.completed":
        error_code = (
            "create_invalid_terminal"
            if stage == "create"
            else "resume_invalid_terminal"
        )
    return _Attempt(outcome, stream, error_code)


def _stream_error_code(
    stage: Literal["create", "resume"], error: ParseError
) -> ErrorCode:
    prefix = "create_" if stage == "create" else "resume_"
    return cast("ErrorCode", prefix + error)


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


def _run_session(*, worktree: Path, cli_version: str, model: str) -> SmokeResult:
    git_init = _run_process(["git", "init", "--quiet"], cwd=worktree)
    if git_init.unavailable or git_init.timed_out or git_init.exit_code != 0:
        return _result(
            "failed",
            cli_version=cli_version,
            model=model,
            error_code="repo_setup_failed",
        )

    create = _attempt(
        [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--model",
            model,
            FIXED_PROMPT,
        ],
        cwd=worktree,
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
        [
            "codex",
            "exec",
            "resume",
            session_id,
            "--json",
            "--ignore-user-config",
            "--model",
            model,
            FIXED_RESUME_PROMPT,
        ],
        cwd=worktree,
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


def _version_result(model: str) -> tuple[str | None, SmokeResult | None]:
    outcome = _run_process(["codex", "--version"], cwd=None)
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


def _run_disposable(*, cli_version: str, model: str) -> SmokeResult:
    try:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="cw-codex-live-session-"
        )
    except OSError:
        return _result(
            "failed",
            cli_version=cli_version,
            model=model,
            error_code="repo_setup_failed",
        )
    result = _result(
        "failed", cli_version=cli_version, model=model, error_code="repo_setup_failed"
    )
    try:
        result = _run_session(
            worktree=Path(temporary_directory.name),
            cli_version=cli_version,
            model=model,
        )
    finally:
        try:
            temporary_directory.cleanup()
        except OSError:
            result = _result(
                "failed",
                cli_version=cli_version,
                model=model,
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
    if model is None or _MODEL_RE.fullmatch(model) is None:
        return _result(
            "failed", cli_version=None, model=None, error_code="invalid_model"
        )
    cli_version, failure = _version_result(model)
    if failure is not None:
        return failure
    if cli_version is None:
        return _result(
            "failed", cli_version=None, model=model, error_code="version_unavailable"
        )
    return _run_disposable(cli_version=cli_version, model=model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex session smoke probe.")
    parser.add_argument("--model", help="Explicit Codex model identifier.")
    result = run_probe(parser.parse_args(argv).model)
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0 if result["status"] in {"skipped", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
