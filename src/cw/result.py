"""Sentinel validation utilities for cw result subcommands."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state, save_state, sessions_lock
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError

logger = logging.getLogger(__name__)


def _format_errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def _read_json_payload(path: str) -> dict[str, Any]:
    """Read PATH ('-' for stdin) and decode it as JSON.

    Shared by ``result validate`` and ``result emit`` so the two commands'
    I/O shape (positional PATH, ``-`` stdin, ``json:``-prefixed decode errors)
    can't drift apart. On a decode error: echoes ``json: <message>`` to
    stderr and exits 1.
    """
    if path == "-":
        raw = sys.stdin.read()
    else:
        with click.open_file(path, "r") as f:
            raw = f.read()

    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        click.echo(f"json: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
    return payload


def _validate_or_exit(
    payload: dict[str, Any], *, extra_stderr_line: str | None = None
) -> AutoDevResult:
    """Validate PAYLOAD against AutoDevResult, echoing errors and exiting on failure.

    Shared by ``result validate`` and ``result emit`` so their validation-failure
    output (the ``field: message`` lines from :func:`_format_errors`) can't drift
    apart. *extra_stderr_line*, if given, is echoed after the field-error lines
    (``emit`` uses this to note that no state was mutated).
    """
    try:
        return AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        for line in _format_errors(exc):
            click.echo(line, err=True)
        if extra_stderr_line is not None:
            click.echo(extra_stderr_line, err=True)
        raise click.exceptions.Exit(1) from exc


def validate_payload(payload: dict[str, Any]) -> list[str]:
    """Validate a raw AutoDevResult payload dict.

    Returns a list of field-error lines ("field.path: message").
    Empty list means valid.
    """
    try:
        AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        return _format_errors(exc)
    return []


@click.group()
def result() -> None:
    """Validate AutoDevResult sentinels, and emit them onto a session.

    ``validate`` is a pre-emit gate: validates the inner JSON object (NOT the
    <<<AUTO_DEV_RESULT>>> framed block) against the authoritative AutoDevResult
    schema. ``emit`` performs the same validation, then pushes the result onto
    a session's state as the authoritative completion record (see ``cw result
    emit --help``).

    Field rules:
    - schema_version: int -- must be 1-4
    - ticket_id: str
    - status: "shipped" | "no_op" | "plan_pending_approval" |
      "ambiguities_pending_resolution" | "premises_pending_verification" |
      "review_pending_approval" | "merge_gate_blocked" | "scope_exceeded" |
      "forbidden_area" | "blocked"
    - stage_reached: str literal (see cw schema show auto-dev-result for full list)
    - scope: {tier, files, lines_estimate, lines_actual, forbidden_touched}
    - plan_source: "linear_existing" | "github_issue_existing" | "generated" |
      "free_text" | "none"
    - branch: str | null

    Constraints (cross-field invariants):
    - pr: non-null iff status == "shipped"
    - blocker: non-null iff status == "blocked"
    - next_actions contains "wait_for_ci" iff status == "shipped"
    - scope.tier: required when stage_reached not in {stage1_plan, stage1_pre_flight}
    - scope.lines_actual: required when stage_reached not in
      {stage1_plan, stage1_pre_flight}
    - health.lowest_agent_confidence: required when stage_reached not in
      {stage1_plan, stage1_pre_flight}
    - branch: null when status in pre-branch statuses (plan_pending_approval, etc.)
    - health.downgrade_applied: True requires status == "review_pending_approval"
    - schema_version: v2-introduced statuses require schema_version >= 2

    Use 'cw schema show auto-dev-result' for the full schema reference.
    """


@result.command(name="validate")
@click.argument("path")
def result_validate(path: str) -> None:
    """Validate a candidate sentinel JSON against AutoDevResult schema.

    PATH is a file path or '-' for stdin. The payload must be the inner
    JSON object only -- do NOT include the <<<AUTO_DEV_RESULT>>> delimiters.

    On success: exits 0, prints normalized JSON to stdout.
    On failure: exits 1, prints 'field.path: message' lines to stderr.
    """
    payload = _read_json_payload(path)
    result_obj = _validate_or_exit(payload)
    click.echo(result_obj.model_dump_json(indent=2))


def _resolve_emit_session_id(session_id: str | None) -> str:
    """Resolve the target session id for ``result emit``.

    ``--session-id`` wins; otherwise fall back to the ``session_id`` recorded in
    ``<cwd>/.claude/cw-context.json`` (the file dispatch writes into a spawned
    worktree). Unlike the best-effort hook read, a missing/malformed context is a
    loud error here — emit must not silently no-op — so the operator sees exactly
    which path was expected and can pass ``--session-id`` instead.
    """
    if session_id is not None:
        return session_id

    # Function-local import breaks the cw.cli <-> cw.result circular dependency;
    # inline import is the sanctioned mechanism (PLC0415), not a workaround.
    from cw.cli._hook_io import _read_cw_context

    cwd = str(Path.cwd())
    context_path = Path(cwd) / ".claude" / "cw-context.json"
    context = _read_cw_context(cwd)
    if context is None:
        click.echo(
            f"No cw-context.json at {context_path}; pass --session-id explicitly.",
            err=True,
        )
        raise click.exceptions.Exit(1)
    ctx_session_id = context.get("session_id")
    if not isinstance(ctx_session_id, str):
        click.echo(
            f"cw-context.json at {context_path} has no string session_id; "
            "pass --session-id explicitly.",
            err=True,
        )
        raise click.exceptions.Exit(1)
    return ctx_session_id


@dataclass(frozen=True)
class EmitOutcome:
    """Result of a successful emit_result_locked() call.

    Carries exactly what the CLI/log line need to render the current
    'Recorded result for session ...' stdout line and the
    'cw result emit: session=... prior_status=... new_status=...' log line.
    """

    session_id: str
    result: AutoDevResult
    prior_status: str | None


def emit_result_locked(payload: dict[str, Any], session_id: str) -> EmitOutcome:
    """Validate PAYLOAD and record it onto SESSION_ID's last_result.

    Caller MUST already hold sessions_lock(). Extracted from emit_result()
    so an in-process caller that has already acquired the sessions lock can
    invoke the mutation directly without a second acquisition of the same
    flock-based lock, which would self-deadlock (mirrors
    cw.dev_queue.approval._approve_ticket_locked, GitHub #1065).

    Emits no event and performs no task routing -- write-only, matching the
    original cw result emit CLI contract byte-for-byte (RFC 0012 D-A1).

    Raises:
        EmitValidationError: if PAYLOAD fails AutoDevResult validation.
        EmitSessionNotFoundError: if SESSION_ID has no matching session.
    """
    try:
        result_obj = AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        msg = "AutoDevResult payload failed validation"
        raise EmitValidationError(msg, errors=_format_errors(exc)) from exc

    state = load_state()
    session = state.find_by_name_or_id(session_id)
    if session is None:
        msg = f"Session {session_id!r} not found"
        raise EmitSessionNotFoundError(msg, session_id=session_id)

    prior_status: str | None = (
        session.last_result.get("status")
        if isinstance(session.last_result, dict)
        else None
    )
    session.last_result = result_obj.model_dump(mode="json")
    save_state(state)

    return EmitOutcome(
        session_id=session.id, result=result_obj, prior_status=prior_status
    )


def emit_result(payload: dict[str, Any], session_id: str) -> EmitOutcome:
    """Acquire sessions_lock() and record PAYLOAD onto SESSION_ID.

    Thin lock-acquiring wrapper over emit_result_locked() (mirrors
    cw.dev_queue.approval.approve_ticket). Use this from any caller not
    already holding sessions_lock(); use emit_result_locked() directly from
    inside an existing `with sessions_lock():` block to avoid the
    non-reentrant flock deadlocking (see SessionsLockReentryError).
    """
    with sessions_lock():
        return emit_result_locked(payload, session_id)


@result.command(name="emit")
@click.argument("path")
@click.option(
    "--session-id",
    "session_id",
    default=None,
    help="Session id override; wins over cw-context.json.",
)
def result_emit(path: str, session_id: str | None) -> None:
    """Record an AutoDevResult onto its session (push-based completion).

    PATH is a file path or '-' for stdin. The payload must be the inner JSON
    object only -- do NOT include the <<<AUTO_DEV_RESULT>>> delimiters.

    Write-only: validates the payload, resolves the target session
    (``--session-id`` wins, else the ``session_id`` from
    ``<cwd>/.claude/cw-context.json``), and writes ``session.last_result`` under
    the sessions lock. Emits NO event and changes NO session status -- the Stop
    hook remains the sole completion-event source. Validation strictly precedes
    any state read/write, so a bad payload leaves state untouched.

    On success: exits 0, prints
    'Recorded result for session <short_id>: status=<status>'.
    On validation failure: exits 1, prints 'field.path: message' lines plus a
    'No session state was modified.' notice to stderr.
    """
    payload = _read_json_payload(path)
    resolved_id = _resolve_emit_session_id(session_id)

    try:
        outcome = emit_result(payload, resolved_id)
    except EmitValidationError as exc:
        for line in exc.errors:
            click.echo(line, err=True)
        click.echo("No session state was modified.", err=True)
        raise click.exceptions.Exit(1) from exc
    except EmitSessionNotFoundError as exc:
        click.echo(
            f"Session '{exc.session_id}' not found; no state was modified.",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc

    logger.info(
        "cw result emit: session=%s prior_status=%s new_status=%s",
        outcome.session_id,
        outcome.prior_status,
        outcome.result.status,
    )
    click.echo(
        f"Recorded result for session {outcome.session_id}: "
        f"status={outcome.result.status}"
    )
