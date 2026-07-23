"""Sentinel validation utilities for cw result subcommands."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from pydantic import ValidationError

from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.config import load_state, save_state, sessions_lock
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError
from cw.models import LastResultSource

if TYPE_CHECKING:
    from cw.models import Session

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


def has_terminal_result(last_result: dict[str, Any] | None) -> bool:
    """True when LAST_RESULT is an already-emitted terminal sentinel.

    A real AUTO_DEV sentinel dump always carries a ``"status"`` key; the park
    markers (``silently_idle``/``needs_salvage``) carry ``"paused_status"`` and
    no ``"status"``. Key presence -- not value -- is the structural discriminant,
    so a parked session is correctly NOT treated as terminal and the idle
    watchdog re-checks it for a late terminal sentinel. See #418, #497.

    The door (``emit_result_locked``) uses this to arbitrate first-writer-wins
    (RFC 0012 S2, #1456); ``cw.reconcile._shared._has_terminal_sentinel``
    delegates here so both layers share one predicate.
    """
    return isinstance(last_result, dict) and "status" in last_result


@dataclass(frozen=True)
class EmitOutcome:
    """Result of an emit_result_locked() call: either a successful write
    (``result`` non-None, ``refused=False``) or a refusal because a terminal
    result was already recorded (``result=None``, ``refused=True``,
    ``existing_result``/``existing_source`` populated).

    Carries exactly what the CLI/log line need to render the current
    'Recorded result for session ...' stdout line and the
    'cw result emit: session=... prior_status=... new_status=...' log line.
    """

    session_id: str
    result: AutoDevResult | BlockedResult | None
    prior_status: str | None
    refused: bool = False
    existing_result: dict[str, Any] | None = None
    existing_source: LastResultSource | None = None


def _validate_harvest_payload(payload: dict[str, Any]) -> AutoDevResult | BlockedResult:
    """Validate PAYLOAD against the discriminated AutoDevResult/BlockedResult union.

    RFC 0012 A1 (#1457): the Stop-hook harvest write pushes both shapes a
    parsed sentinel can take -- ``parse_stdout`` returns either a full
    ``AutoDevResult`` or a parser-synthesized ``BlockedResult`` (issued on a
    §6 failure mode, e.g. cross-field-invariant failure). The two shapes are
    told apart structurally, not by a schema field: a genuine producer-emitted
    ``AutoDevResult`` with ``status=blocked`` always carries ``schema_version``
    (and every other AutoDevResult field); a synthetic ``BlockedResult`` never
    does. So ``status == "blocked"`` with no ``schema_version`` key routes to
    ``BlockedResult``; everything else (including a real blocked AutoDevResult)
    routes to ``AutoDevResult`` as before.
    """
    if payload.get("status") == "blocked" and "schema_version" not in payload:
        try:
            return BlockedResult.model_validate(payload)
        except ValidationError as exc:
            msg = "BlockedResult payload failed validation"
            raise EmitValidationError(msg, errors=_format_errors(exc)) from exc
    try:
        return AutoDevResult.model_validate(payload)
    except ValidationError as exc:
        msg = "AutoDevResult payload failed validation"
        raise EmitValidationError(msg, errors=_format_errors(exc)) from exc


def emit_result_on(
    session: Session, payload: dict[str, Any], *, source: LastResultSource
) -> EmitOutcome:
    """Validate PAYLOAD and record it onto SESSION's last_result in place.

    Pure mutator (RFC 0012 A3, #1459): performs NO I/O -- it does not load or
    save state and acquires no lock. It validates PAYLOAD, arbitrates first-
    writer-wins against the passed-in ``session``, and (when accepted) mutates
    ``session.last_result``/``session.last_result_source`` on the object it was
    handed. The caller owns persistence: :func:`emit_result_locked` wraps it in
    a load/save under the sessions lock; the reconcile write sites call it on a
    ``Session`` already inside ``state.sessions`` and rely on their own single
    trailing ``save_state`` to flush the mutation.

    Refusing to overwrite an already-terminal last_result (RFC 0012 S2,
    #1456) is a normal, non-raising return -- EmitOutcome(refused=True,
    result=None, ...) -- and leaves ``session`` byte-identical.

    Validation is discriminated (RFC 0012 A1, #1457): PAYLOAD is checked
    against ``AutoDevResult`` or, for the parser-synthesized blocked shape
    (``status=blocked`` with no ``schema_version``), ``BlockedResult`` --
    see :func:`_validate_harvest_payload`.

    Raises:
        EmitValidationError: if PAYLOAD fails validation against the
            discriminated AutoDevResult/BlockedResult union (before any
            mutation of ``session``).
    """
    result_obj = _validate_harvest_payload(payload)

    prior_status: str | None = (
        session.last_result.get("status")
        if isinstance(session.last_result, dict)
        else None
    )

    if has_terminal_result(session.last_result):
        logger.warning(
            "cw result emit: refusing overwrite session=%s existing_source=%s "
            "attempted_source=%s existing_status=%s",
            session.id,
            session.last_result_source,
            source,
            prior_status,
        )
        return EmitOutcome(
            session_id=session.id,
            result=None,
            prior_status=prior_status,
            refused=True,
            existing_result=session.last_result,
            existing_source=session.last_result_source,
        )

    session.last_result = result_obj.model_dump(mode="json")
    session.last_result_source = source

    return EmitOutcome(
        session_id=session.id, result=result_obj, prior_status=prior_status
    )


def emit_result_locked(
    payload: dict[str, Any], session_id: str, *, source: LastResultSource
) -> EmitOutcome:
    """Validate PAYLOAD and record it onto SESSION_ID's last_result.

    Caller MUST already hold sessions_lock(). Extracted from emit_result()
    so an in-process caller that has already acquired the sessions lock can
    invoke the mutation directly without a second acquisition of the same
    flock-based lock, which would self-deadlock (mirrors
    cw.dev_queue.approval._approve_ticket_locked, GitHub #1065).

    Thin I/O wrapper (RFC 0012 A3, #1459) over the pure :func:`emit_result_on`:
    ``load_state`` -> ``find_by_name_or_id`` -> ``emit_result_on`` -> ``save_state``
    only when the write was accepted (a refusal mutated nothing, so persisting
    it is wasted work). External behavior/signature/exceptions are unchanged.

    Emits no event and performs no task routing -- write-only, matching the
    original cw result emit CLI contract byte-for-byte (RFC 0012 D-A1).

    Refusing to overwrite an already-terminal last_result (RFC 0012 S2,
    #1456) is a normal, non-raising return -- EmitOutcome(refused=True,
    result=None, ...) -- not one of the two exceptions below.

    Validation is discriminated (RFC 0012 A1, #1457): PAYLOAD is checked
    against ``AutoDevResult`` or, for the parser-synthesized blocked shape
    (``status=blocked`` with no ``schema_version``), ``BlockedResult`` --
    see :func:`_validate_harvest_payload`. Because validation now runs inside
    ``emit_result_on`` (after the session lookup), a request supplying BOTH an
    unknown ``session_id`` and an invalid payload raises
    ``EmitSessionNotFoundError`` (not ``EmitValidationError``); this exception-
    precedence flip is the accepted structural consequence of the pure-mutator
    split (RFC 0012 A3 #1459 Adopted Assumption 5). ``cw result emit``'s CLI
    contract is unaffected -- it independently re-validates via
    ``_validate_or_exit`` before touching state at all.

    Raises:
        EmitValidationError: if PAYLOAD fails validation against the
            discriminated AutoDevResult/BlockedResult union.
        EmitSessionNotFoundError: if SESSION_ID has no matching session.
    """
    state = load_state()
    session = state.find_by_name_or_id(session_id)
    if session is None:
        msg = f"Session {session_id!r} not found"
        raise EmitSessionNotFoundError(msg, session_id=session_id)

    outcome = emit_result_on(session, payload, source=source)
    if not outcome.refused:
        save_state(state)
    return outcome


def emit_result(
    payload: dict[str, Any], session_id: str, *, source: LastResultSource
) -> EmitOutcome:
    """Acquire sessions_lock() and record PAYLOAD onto SESSION_ID.

    Thin lock-acquiring wrapper over emit_result_locked() (mirrors
    cw.dev_queue.approval.approve_ticket). Use this from any caller not
    already holding sessions_lock(); use emit_result_locked() directly from
    inside an existing `with sessions_lock():` block to avoid the
    non-reentrant flock deadlocking (see SessionsLockReentryError).
    """
    with sessions_lock():
        return emit_result_locked(payload, session_id, source=source)


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
    On refusal (result already recorded): exits 0, prints
    'Result already recorded for session <id> (source=<source>); not
    overwritten.'
    """
    payload = _read_json_payload(path)
    # RFC 0012 A1 (#1457): emit_result_locked's validation widened to accept
    # the parser-synthesized BlockedResult shape (for the Stop-hook harvest
    # write), but `cw result emit`'s CLI contract must not change alongside
    # it -- strictly re-validate against AutoDevResult only, byte-compatible
    # with the pre-#1457 behavior, before resolving the session or mutating
    # any state.
    _validate_or_exit(payload, extra_stderr_line="No session state was modified.")
    resolved_id = _resolve_emit_session_id(session_id)

    try:
        outcome = emit_result(payload, resolved_id, source=LastResultSource.EMIT_CLI)
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

    if outcome.refused or outcome.result is None:
        click.echo(
            f"Result already recorded for session {outcome.session_id} "
            f"(source={outcome.existing_source}); not overwritten."
        )
        return

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
