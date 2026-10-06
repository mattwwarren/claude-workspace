"""Sentinel validation utilities for cw result subcommands."""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from pydantic import ValidationError

from cw._hook_context import _read_cw_context, _write_cw_context_locked
from cw.auto_dev_result import AutoDevResult, BlockedResult
from cw.config import load_state, save_state, sessions_lock
from cw.events import record_event
from cw.exceptions import (
    AmbiguousSessionIdentifierError,
    EmitSessionNotFoundError,
    EmitValidationError,
    PlanDraftBindingError,
)
from cw.models import (
    HOOK_CONTEXT_RELATIVE_PATH,
    PLAN_DRAFT_FINGERPRINT_KEY,
    STAGED_EMIT_RESULT_KEY,
    LastResultSource,
    OrchestratorEventType,
)
from cw.plan_fingerprint import (
    PLAN_DRAFT_RELATIVE_PATH,
    FingerprintBinding,
    bind_claimed_fingerprint,
    sanitize_persisted_fingerprint,
)

if TYPE_CHECKING:
    from cw.models import Session

logger = logging.getLogger(__name__)

_NO_STATE_MODIFIED = "No session state was modified."


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

    Cross-module invariant (#2458 round 2): this gate accepts ``AutoDevResult``
    ONLY -- ``cw result emit`` never stages a bare ``BlockedResult``. The idle
    sweep's ``cw.reconcile.idle._mutations._apply_idle_routed_mutations``
    consumes ``landed_terminal`` (#2482), like the Stop hook's
    ``_handle_unrouted_stop`` (#1273), so widening this gate to the
    ``BlockedResult`` shape (the way the Stop-hook harvest door was, RFC 0012
    A1 / #1457) would not strand a staged landed-terminal-FAILED result in the
    idle backstop. Pinned by
    ``test_validate_or_exit_rejects_bare_blocked_result_shape``.
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

    Field rules (``cw schema show auto-dev-result`` is authoritative):
    - schema_version: int -- one of the accepted versions (currently 1-8)
    - ticket_id: str
    - status: "shipped" | "stage_complete" | "no_op" | "blocked" |
      "merge_pending" | "merge_gate_blocked" | "plan_pending_approval" |
      "review_pending_approval" | "ambiguities_pending_resolution" |
      "premises_pending_verification" | "scope_exceeded" | "forbidden_area" |
      "empty_diff_blocked" | "stale_dispatch"
    - stage_reached: str literal (see cw schema show auto-dev-result for full list)
    - scope: {tier, files, lines_estimate, lines_actual, forbidden_touched}
    - plan_source: "linear_existing" | "github_issue_existing" | "generated" |
      "free_text" | "none"
    - branch: str | null

    Constraints (cross-field invariants):
    - pr: non-null iff status in {"shipped", "merge_pending"}
    - blocker: required when status == "blocked"; optional for
      "merge_gate_blocked", "empty_diff_blocked", "stale_dispatch"; null otherwise
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

    cwd = str(Path.cwd())
    context_path = Path(cwd) / HOOK_CONTEXT_RELATIVE_PATH
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


def reconstruct_staged_sentinel(
    last_result: dict[str, Any] | None,
) -> AutoDevResult | BlockedResult | None:
    """Rebuild a staged ``session.last_result`` into its validated sentinel object.

    :func:`has_terminal_result` only confirms a ``"status"`` key is present; it
    does not prove the dict matches either arm of the discriminated
    ``AutoDevResult``/``BlockedResult`` union (a stale or foreign shape satisfies
    it too). Returns ``None`` on any validation failure so callers can fall
    back rather than raise.

    Single implementation shared by the Stop hook's emit-precedence path
    (``cw.cli.stop_hook``) and the phantom sweep's staged-sentinel router
    (``cw.reconcile.phantom``) -- GitHub #1762. Both used to reconstruct
    ``last_result`` independently, and the Stop hook's copy validated against
    ``AutoDevResult`` alone, so a session that died holding a parser-synthesized
    ``BlockedResult`` was unreconstructable there.
    """
    if last_result is None:
        return None
    sanitized = sanitize_persisted_fingerprint(last_result)
    if sanitized is not last_result:
        raw = last_result.get(PLAN_DRAFT_FINGERPRINT_KEY)
        logger.warning(
            "reconstruct_staged_sentinel: persisted plan_draft_fingerprint is not "
            "a 64-character lowercase hex digest (got %d characters); "
            "reconstructing with null (#2382)",
            len(raw) if isinstance(raw, str) else -1,
        )
    try:
        return _validate_harvest_payload(sanitized)
    except EmitValidationError as exc:
        # #2458: every caller falls back on None (the Stop hook to the
        # transcript, the reconcile sweeps to their next producer), so this is
        # the only place the *reason* a staged result could not be routed is
        # still known.
        logger.warning(
            "reconstruct_staged_sentinel: validation failed: %s",
            "; ".join(exc.errors),
        )
        return None


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


def _record_result_emitted_audit(
    session: Session,
    payload_for_digest: dict[str, Any],
    *,
    source: LastResultSource,
    status: str,
) -> None:
    """Append the #2439 audit-only ``session.result_emitted`` event, best-effort.

    Audit-only by construction (R2): never read by any routing, reconcile,
    salvage, or attention consumer, and carries no completion/task-routing
    effect of its own -- it exists purely so an operator can answer "who
    wrote this session's result, and when" without reconstructing it from
    logs. ``payload_digest`` is a sha256 hex of the normalized sentinel
    actually written to ``session.last_result`` (``payload_for_digest``),
    not the raw incoming payload.

    Fails open (#2465): the result is already accepted by first-writer-wins
    arbitration before this runs, and persisting it matters more than
    recording who wrote it. An ``OSError`` from the event inbox (or from
    resolving the actor) is therefore logged and swallowed, so every caller
    -- ``cw result emit``, the Stop-hook harvest, executor-direct writers and
    the reconcile result-write paths -- still persists the accepted result.
    Only the audit append is covered; a failure to persist session or queue
    state elsewhere is never routed through here and still propagates.
    """
    # Function-local import breaks the cw.cli <-> cw.result circular dependency;
    # inline import is the sanctioned mechanism (PLC0415), not a workaround.
    from cw.reconcile._shared import ticket_id_for_session

    ticket_id = ticket_id_for_session(session.name)
    payload_digest = hashlib.sha256(
        json.dumps(payload_for_digest, sort_keys=True).encode()
    ).hexdigest()
    audit_payload = {
        "session_id": session.id,
        "ticket_id": ticket_id,
        "client": session.client,
        "lane": session.lane,
        "stage": session.stage.value if session.stage else None,
        "last_result_source": source.value,
        "status": status,
        "payload_digest": payload_digest,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    try:
        actor = getpass.getuser()
        record_event(
            OrchestratorEventType.SESSION_RESULT_EMITTED,
            {**audit_payload, "actor": actor},
            correlation_id=ticket_id,
        )
    except OSError as exc:
        logger.warning(
            "session.result_emitted audit append failed for session=%s "
            "source=%s status=%s payload_digest=%s; continuing without the "
            "audit record: %s",
            session.id,
            source.value,
            status,
            payload_digest,
            exc,
        )


def emit_result_on_audited(
    session: Session, payload: dict[str, Any], *, source: LastResultSource
) -> EmitOutcome:
    """Apply the pure emit mutation and audit an accepted result.

    This remains an in-memory operation; the caller owns persistence. It is
    the audit-aware seam for reconcile paths that already hold a loaded
    ``Session`` and therefore cannot use :func:`emit_result_locked`.
    """
    outcome = emit_result_on(session, payload, source=source)
    if outcome.result is not None:
        _record_result_emitted_audit(
            session,
            outcome.result.model_dump(mode="json"),
            source=source,
            status=outcome.result.status,
        )
    return outcome


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

    Records an audit-only ``session.result_emitted`` event (#2439) on every
    accepted write, before the state save -- never consumed by any routing,
    reconcile, salvage, or attention path (R2). The audit append is
    best-effort (#2465): an inbox failure is logged and the accepted result
    still reaches ``save_state``, whose own failure still propagates. A
    refusal records no event and persists nothing. The function still performs
    NO task routing of its own: the Stop hook remains the sole
    completion-event source, matching the original cw result emit CLI
    contract byte-for-byte (RFC 0012 D-A1).

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

    # Why: event-first ordering is deliberate (mirrors
    # revoke_plan_approval's documented accepted-risk case exactly, see
    # tests/test_dev_queue.py's test_revoke_plan_approval_save_failure_
    # raises_with_event_recorded) -- if save_state below later fails, a
    # phantom audit record is preferable to an unaudited mutation
    # reaching disk with no trail. The audit append itself fails open
    # (#2465): it cannot keep the accepted result from reaching save_state.
    outcome = emit_result_on_audited(session, payload, source=source)
    # outcome.result is non-None exactly when the write was accepted (see
    # EmitOutcome's docstring) -- narrowing on this, rather than on
    # `not outcome.refused`, lets mypy see through to the accepted branch.
    if outcome.result is not None:
        save_state(state)
    return outcome


def emit_result(
    payload: dict[str, Any], session_id: str, *, source: LastResultSource
) -> EmitOutcome:
    """Acquire sessions_lock() and record PAYLOAD onto SESSION_ID.

    Thin lock-acquiring wrapper over emit_result_locked() (mirrors
    cw.dev_queue.approval.approve_ticket). Use this from any caller not
    already holding sessions_lock(); use emit_result_locked() directly from
    inside an existing `with sessions_lock():` block: a nested acquisition
    raises CwLockReentrancyError (ADR-0019).
    """
    with sessions_lock():
        return emit_result_locked(payload, session_id, source=source)


def _load_session_for_emit(session_id: str) -> Session:
    """Read-only lookup of the emit target, outside the sessions lock.

    Raises the same ``EmitSessionNotFoundError`` /
    ``AmbiguousSessionIdentifierError`` that ``emit_result_locked`` raises, so
    the CLI reports one message for either path. The authoritative
    first-writer-wins arbitration still happens inside the lock; this lookup
    only lets the CLI short-circuit an already-recorded session before it
    binds a draft or validates (#2382), and resolve the draft against the
    target session's own worktree rather than the invoker's cwd.
    """
    session = load_state().find_by_name_or_id(session_id)
    if session is None:
        msg = f"Session {session_id!r} not found"
        raise EmitSessionNotFoundError(msg, session_id=session_id)
    return session


def _default_plan_draft_path(session: Session) -> Path:
    """The draft `cw result emit` binds against when ``--plan-draft`` is absent.

    The target session's recorded ``worktree_path`` wins: with ``--session-id``
    an operator may run the command from any directory, and a draft that
    happens to sit in the invoker's cwd must never be bound to another
    session's approval (#2382). A session without a recorded worktree (a
    USER-origin session) falls back to ``<cwd>/.cw/plan-draft.md``, the same
    directory ``cw-context.json`` is read from.
    """
    if session.worktree_path is not None:
        return Path(session.worktree_path) / PLAN_DRAFT_RELATIVE_PATH
    return PLAN_DRAFT_RELATIVE_PATH


def _echo_refusal(session_id: str, source: LastResultSource | None) -> None:
    click.echo(
        f"Result already recorded for session {session_id} "
        f"(source={source}); not overwritten."
    )


def _echo_binding_note(binding: FingerprintBinding | None) -> None:
    """Report a replaced producer value, only once the write is known to have landed."""
    if binding is not None and binding.replaced:
        click.echo(
            f"{PLAN_DRAFT_FINGERPRINT_KEY}: replaced the payload's value with the "
            f"digest computed from {binding.draft_path} (the payload's value "
            "differed).",
            err=True,
        )


def _fail_no_mutation(message: str | None) -> click.exceptions.Exit:
    """Build the exit-1 for a pre-write failure, after echoing *message* (if any)."""
    if message is not None:
        click.echo(message, err=True)
    click.echo(_NO_STATE_MODIFIED, err=True)
    return click.exceptions.Exit(1)


def _stamp_staged_emit_result(session_id: str) -> None:
    """Best-effort: flag this worktree's cw-context.json after a successful emit.

    #2458: the Stop hook's lock-free peek
    (``cw.cli.stop_hook._peek_staged_emit_result``)
    reads this flag instead of ``load_state()`` to answer "does this session
    hold a staged emit_cli result" without a fleet-wide sessions.json load on
    every Stop-hook fire with pending ``background_tasks``.

    Only stamps when the invoking cwd's own cw-context.json already names
    *session_id* as its session -- an operator running ``cw result emit
    --session-id`` from an unrelated directory must not stamp a foreign
    worktree's context. Silent no-op on any mismatch, missing file, or write
    failure: purely an optimization for the peek, never load-bearing -- a
    peek that misses the flag just defers as it did before #2458, which a
    later Stop or the idle sweep recovers from.
    """
    cwd = str(Path.cwd())
    context = _read_cw_context(cwd)
    if context is None or context.get("session_id") != session_id:
        return
    _write_cw_context_locked(cwd, lambda ctx: {**ctx, STAGED_EMIT_RESULT_KEY: True})


@result.command(name="emit")
@click.argument("path")
@click.option(
    "--session-id",
    "session_id",
    default=None,
    help="Session id override; wins over cw-context.json.",
)
@click.option(
    "--plan-draft",
    "plan_draft",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help=(
        "Plan draft the payload's plan_draft_fingerprint is recomputed from; "
        "defaults to <session worktree>/.cw/plan-draft.md."
    ),
)
def result_emit(path: str, session_id: str | None, plan_draft: Path | None) -> None:
    """Record an AutoDevResult onto its session (push-based completion).

    PATH is a file path or '-' for stdin. The payload must be the inner JSON
    object only -- do NOT include the <<<AUTO_DEV_RESULT>>> delimiters.

    This is the worker's terminal step in every headless stage (#2382): the
    bytes recorded here are the bytes cw routes on, and the Stop hook takes an
    emitted result over the transcript sentinel (#536). Validation is strict --
    none of the transcript parser's leniency coercions apply -- so a payload
    that fails here is fixed and re-emitted, never framed as-is.

    Write-only: resolves the target session (``--session-id`` wins, else the
    ``session_id`` from ``<cwd>/.claude/cw-context.json``), validates the
    payload, and writes ``session.last_result`` under the sessions lock. Also
    records an audit-only ``session.result_emitted`` event (#2439) -- never
    consumed by any routing/reconcile/salvage/attention path (R2). Changes NO
    session status -- the Stop hook remains the sole completion-event source.
    Validation strictly precedes any state write, so a bad payload leaves
    state untouched.

    A session that already carries a terminal result short-circuits to the
    'already recorded' outcome before the payload is bound or validated: a
    repeat call has nothing to fix, and its draft may since have been
    promoted away. A non-null ``plan_draft_fingerprint`` is read only as a
    claim that a draft is in hand: the digest recorded is recomputed by cw
    from ``<session worktree>/.cw/plan-draft.md`` (or ``--plan-draft``),
    never copied from the payload. A claim with no draft file exits 1 with no
    state modified.

    On success: exits 0, prints
    'Recorded result for session <short_id>: status=<status>'.
    On validation failure: exits 1, prints 'field.path: message' lines plus a
    'No session state was modified.' notice to stderr.
    On refusal (result already recorded): exits 0, prints
    'Result already recorded for session <id> (source=<source>); not
    overwritten.'
    On ambiguous ``--session-id``: exits 1, prints the candidate listing and
    'No session state was modified.' to stderr.
    """
    payload = _read_json_payload(path)
    resolved_id = _resolve_emit_session_id(session_id)

    try:
        session = _load_session_for_emit(resolved_id)
        if has_terminal_result(session.last_result):
            _echo_refusal(session.id, session.last_result_source)
            return
        draft_path = (
            plan_draft
            if plan_draft is not None
            else (_default_plan_draft_path(session))
        )
        binding = bind_claimed_fingerprint(payload, draft_path)
        # RFC 0012 A1 (#1457): emit_result_locked's validation widened to
        # accept the parser-synthesized BlockedResult shape (for the Stop-hook
        # harvest write), but `cw result emit`'s CLI contract must not change
        # alongside it -- strictly re-validate against AutoDevResult only,
        # byte-compatible with the pre-#1457 behavior, before mutating state.
        _validate_or_exit(payload, extra_stderr_line=_NO_STATE_MODIFIED)
        outcome = emit_result(payload, resolved_id, source=LastResultSource.EMIT_CLI)
    except PlanDraftBindingError as exc:
        raise _fail_no_mutation(str(exc)) from exc
    except EmitValidationError as exc:
        for line in exc.errors:
            click.echo(line, err=True)
        raise _fail_no_mutation(None) from exc
    except AmbiguousSessionIdentifierError as exc:
        raise _fail_no_mutation(str(exc)) from exc
    except EmitSessionNotFoundError as exc:
        click.echo(
            f"Session '{exc.session_id}' not found; no state was modified.",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc

    if outcome.refused or outcome.result is None:
        _echo_refusal(outcome.session_id, outcome.existing_source)
        return

    _stamp_staged_emit_result(outcome.session_id)
    _echo_binding_note(binding)
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
