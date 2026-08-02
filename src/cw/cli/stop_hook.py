"""The ``cw signal-stop`` Stop-hook backstop.

Extracted verbatim from ``cw.cli.sessions`` (module-size split): the Stop-hook
handler and its headless-resolution helpers are a separate concern from the
session lifecycle commands. Wired in via ``.claude/settings.local.json``
written by spawn into each dispatched session's worktree; see
:func:`signal_stop` for the full contract (GitHub #133, #147, #151, #176).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from pydantic import ValidationError

from cw.auto_dev_result import AutoDevResult
from cw.cli._base import handle_errors, main
from cw.cli._hook_io import _read_cw_context, _read_hook_stdin_json
from cw.cli._sentinels import _parse_sentinel_from_transcript
from cw.config import (
    get_client,
    load_orchestrator_config,
    load_state,
    save_state,
    sessions_lock,
)
from cw.dev_queue import (
    dev_queue_lock,
    load_dev_queue,
    save_dev_queue,
    transition_task_status,
)
from cw.events import record_event
from cw.exceptions import CwError, EmitSessionNotFoundError, EmitValidationError
from cw.models import (
    CompletionReason,
    LastResultSource,
    OrchestratorEventType,
    QueueItemStatus,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import (
    _apply_sentinel_to_task,
    _has_terminal_sentinel,
    resolve_headless_budget,
)
from cw.result import emit_result_locked
from cw.worktree import reconcile_result_scope

if TYPE_CHECKING:
    from cw.auto_dev_result import BlockedResult
    from cw.models import CwState, Session, TicketTask

logger = logging.getLogger(__name__)


def _read_stop_hook_payload() -> tuple[dict[str, object], str] | None:
    """Read the Stop-hook JSON from stdin and extract its ``cwd``.

    Returns ``(hook_payload, cwd_value)`` when stdin holds a JSON object with a
    string ``cwd``, else ``None``. Best-effort: every failure mode (unreadable
    stdin, empty body, malformed JSON, missing cwd) is a silent no-op.
    """
    hook_payload = _read_hook_stdin_json()
    if hook_payload is None:
        return None
    cwd_value = hook_payload.get("cwd")
    if not isinstance(cwd_value, str):
        return None
    return hook_payload, cwd_value


def _resolve_signal_stop_context() -> (
    tuple[dict[str, object], dict[str, object], str, str] | None
):
    """Read and validate the Stop-hook payload + cw-context.json.

    Returns ``(hook_payload, context, cwd_value, cw_session_id)`` when every
    required field is present and well-typed, else ``None`` (silent no-op so
    hook execution never blocks claude from exiting). See :func:`signal_stop`.
    """
    payload = _read_stop_hook_payload()
    if payload is None:
        return None
    hook_payload, cwd_value = payload

    context = _read_cw_context(cwd_value)
    if context is None:
        return None

    cw_session_id = context.get("session_id")
    if not isinstance(cw_session_id, str):
        return None

    return hook_payload, context, cwd_value, cw_session_id


def _parse_headless_sentinel(
    session: Session,
    cwd_value: str,
    claude_session_id: object,
) -> AutoDevResult | BlockedResult | None:
    """Parse the transcript sentinel for a headless Stop hook.

    Issue #799: when EnterWorktree shifts the hook cwd to a nested worktree,
    ``cwd_value`` derives the wrong Claude project dir. Retry with the session's
    recorded ``worktree_path`` — the directory whose project dir holds the actual
    transcript. Returns ``None`` when neither location yields a parseable sentinel.

    Extracted out of :func:`signal_stop` (rather than inlined) so the #536
    emit-precedence gate could be added there without pushing the function
    over its PLR0912 branch-count ceiling.
    """
    csid = claude_session_id if isinstance(claude_session_id, str) else None
    parsed = _parse_sentinel_from_transcript(cwd_value, csid)
    if parsed is None and session.worktree_path is not None:
        parsed = _parse_sentinel_from_transcript(str(session.worktree_path), csid)
    if isinstance(parsed, AutoDevResult):
        parsed = _verify_headless_scope(parsed, session)
    return parsed


def _verify_headless_scope(result: AutoDevResult, session: Session) -> AutoDevResult:
    """Correct a headless sentinel's self-reported scope against git facts (#1487).

    This is the last point before ``signal_stop`` writes ``last_result``, so a
    fabricated or stale-merge-base scope corrected here never reaches the queue.
    An unresolvable client falls back to ``main`` — the Stop hook must never
    raise, and losing the sentinel would cost far more than measuring against
    the wrong base.
    """
    try:
        default_branch = get_client(session.client).default_branch
    except CwError:
        logger.warning(
            "scope_verification_client_unresolved: session=%s client=%s; "
            "measuring against 'main'",
            session.id,
            session.client,
        )
        default_branch = "main"
    return reconcile_result_scope(
        result,
        worktree_path=session.worktree_path,
        default_branch=default_branch,
    )


def _reconstruct_emitted_sentinel(session: Session) -> AutoDevResult | None:
    """Reconstruct the authoritative sentinel from an emitted ``last_result``.

    ``_has_terminal_sentinel`` only confirms a ``"status"`` key is present —
    it does not guarantee the dict matches the ``AutoDevResult`` schema (e.g.
    a stale/foreign shape). Returns ``None`` on a validation failure so the
    caller falls back to the transcript parse instead of raising out of the
    Stop hook, which must never block claude from exiting.
    """
    try:
        return AutoDevResult.model_validate(session.last_result)
    except ValidationError:
        logger.warning(
            "session=%s emitted last_result failed AutoDevResult validation, "
            "falling back to transcript parse",
            session.id,
        )
        return None


def _handle_headless_no_sentinel(
    state: CwState,
    session: Session,
    *,
    now: datetime,
    claude_session_id: object,
    context: dict[str, object],
    ticket_id_value: object,
    hook_payload: dict[str, object],
) -> bool:
    """Resolve a sentinel-less headless Stop hook: defer or time out.

    Under the resolved headless budget the call defers (returns True without
    mutating state) so a later Stop hook or reconcile can retry. Over budget
    it records a TIMED_OUT transition via :func:`_record_headless_timeout`.
    Returns True in both cases — the caller must stop processing.
    """
    elapsed = (now - session.started_at).total_seconds()
    headless_config = load_orchestrator_config()
    stop_task: TicketTask | None = None
    if isinstance(ticket_id_value, str):
        stop_store = load_dev_queue()
        stop_task = next(
            (t for t in stop_store.tasks if t.ticket_id == ticket_id_value),
            None,
        )
    budget = resolve_headless_budget(stop_task, session, headless_config)
    if elapsed < budget:
        # Under budget — defer. Another Stop hook turn will fire, or
        # reconcile will eventually catch a phantom and CRASH it.
        return True
    # Budget exceeded without sentinel → TIMED_OUT (loud, retry-eligible).
    _record_headless_timeout(
        state,
        session,
        now=now,
        elapsed=elapsed,
        claude_session_id=claude_session_id,
        context=context,
        ticket_id_value=ticket_id_value,
        hook_payload=hook_payload,
    )
    return True


def _harvest_last_result_through_door(
    session_id: str, sentinel: AutoDevResult | BlockedResult
) -> None:
    """Push a freshly re-parsed Stop-hook sentinel through the emit door.

    RFC 0012 A1 (#1457): the Stop-hook harvest write no longer assigns
    ``session.last_result`` directly -- it routes through
    ``emit_result_locked`` (the same first-writer-wins arbitration ``cw
    result emit`` uses, RFC 0012 S2, #1456) so a session that already has a
    terminal result recorded from another writer can't be silently
    clobbered by a late transcript re-parse.

    Best-effort: a validation failure or missing session is logged and
    swallowed, never raised -- the Stop hook must never block claude from
    exiting. There is no fallback write; a failure here just means
    ``last_result`` stays whatever it already was. A refusal (terminal
    result already present) is not logged again here -- ``emit_result_locked``
    already emits its own warning on refusal.
    """
    try:
        emit_result_locked(
            sentinel.model_dump(mode="json"),
            session_id,
            source=LastResultSource.STOP_HOOK_HARVEST,
        )
    except (EmitValidationError, EmitSessionNotFoundError) as exc:
        logger.warning(
            "stop-hook harvest write rejected by door for session %s: %s",
            session_id,
            exc,
        )


class _HeadlessResolution(NamedTuple):
    """Result of ``_resolve_and_complete_headless_session`` (#1273).

    ``rescued`` is ``None`` when the caller must bail without further action
    (see that function's docstring); ``landed_terminal`` is ``True`` only
    when the bail was caused by a BlockedResult that itself just landed the
    task terminal-FAILED (``SentinelRouteOutcome.landed_terminal``) — the
    signal for ``signal_stop`` to stop the now-leaked DAEMON worker.
    """

    rescued: bool | None
    landed_terminal: bool = False


def _resolve_and_complete_headless_session(
    state: CwState,
    session: Session,
    *,
    hook_payload: dict[str, object],
    context: dict[str, object],
    cwd_value: str,
    claude_session_id: object,
    ticket_id_value: object,
    is_headless: bool,
    now: datetime,
) -> _HeadlessResolution:
    """Resolve the headless sentinel and mark the session COMPLETED (#176, #251).

    Extracted from ``signal_stop`` to stay under the branch/return caps; owns
    the sentinel lookup, the #251 staged-advance routing, and the terminal
    session mutation + ``save_state``.

    Returns a ``_HeadlessResolution`` with ``rescued=None`` when the caller
    must bail without any further action: either no sentinel was found under
    budget (``_handle_headless_no_sentinel`` already ran its own transition),
    or the shared staged-advance authority refused the route on a stage
    mismatch (GitHub #1031, the #986 incident — extends #1019's phantom-path
    guard to the Stop-hook path) or a #1189 raced-to-terminal lookup miss. A
    refusal leaves session and task completely untouched so a later reconcile
    tick or Stop hook can re-observe them — except when the refusal was
    itself a BlockedResult landing the task terminal-FAILED, signaled via
    ``landed_terminal=True`` (#1273), which leaves the daemon worker leaked.

    Returns ``_HeadlessResolution(rescued=<bool>)`` once the session has been
    marked COMPLETED and persisted.
    """
    parsed_sentinel: AutoDevResult | BlockedResult | None = None
    # Issue #536: emit precedence. When the producer already pushed a
    # terminal result via ``cw result emit`` (session.last_result carries a
    # "status"), that value is authoritative — reconstruct it and skip the
    # transcript re-parse entirely. The transcript sentinel is demoted to a
    # forensic fallback: it still runs (and is still authoritative) whenever
    # no emitted result exists, so a worker that never emits is unaffected.
    emit_terminal = False
    if is_headless and _has_terminal_sentinel(session):
        parsed_sentinel = _reconstruct_emitted_sentinel(session)
        emit_terminal = parsed_sentinel is not None
    if not emit_terminal and is_headless:
        parsed_sentinel = _parse_headless_sentinel(
            session, cwd_value, claude_session_id
        )
        if parsed_sentinel is None:
            _handle_headless_no_sentinel(
                state,
                session,
                now=now,
                claude_session_id=claude_session_id,
                context=context,
                ticket_id_value=ticket_id_value,
                hook_payload=hook_payload,
            )
            return _HeadlessResolution(rescued=None, landed_terminal=False)

    # Issue #251: directly update the dev-queue task *before* marking the
    # session COMPLETED. This closes the race where revert_completed_silent_tasks
    # sees a COMPLETED session with a still-RUNNING task and reverts it to
    # PENDING before consume_completed_sessions can process the event — causing
    # no_op and similar terminal outcomes to trigger infinite re-dispatch.
    rescued = False
    routed = True
    if is_headless and parsed_sentinel is not None and isinstance(ticket_id_value, str):
        outcome = _apply_sentinel_to_task(
            ticket_id_value, session, parsed_sentinel, now=now
        )
        rescued = outcome.rescued
        routed = outcome.routed
    if not routed:
        return _HeadlessResolution(
            rescued=None, landed_terminal=outcome.landed_terminal
        )

    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    if isinstance(claude_session_id, str):
        session.claude_session_id = claude_session_id
    # Issue #225: headless DAEMON sessions set last_result via signal_stop,
    # which parses the transcript before save_state so downstream consumers
    # (consume_completed_sessions, /cw-followup) can route by status.
    # parse_stdout returns BlockedResult on malformed payloads — we
    # persist either shape; both serialize to a dict with a "status" field.
    save_state(state)
    # Issue #536 / RFC 0012 A1 (#1457): when the result was pushed via
    # ``cw result emit`` (emit_terminal), session.last_result is already the
    # authoritative value — do NOT overwrite it with the reconstructed/
    # re-parsed sentinel. Otherwise, push the freshly re-parsed sentinel
    # through the emit door (first-writer-wins arbitration, RFC 0012 S2)
    # rather than assigning session.last_result directly.
    if parsed_sentinel is not None and not emit_terminal:
        _harvest_last_result_through_door(session.id, parsed_sentinel)
    return _HeadlessResolution(rescued=rescued, landed_terminal=False)


def _handle_user_origin_stop(
    state: CwState,
    session: Session,
    claude_session_id: object,
) -> bool:
    """Handle a Stop hook for a USER-origin (interactive) session.

    Issue #165 Phase B: mark an ACTIVE session IDLE (no SESSION_COMPLETED,
    no daemon stop). A non-ACTIVE session is left untouched. Returns True
    when the caller should stop processing (always, for USER origin).
    """
    if session.status != SessionStatus.ACTIVE:
        # BACKGROUNDED (or any non-ACTIVE state) — silent no-op so a
        # Stop hook firing on a session the user has explicitly
        # parked doesn't flip its status.
        return True
    session.status = SessionStatus.IDLE
    if isinstance(claude_session_id, str):
        session.claude_session_id = claude_session_id
    save_state(state)
    return True


def _record_headless_timeout(
    state: CwState,
    session: Session,
    *,
    now: datetime,
    elapsed: float,
    claude_session_id: object,
    context: dict[str, object],
    ticket_id_value: object,
    hook_payload: dict[str, object],
) -> None:
    """Mark a budget-exceeded headless session TIMED_OUT and revert its task.

    Transitions *session* to TIMED_OUT, persists state, emits
    ``SESSION_TIMED_OUT``, reverts the owning RUNNING TicketTask to PENDING so
    the dispatch loop can retry, and best-effort stops the daemon worker.
    See issue #176.
    """
    last_msg = hook_payload.get("last_assistant_message", "")
    excerpt = str(last_msg)[:500] if last_msg else ""
    session.status = SessionStatus.TIMED_OUT
    session.completed_at = now
    session.completed_reason = CompletionReason.TIMED_OUT
    if isinstance(claude_session_id, str):
        session.claude_session_id = claude_session_id
    save_state(state)
    timed_out_payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "client": context.get("client"),
        "ticket_id": ticket_id_value,
        "claude_session_id": claude_session_id,
        "elapsed_seconds": elapsed,
        "last_assistant_message_excerpt": excerpt,
    }
    record_event(OrchestratorEventType.SESSION_TIMED_OUT, timed_out_payload)
    # Revert the owning TicketTask from RUNNING → PENDING so the
    # dispatch loop can retry this ticket on the next tick.
    with dev_queue_lock():
        store = load_dev_queue()
        for task in store.tasks:
            if (
                task.ticket_id == ticket_id_value
                and task.status == QueueItemStatus.RUNNING
            ):
                transition_task_status(task, QueueItemStatus.PENDING)
                task.session_id = None
                break
        save_dev_queue(store)
    if session.surface_ref is not None:
        get_native_daemon_client().stop(session.surface_ref)


@main.command(name="signal-stop")
@handle_errors
def signal_stop() -> None:
    """Emit SESSION_COMPLETED on a Stop hook fire.

    Reads the hook JSON from stdin, extracts ``cwd`` (the worktree path),
    reads ``<cwd>/.claude/cw-context.json`` for cw correlation IDs,
    transitions the matching Session to COMPLETED, and posts a
    ``session.completed`` event to the inbox so the dispatch consumer
    can transition the matching TicketTask to COMPLETED.

    Wired in via ``.claude/settings.local.json`` written by spawn into
    each dispatched session's worktree. Bypasses env-var loss under
    ``claude --bg`` (see GitHub issue #133, design in #147).

    Idempotent: a session already COMPLETED is a no-op (re-firing the
    Stop hook on a subsequent turn won't double-record).

    Best-effort: a missing or unreadable context file is a silent no-op
    so hook execution never blocks claude from exiting.

    Defers when the hook payload carries a non-empty ``background_tasks``
    list: the Stop hook fires at every main-agent turn boundary, and
    dispatching a ``run_in_background: true`` subagent ends the parent's
    turn while the subagent is still running. Completing the session
    here would orphan the subagent. See issue #151.
    """
    resolved_context = _resolve_signal_stop_context()
    if resolved_context is None:
        return
    hook_payload, context, cwd_value, cw_session_id = resolved_context

    bg_tasks = hook_payload.get("background_tasks")
    if isinstance(bg_tasks, list) and bg_tasks:
        # Turn boundary with pending background work — leave the session
        # in its current status; another Stop hook will fire when the bg
        # work drains (the contract `claude --bg + run_in_background: true`
        # relies on: the subagent's result arrives as the next main-agent
        # turn, which then ends, firing Stop again with background_tasks
        # empty). Without this guard, dispatching a run_in_background: true
        # subagent causes the parent to be marked COMPLETED and, for
        # DAEMON-origin sessions, killed via `claude stop`, orphaning the
        # in-flight subagent. See issue #151.
        #
        # Fast path: no state I/O. The idempotency guard below is
        # unreachable on this path by design — deferral leaves state
        # untouched regardless of current session status.
        #
        # Backstop: if the second Stop hook ever fails to fire (daemon
        # bug, subagent hard crash with no clean Stop), reconcile.py
        # eventually detects the phantom and marks the session CRASHED,
        # reverting any matching dev_queue task to PENDING for retry.
        # Recovery, not silent wedge.
        return

    # Why not mutate_state: dual-lock (dev_queue_lock nested at the TIMED_OUT path)
    # and daemon.stop() network call inside the lock window (criteria 1 and 2).
    with sessions_lock():
        state = load_state()
        session = next((s for s in state.sessions if s.id == cw_session_id), None)
        if session is None or session.status in (
            SessionStatus.COMPLETED,
            SessionStatus.IDLE,
            SessionStatus.TIMED_OUT,
        ):
            return

        claude_session_id = hook_payload.get("session_id")

        # Issue #285: stale-hook guard. When dispatch reuses a worktree for a
        # blocked→retry sequence, spawn_create_impl overwrites cw-context.json with
        # the new session's ID *before* the old Claude process finishes. The old
        # process can then fire one final Stop hook: the hook reads the new session's
        # CW ID from context but carries the old Claude UUID in its payload. Without
        # this guard the stale hook would parse the old (blocked) transcript and
        # apply that sentinel to the new session's task, reverting it to PENDING.
        # Fix: drop any DAEMON-origin hook whose Claude UUID doesn't match this
        # session's surface_ref (the 8-char prefix stored at spawn time).
        # USER-origin sessions are interactive and never have cw-context.json
        # overwritten by dispatch, so the guard does not apply to them.
        if (
            session.origin is SessionOrigin.DAEMON
            and isinstance(claude_session_id, str)
            and session.surface_ref is not None
            and not claude_session_id.startswith(session.surface_ref)
        ):
            return

        # Issue #165 Phase B: USER-origin sessions are interactive — the Stop
        # hook fires at every agent turn but the human is still driving. Mark
        # IDLE so wait loops / daemon triggers can react, but do NOT emit
        # SESSION_COMPLETED (no dev_queue task to retire) and do NOT call
        # native_daemon.stop (no roster entry to clean up). DAEMON-origin
        # falls through to the existing COMPLETED transition below.
        if session.origin is SessionOrigin.USER:
            _handle_user_origin_stop(state, session, claude_session_id)
            return

        # Issue #176 Layer 1: headless backstop.
        #
        # A headless DAEMON session (ticket_id present in context) must NOT be
        # silently marked COMPLETED unless it emitted an AUTO_DEV_RESULT sentinel.
        # The bg_tasks guard above correctly defers when a subagent is in flight,
        # but the parent's *next* turn may end (with background_tasks=[]) before
        # it has finished its post-wait pipeline work — a silent orphan.
        #
        # Detection: DAEMON-origin + non-None ticket_id in context ≡ headless.
        # Sentinel check: look for the sentinel open tag in the Claude transcript.
        # Budget: if no sentinel AND wall-clock since session.started_at exceeds
        # HEADLESS_TIMEOUT_SECONDS, transition to TIMED_OUT (retry-eligible) so
        # the failure is loud and dev-queue can retry. Under budget: defer (return)
        # so another Stop hook (or reconcile) can catch it later.
        #
        # The guard does NOT replace the bg_tasks deferral — both fire independently.
        ticket_id_value = context.get("ticket_id")
        # ``headless: true`` in cw-context.json is written by spawn_create_impl
        # when dispatch launches a /auto-dev session. Absent (or False) for legacy
        # sessions and non-headless daemon sessions — those fall through to the
        # normal COMPLETED path unchanged.
        is_headless = session.origin is SessionOrigin.DAEMON and bool(
            context.get("headless")
        )
        now = datetime.now(UTC)

        # Resolves the sentinel, routes it through the #251 staged-advance
        # authority, and (if accepted) marks the session COMPLETED. rescued
        # is None when the caller must bail without further action -- no
        # sentinel under budget, a #1031 stage-mismatch route refusal, or a
        # #1189 raced-to-terminal lookup miss. landed_terminal (#1273)
        # distinguishes the case where the bail was itself a BlockedResult
        # that landed the task terminal-FAILED, leaking the daemon worker.
        resolution = _resolve_and_complete_headless_session(
            state,
            session,
            hook_payload=hook_payload,
            context=context,
            cwd_value=cwd_value,
            claude_session_id=claude_session_id,
            ticket_id_value=ticket_id_value,
            is_headless=is_headless,
            now=now,
        )
        rescued = resolution.rescued
        landed_terminal = resolution.landed_terminal

    if rescued is None:
        # #1273: a BlockedResult that itself just landed the task
        # terminal-FAILED leaks the DAEMON worker -- stop it even though the
        # session is never marked COMPLETED. A stage-mismatch (#986) or
        # raced-to-terminal (#1189) refusal leaves landed_terminal False, so
        # a still-legitimate or already-handled worker is left alone. Done
        # after the lock releases, matching the network-call-outside-lock
        # convention below.
        if (
            landed_terminal
            and session.origin is SessionOrigin.DAEMON
            and session.surface_ref is not None
        ):
            get_native_daemon_client().stop(session.surface_ref)
        return

    payload = _build_completed_payload(
        session, context, claude_session_id, hook_payload, rescued=rescued
    )
    record_event(OrchestratorEventType.SESSION_COMPLETED, payload)

    # Native bg workers stay registered with the Claude daemon as
    # ``idle`` after their turn ends; without an explicit stop they
    # accumulate in roster.json across dispatches (the very failure
    # mode that motivated GitHub issue #150 in the first place). The
    # stop call is best-effort: native_daemon.stop logs and swallows
    # missing-binary / timeout errors rather than failing the hook.
    if session.origin is SessionOrigin.DAEMON and session.surface_ref is not None:
        get_native_daemon_client().stop(session.surface_ref)


def _build_completed_payload(
    session: Session,
    context: dict[str, object],
    claude_session_id: object,
    hook_payload: dict[str, object],
    *,
    rescued: bool,
) -> dict[str, object]:
    """Build the SESSION_COMPLETED event payload.

    When *rescued* is True (a late Stop-hook sentinel salvaged an idle-parked
    task, #918) the payload carries ``rescued``/``rescue_reason``; those keys
    are omitted otherwise so the common path is unchanged (no new event type).
    Extracted to keep signal_stop under the branch cap.
    """
    payload: dict[str, object] = {
        "session_id": session.id,
        "session_name": session.name,
        "client": context.get("client"),
        "ticket_id": context.get("ticket_id"),
        "claude_session_id": claude_session_id,
        "hook_event": hook_payload.get("hook_event_name"),
        "crashed": False,
    }
    if rescued:
        payload["rescued"] = True
        payload["rescue_reason"] = "late_sentinel"
    return payload
