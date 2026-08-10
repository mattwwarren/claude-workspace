"""OpenCode subprocess runner for OpencodeExecutor (#1669).

Parallel to local_runner.py's AiderRunner seam. OpencodeExecutor delegates
the ``opencode run --format json`` invocation to an OpencodeRunner so tests
can drive every disposition without spawning a real subprocess.

Fire-and-forget: like AiderRunner, launch() returns a live Popen and the
caller captures PID + start-time as a LocalLivenessHandle. The opencode run
completes asynchronously; reconcile/local harvest detects the dead process
and parses the JSONL log for the ``<<<AUTO_DEV_RESULT>>>`` sentinel.

opencode has no ``--output-schema`` (unlike codex); the result travels as
free-form text in ``text`` event payloads, harvested via the sentinel pattern
(#1669 R3, probe-confirmed).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from cw.auto_dev_result import (
    AutoDevResult,
    Blocker,
    Health,
    Review,
    Scope,
    StageReached,
)
from cw.auto_dev_result.parse import parse_stdout
from cw.executor_diagnostics import (
    append_diagnostics_pointer,
    build_executor_failure,
    persist_diagnostics_bundle,
)

if TYPE_CHECKING:
    from cw.models import TicketTask

_SCHEMA_VERSION: Literal[4] = 4

OPENCODE_LOG_RELATIVE_PATH: Path = Path(".cw", "opencode.log")
_OPENCODE_LOG_TAIL_CHARS = 4000  # matches local_runner's _AIDER_LOG_TAIL_CHARS

OPENCODE_NOT_FOUND = "opencode_not_found"
OPENCODE_NO_OUTPUT = "opencode_no_output"
UNEXPECTED_ERROR = "unexpected_error"
LIVENESS_UNAVAILABLE = "liveness_unavailable"

# The FINALIZE entry-point stage marker (mirrors STAGE3_REVIEW for codex).
# Used as the stage_reached for the stage-block on non-FINALIZE stages (#1670 R5).
STAGE4A_MERGE_GATE: StageReached = "stage4a_merge_gate"

_blocked_scope = Scope(
    tier="small",
    files=0,
    lines_estimate=0,
    lines_actual=0,
    forbidden_touched=False,
)
_FIXED_HEALTH = Health(
    lowest_agent_confidence="LOW",
    any_incomplete_risk=True,
    recommendation="EXIT_FOR_HUMAN_REVIEW",
)
_FIXED_REVIEW = Review(must_fix_initial=0, should_fix=0, fix_cycles_used=0)
_FIXED_NEXT_ACTIONS: list[str] = ["user_resolve_opencode_executor_failure"]

_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "LOGNAME",
        "SHELL",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
    }
)


def opencode_available() -> bool:
    """Return True if the opencode binary is on PATH."""
    return shutil.which("opencode") is not None


@runtime_checkable
class OpencodeRunner(Protocol):
    """Testability seam for the opencode subprocess launch (#1669).

    Mirrors AiderRunner in local_runner.py — fire-and-forget: the caller does
    NOT wait, it captures the PID + start-time as a liveness handle and returns
    immediately. reconcile/local harvest later detects the dead process and
    parses the JSONL log for the sentinel.
    """

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        """Fire-and-forget spawn of the opencode process; return the live Popen."""
        ...


class RealOpencodeRunner:
    """Production implementation: launches opencode as a detached subprocess.

    Mirrors RealAiderRunner — redirects stdout to a per-worktree log file
    (``.cw/opencode.log``) for the harvest path to parse. Truncated ("w") on
    every call so a retry into the same worktree does not bleed a prior
    attempt's output into the next harvest read.
    """

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w") as log_file:
            return subprocess.Popen(
                argv,
                env=env,
                cwd=worktree,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )


class FakeOpencodeRunner:
    """Test double: records the launch call; returns a real live subprocess.

    Mirrors FakeAiderRunner in local_runner.py. Returns
    ``Popen(["sleep", "60"])`` rather than a fast-exiting process so the
    caller's ``read_process_start_time_ns`` lookup does not race a just-exited
    PID. Spawned processes are tracked in ``self.procs`` so tests can kill them.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.procs: list[subprocess.Popen[bytes]] = []

    def launch(
        self,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": worktree,
                "env": dict(env),
            }
        )
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return proc


def build_argv(model: str | None, worktree: Path, prompt: str) -> list[str]:
    """Return the opencode argv for the given model, worktree, and prompt.

    Pins ``--format json`` (event stream for harvest), ``--pure`` (no external
    plugins — mechanical permission profile per #1669 R4), and ``--dir`` (run
    in the worktree). The prompt is the trailing positional, redacted in
    diagnostics by ``redact_argv``.
    """
    argv: list[str] = [
        "opencode",
        "run",
        "--format",
        "json",
        "--pure",
        "--dir",
        str(worktree),
    ]
    if model is not None:
        argv.extend(["--model", model])
    argv.append(prompt)
    return argv


def build_env() -> dict[str, str]:
    """Return the subprocess env dict for opencode.

    Passes only an explicit allowlist of env vars. All operator shell secrets
    (AWS_*, tokens, etc.) are excluded by default. opencode reads its model
    config from its own config file (``~/.config/opencode/opencode.json``),
    not from env vars — no OPENAI_* overrides are needed (unlike aider's
    ``build_env``).
    """
    return {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}


