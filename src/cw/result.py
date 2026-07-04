"""Sentinel validation utilities for cw result subcommands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state, save_state, sessions_lock


def _format_errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


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
    """Validate AutoDevResult sentinels before emission.

    Pre-emit gate: validates the inner JSON object (NOT the <<<AUTO_DEV_RESULT>>>
    framed block) against the authoritative AutoDevResult schema.

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

    try:
        result_obj = AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        for line in _format_errors(exc):
            click.echo(line, err=True)
        raise click.exceptions.Exit(1) from exc

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

    try:
        result_obj = AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        for line in _format_errors(exc):
            click.echo(line, err=True)
        click.echo("No session state was modified.", err=True)
        raise click.exceptions.Exit(1) from exc

    resolved_id = _resolve_emit_session_id(session_id)

    with sessions_lock():
        state = load_state()
        session = state.find_by_name_or_id(resolved_id)
        if session is None:
            click.echo(
                f"Session '{resolved_id}' not found; no state was modified.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        session.last_result = result_obj.model_dump(mode="json")
        save_state(state)

    click.echo(f"Recorded result for session {session.id}: status={result_obj.status}")
