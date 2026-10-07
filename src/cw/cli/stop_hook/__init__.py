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

from cw._hook_context import _write_cw_context_locked
from cw.cli._base import handle_errors, main
from cw.cli._hook_io import _context_str
from cw.cli.stop_hook._constants import (
    _LOGGER_NAME,
    _SENTINEL_UNROUTABLE_PAGED_KEY,
    _SENTINEL_UNROUTABLE_REASON,
    _STAGED_ROUTE_RESCUED_KEY,
    _STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY,
)
from cw.cli.stop_hook.agent_stamp import (
    _agent_spawn_stamp_is_clear,
    _clear_agent_spawn_stamp,
    _snapshot_agent_spawn_stamp,
)
from cw.cli.stop_hook.headless import (
    _HeadlessResolution,
    _resolve_and_complete_headless_session,
)
from cw.cli.stop_hook.park import (
    _armed_running_task,
    _park_if_abandoned,
    _sentinel_frame_follows_marker,
)
from cw.cli.stop_hook.payload import (
    _read_stop_hook_payload,
    _resolve_signal_stop_context,
)
from cw.cli.stop_hook.sentinel import (
    _handle_headless_no_sentinel,
    _harvest_last_result_through_door,
    _parse_headless_sentinel,
    _reconstruct_emitted_sentinel,
    _verify_headless_scope,
)
from cw.cli.stop_hook.staged_emit import (
    _clear_staged_emit_result_marker,
    _maybe_clear_staged_emit_result,
    _maybe_stamp_sentinel_unroutable_paged,
    _peek_staged_emit_result,
    _restore_staged_route_outcome,
    _sentinel_unroutable_already_paged,
    _stamp_staged_route_outcome,
)
from cw.config import (
    load_state,
    save_state,
    sessions_lock,
)
from cw.events import record_event
from cw.models import (
    OrchestratorEventType,
    SessionOrigin,
    SessionStatus,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import holds_staged_emit_result

if TYPE_CHECKING:
    from cw.models import CwState, Session

logger = logging.getLogger(_LOGGER_NAME)

__all__ = [
    "_SENTINEL_UNROUTABLE_PAGED_KEY",
    "_SENTINEL_UNROUTABLE_REASON",
    "_STAGED_ROUTE_RESCUED_KEY",
    "_STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY",
    "_HeadlessResolution",
    "_LockedStop",
    "_agent_spawn_stamp_is_clear",
    "_armed_running_task",
    "_build_completed_payload",
    "_clear_agent_spawn_stamp",
    "_clear_staged_emit_result_marker",
    "_handle_headless_no_sentinel",
    "_handle_unrouted_stop",
    "_handle_user_origin_stop",
    "_harvest_last_result_through_door",
    "_maybe_clear_staged_emit_result",
    "_maybe_stamp_sentinel_unroutable_paged",
    "_page_sentinel_unroutable",
    "_park_if_abandoned",
    "_parse_headless_sentinel",
    "_peek_staged_emit_result",
    "_read_stop_hook_payload",
    "_reconstruct_emitted_sentinel",
    "_resolve_and_complete_headless_session",
    "_resolve_signal_stop_context",
    "_resolve_stop_under_lock",
    "_restore_staged_route_outcome",
    "_sentinel_frame_follows_marker",
    "_sentinel_unroutable",
    "_sentinel_unroutable_already_paged",
    "_snapshot_agent_spawn_stamp",
    "_stamp_staged_route_outcome",
    "_verify_headless_scope",
    "logger",
    "signal_stop",
]


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
    list AND the session holds no staged emit_cli result yet: the Stop hook
    fires at every main-agent turn boundary, and dispatching a
    ``run_in_background: true`` subagent ends the parent's turn while the
    subagent is still running. Completing the session here would orphan the
    subagent. See issue #151.

    A session that already holds a staged ``cw result emit`` result is
    routed immediately, even mid-background-task (#2458): the TASK is routed
    now, while the SESSION's own completion (``SESSION_COMPLETED`` + daemon
    stop) is deferred until ``background_tasks`` drains or the idle-sweep
    backstop acts. The one exception is a BlockedResult that lands the task
    terminal-FAILED, which stops the provably-leaked daemon worker at once.
    """
    resolved_context = _resolve_signal_stop_context()
    if resolved_context is None:
        return
    hook_payload, context, cwd_value, cw_session_id = resolved_context

    bg_tasks = hook_payload.get("background_tasks")
    bg_count = len(bg_tasks) if isinstance(bg_tasks, list) else 0
    if bg_count:
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
        # #1947: snapshot the live background_tasks count into cw-context.json
        # -- this is the replacement for the removed PostToolUse:Agent
        # decrement, which replaying a real async spawn showed balances at
        # launch-return rather than subagent completion. This field, unlike
        # that hook, tracks the harness's own turn-accounting. Fails open
        # silently (missing file, lock contention, malformed context) via
        # _write_cw_context_locked's own contract -- never raises, never
        # blocks the Stop hook's remaining duties.
        _write_cw_context_locked(
            cwd_value, lambda ctx: _snapshot_agent_spawn_stamp(ctx, bg_count)
        )
        # #2458: that "another Stop hook will fire" is not guaranteed (#1889:
        # the upstream async-completion wakeup can be dropped), so a result
        # the worker already emitted must not wait on it. Fast path: one
        # lock-free state read; with nothing staged the session is left
        # untouched exactly as before. Backstops if no further Stop ever
        # fires: the idle sweep routes a staged emit_cli result; the phantom
        # sweep handles a daemon that has exited.
        if not _peek_staged_emit_result(context):
            return
    elif not _agent_spawn_stamp_is_clear(context):
        # #1947: this Stop has background_tasks empty/absent -- clear any
        # stale agent_spawn_stamp snapshot a prior deferred turn left behind.
        # Runs here (before the session lookup) so it fires even when no
        # session in state.sessions matches this hook's session_id; fails
        # open silently, same contract as the snapshot write above.
        #
        # #2229: skipped when the stamp is already the resolved shape -- no
        # lock, no rewrite. Safe because (a) ``last_stamped_at`` is unread at
        # count 0 (``reconcile/_shared/_worktree_evidence.py`` returns early
        # on a zero count), and (b) the decision uses the unlocked read from
        # above, which is linearizable: an ``agent-spawn-pre`` increment
        # landing after that read is equivalent to "this Stop cleared first,
        # then the spawn incremented". The write path re-reads under the lock
        # and must never be handed ``context``.
        _write_cw_context_locked(cwd_value, _clear_agent_spawn_stamp)

    locked = _resolve_stop_under_lock(
        hook_payload,
        context,
        cwd_value=cwd_value,
        cw_session_id=cw_session_id,
        complete_session=not bg_count,
    )
    if locked is None:
        return
    session, claude_session_id, resolution = locked

    if resolution.rescued is None:
        _handle_unrouted_stop(session, context, resolution, bg_count)
        return

    if bg_count:
        # #2458: the staged result has routed the task; the session stays
        # live (and its daemon running) for the background work still in
        # flight. A later Stop with background_tasks drained completes it.
        # If none ever fires, reconcile's stranded-routed-result sweep pages
        # the operator once (#2524) and the operator closes it (cw doctor
        # --reap or cw spawn close); the idle sweep no longer routes it.
        return

    payload = _build_completed_payload(
        session,
        context,
        claude_session_id,
        hook_payload,
        rescued=resolution.rescued,
        task_already_terminal=resolution.task_already_terminal,
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


class _LockedStop(NamedTuple):
    """What ``signal_stop`` needs after ``sessions_lock`` releases."""

    session: Session
    claude_session_id: object
    resolution: _HeadlessResolution


def _resolve_stop_under_lock(
    hook_payload: dict[str, object],
    context: dict[str, object],
    *,
    cwd_value: str,
    cw_session_id: str,
    complete_session: bool,
) -> _LockedStop | None:
    """Look up the session and resolve this Stop under ``sessions_lock``.

    Returns ``None`` when ``signal_stop`` has nothing further to do: no such
    session, an already-settled one (idempotency guard), a stale hook, or a
    USER-origin session (marked IDLE here). Extracted from ``signal_stop``
    (#2458) to keep it under the branch/return caps.
    """
    # Why not mutate_state: dual-lock (dev_queue_lock nested at the TIMED_OUT path).
    # The daemon.stop() network call runs in signal_stop after this lock releases.
    with sessions_lock():
        state = load_state()
        session = next((s for s in state.sessions if s.id == cw_session_id), None)
        if session is None or session.status in (
            SessionStatus.COMPLETED,
            SessionStatus.IDLE,
            SessionStatus.TIMED_OUT,
        ):
            return None

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
            return None

        # Issue #165 Phase B: USER-origin sessions are interactive — the Stop
        # hook fires at every agent turn but the human is still driving. Mark
        # IDLE so wait loops / daemon triggers can react, but do NOT emit
        # SESSION_COMPLETED (no dev_queue task to retire) and do NOT call
        # native_daemon.stop (no roster entry to clean up). DAEMON-origin
        # falls through to the existing COMPLETED transition below.
        if session.origin is SessionOrigin.USER:
            _handle_user_origin_stop(state, session, claude_session_id)
            return None

        # Issue #176 Layer 1: headless backstop.
        #
        # A headless DAEMON session must NOT be silently marked COMPLETED unless
        # it emitted an AUTO_DEV_RESULT sentinel. The bg_tasks guard in
        # signal_stop defers when a subagent is in flight, but the parent's
        # *next* turn may end (with background_tasks=[]) before it has finished
        # its post-wait pipeline work — a silent orphan.
        #
        # Detection: DAEMON-origin + ``context["headless"]`` truthy.
        # Sentinel check: look for the sentinel open tag in the Claude transcript.
        # No sentinel: defer unconditionally (ADR-0014) — there is no wall-clock
        # budget and no TIMED_OUT transition — so another Stop hook (or
        # reconcile) can catch it later. A dispatch whose prompt never emits a
        # sentinel must therefore spawn with ``headless=False``.
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
        # authority, and (if accepted, and complete_session) marks the session
        # COMPLETED. rescued is None when the caller must bail without further
        # action -- no sentinel under budget, or a #1031 stage-mismatch route
        # refusal. landed_terminal (#1273) distinguishes the case where the
        # bail was itself a BlockedResult that landed the task terminal-FAILED,
        # leaking the daemon worker. A #1189 raced-to-terminal lookup miss
        # (#1692) is NOT a bail -- rescued comes back False (not None) and the
        # session completes normally, with task_already_terminal marking why.
        resolution = _resolve_and_complete_headless_session(
            state,
            session,
            context=context,
            cwd_value=cwd_value,
            claude_session_id=claude_session_id,
            ticket_id_value=ticket_id_value,
            is_headless=is_headless,
            now=now,
            complete_session=complete_session,
        )
    return _LockedStop(session, claude_session_id, resolution)


def _handle_unrouted_stop(
    session: Session,
    context: dict[str, object],
    resolution: _HeadlessResolution,
    bg_count: int,
) -> None:
    """Act on a ``rescued=None`` bail once ``sessions_lock`` has released.

    Pages ``sentinel_unroutable`` when the bail was neither a route refusal
    nor the #2135 park (:func:`_sentinel_unroutable`), then applies the #1273
    daemon stop. Runs identically whether or not ``background_tasks`` was
    pending (#2458): a leaked worker is leaked whatever it is still waiting
    on, and an unroutable staged result deserves a page either way.

    *bg_count* is the hook payload's ``background_tasks`` length at this
    Stop -- carried through only to log alongside the #1273 daemon stop
    (#2458), so a post-incident read can tell "stopped a leaked worker" from
    "stopped a worker with N background tasks still in flight" instead of
    inferring it from the surrounding Stop-hook fires.
    """
    if _sentinel_unroutable(session, resolution):
        # Defense in depth for a third, rarer failure mode: the hook DID run
        # and resolve, but the staged result is genuinely unroutable. It does
        # not by itself catch the dropped-wakeup shape (#1889 -- a session
        # that never fires another Stop at all); the idle-sweep backstop and
        # the background_tasks-routing in signal_stop close that between them.
        _page_sentinel_unroutable(session, context)
    # #1273: a BlockedResult that itself just landed the task
    # terminal-FAILED leaks the DAEMON worker -- stop it even though the
    # session is never marked COMPLETED. A stage-mismatch (#986) refusal
    # leaves landed_terminal False, so a still-legitimate worker is left
    # alone. Done after the lock releases, matching the
    # network-call-outside-lock convention in signal_stop.
    if (
        resolution.landed_terminal
        and session.origin is SessionOrigin.DAEMON
        and session.surface_ref is not None
    ):
        logger.info(
            "landed_terminal daemon stop: session=%s surface_ref=%s "
            "pending_background_tasks=%d",
            session.id,
            session.surface_ref,
            bg_count,
        )
        get_native_daemon_client().stop(session.surface_ref)


def _sentinel_unroutable(session: Session, resolution: _HeadlessResolution) -> bool:
    """Whether a ``rescued=None`` bail left a staged emit_cli result unrouted.

    True only when the session holds a terminal emit_cli result (so the
    emit-precedence path ran) and the bail was the no-sentinel one without
    the #2135 park -- i.e. that result's reconstruction AND the transcript
    fallback both failed. A route refusal (#1031, which fires
    ``SENTINEL_STAGE_MISMATCH``), a terminal landing (#1273) and the park
    (``stopped_without_sentinel``) each already have their own signal.

    #2458 fix cycle 6: also False once ``sentinel_unroutable_already_paged``
    -- a *different*, still-unroutable Stop landed on this exact bail before
    and already paged it. Without this, every subsequent bg_count==0
    drained-transition Stop for a session stuck in this bail re-pages
    forever, since (unlike the bg_count>0 fast path, which
    ``_peek_staged_emit_result`` short-circuits before resolution even runs)
    nothing else gates a bg_count==0 Stop from reaching this bail's
    resolution step every single time.
    """
    return (
        not resolution.landed_terminal
        and not resolution.parked_abandoned
        and not resolution.stage_mismatch_refused
        and not resolution.sentinel_unroutable_already_paged
        and holds_staged_emit_result(session)
    )


def _page_sentinel_unroutable(session: Session, context: dict[str, object]) -> None:
    """WARNING + signal-only ``session.needs_attention`` (#2458, §6d).

    No task-row mutation: the row stays RUNNING so the idle-sweep backstop or
    a later Stop can still route it. Fired outside ``sessions_lock``, like
    ``SESSION_COMPLETED``.
    """
    ticket_id = _context_str(context, "ticket_id")
    logger.warning(
        "sentinel_unroutable: session=%s ticket=%s last_result_source=%s -- "
        "a staged emit_cli result could not be reconstructed and the "
        "transcript carried no routable sentinel either",
        session.id,
        ticket_id,
        session.last_result_source,
    )
    record_event(
        OrchestratorEventType.SESSION_NEEDS_ATTENTION,
        {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": ticket_id,
            "claude_session_id": session.claude_session_id,
            "paused_status": _SENTINEL_UNROUTABLE_REASON,
            "breadcrumbs": (
                "Stop hook fired with an emit_cli result staged but no route "
                "was accepted (emit reconstruction and transcript parse both "
                "failed)"
            ),
            "crashed": False,
            "lane": session.lane,
        },
        correlation_id=ticket_id,
    )


def _build_completed_payload(
    session: Session,
    context: dict[str, object],
    claude_session_id: object,
    hook_payload: dict[str, object],
    *,
    rescued: bool,
    task_already_terminal: bool = False,
) -> dict[str, object]:
    """Build the SESSION_COMPLETED event payload.

    When *rescued* is True (a late Stop-hook sentinel salvaged an idle-parked
    task, #918) the payload carries ``rescued``/``rescue_reason``; those keys
    are omitted otherwise so the common path is unchanged (no new event type).
    When *task_already_terminal* is True (GitHub #1692: a #1189 raced-to-
    terminal lookup miss) the payload carries ``task_already_terminal`` so an
    operator can tell this completion apart from an ordinary one. Extracted
    to keep signal_stop under the branch cap.
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
    if task_already_terminal:
        payload["task_already_terminal"] = True
    return payload