def build_finalize_prompt(ticket_id: str) -> str:
    """Build the opencode prompt for the FINALIZE stage (#1670).

    opencode has no slash-command resolution (unlike ClaudeNativeExecutor's
    ``/auto-dev-finalize <ticket> --headless`` invocation), so the prompt
    inlines the instruction to read and follow the existing
    ``auto-dev-finalize.md`` skill — backend-neutral at the producer level
    (it runs ``gh`` commands, not executor-specific code, per R6). The
    prompt instructs opencode to run the finalize flow (merge-gate → PR
    create → auto-merge → read-back) and emit the ``<<<AUTO_DEV_RESULT>>>``
    sentinel with the correct ``stage_reached`` marker (R1).
    """
    return (
        f"Run the auto-dev FINALIZE stage for ticket {ticket_id}. "
        "Read and follow the instructions in "
        ".claude/commands/auto-dev-finalize.md "
        f"(arguments: {ticket_id} --headless). "
        "The finalize flow runs: merge-gate check, PR creation, "
        "auto-merge enablement, read-back verification. "
        "When complete, emit the <<<AUTO_DEV_RESULT>>> sentinel with "
        "stage_reached set to stage4a_merge_gate, stage4b_pr_create, or "
        "stage5_post_create as appropriate."
    )


def make_blocked(
    *,
    ticket_id: str,
    worktree: Path,
    reason: str,
    details: str = "",
    retry_eligible: bool | None = None,
    retry_delay_seconds: int | None = None,
    stage_reached: StageReached = "stage2_impl",
) -> AutoDevResult:
    """Return a typed blocked AutoDevResult for any OpencodeExecutor failure.

    Mirrors local_runner.make_blocked — opencode-specific ``next_actions`` only.
    """
    return AutoDevResult(
        schema_version=_SCHEMA_VERSION,
        ticket_id=ticket_id,
        status="blocked",
        stage_reached=stage_reached,
        scope=_blocked_scope,
        plan_source="none",
        review=_FIXED_REVIEW,
        health=_FIXED_HEALTH,
        blocker=Blocker(
            stage=stage_reached,
            reason=reason,
            details=details,
            retry_eligible=retry_eligible,
            retry_delay_seconds=retry_delay_seconds,
        ),
        next_actions=_FIXED_NEXT_ACTIONS,
        worktree_path=str(worktree),
    )


def extract_text_from_jsonl(log_content: str) -> str:
    """Parse opencode JSONL events and return concatenated text content.

    opencode's ``--format json`` stream emits events with a ``type`` field.
    Text content lives in ``text`` events:
    ``{"type": "text", "part": {"text": "..."}}``. The sentinel
    (``<<<AUTO_DEV_RESULT>>>``) is embedded in these text events. Returns the
    concatenation of all text event payloads, or empty string if no text events
    are found or the JSONL is unparseable.
    """
    texts: list[str] = []
    for raw_line in log_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "".join(texts)


def _persist_opencode_no_output_diagnostics(*, session_id: str, log_tail: str) -> None:
    """Write a ``missing_output`` diagnostics bundle for an OPENCODE_NO_OUTPUT harvest.

    Mirrors local_runner._persist_aider_no_output_diagnostics. Never raises
    (persist swallows OSError).
    """
    failure = build_executor_failure(
        category="missing_output",
        executor_name="opencode",
        session_id=session_id,
        argv=[],
        stdout_excerpt=log_tail,
        stderr_excerpt="",
    )
    persist_diagnostics_bundle(
        session_id=session_id,
        role_slug="opencode",
        failure=failure,
    )


def synthesize_opencode_result(
    *,
    task: TicketTask,
    worktree: Path,
    session_id: str | None = None,
) -> AutoDevResult:
    """Harvest the opencode JSONL log for a sentinel (#1669).

    Called by reconcile/local harvest AFTER the fire-and-forget opencode process
    has exited. Reads ``.cw/opencode.log``, parses the JSONL event stream,
    extracts text content, and feeds it to ``parse_stdout`` for sentinel
    extraction.

    - sentinel found in text → the parsed ``AutoDevResult``
    - no sentinel / empty log / unparseable → ``OPENCODE_NO_OUTPUT`` (blocked,
      ``retry_eligible``, details from the log tail when readable)

    *session_id* is optional: when set, the ``OPENCODE_NO_OUTPUT`` branch also
    persists a typed ``missing_output`` diagnostics bundle and appends a
    ``[diagnostics: <path>]`` pointer.
    """
    log_path = worktree / OPENCODE_LOG_RELATIVE_PATH
    log_content = ""
    with contextlib.suppress(OSError):
        log_content = log_path.read_text(encoding="utf-8", errors="replace")

    if log_content:
        text = extract_text_from_jsonl(log_content)
        if text:
            result = parse_stdout(text)
            if isinstance(result, AutoDevResult):
                return result

    details = log_content[-_OPENCODE_LOG_TAIL_CHARS:] if log_content else ""
    if session_id is not None:
        _persist_opencode_no_output_diagnostics(session_id=session_id, log_tail=details)
        details = append_diagnostics_pointer(details, session_id=session_id)
    return make_blocked(
        ticket_id=task.ticket_id,
        worktree=worktree,
        reason=OPENCODE_NO_OUTPUT,
        details=details,
        retry_eligible=True,
        retry_delay_seconds=0,
    )
