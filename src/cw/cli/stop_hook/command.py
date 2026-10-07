"""The ``cw signal-stop`` click command and its post-lock actions.

:func:`signal_stop` is the Stop hook's entrypoint (``@main.command(name=
"signal-stop")``): it handles the ``background_tasks`` deferral and the
``agent_spawn_stamp`` writes, delegates the ``sessions_lock`` window to
``locked._resolve_stop_under_lock``, and then -- after the lock releases --
emits ``SESSION_COMPLETED``, pages ``sentinel_unroutable`` and stops the
native daemon worker. Both ``get_native_daemon_client().stop(...)`` calls live
here, outside the lock. Imports ``_constants``, ``payload``, ``agent_stamp``,
``staged_emit`` and ``locked``; never ``sessions_lock`` or ``load_state``.
Split out of the flat ``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw._hook_context import _write_cw_context_locked
from cw.cli._base import handle_errors, main
from cw.cli._hook_io import _context_str
from cw.cli.stop_hook._constants import _LOGGER_NAME, _SENTINEL_UNROUTABLE_REASON
from cw.cli.stop_hook.agent_stamp import (
    _agent_spawn_stamp_is_clear,
    _clear_agent_spawn_stamp,
    _snapshot_agent_spawn_stamp,
)
from cw.cli.stop_hook.locked import _resolve_stop_under_lock
from cw.cli.stop_hook.payload import _resolve_signal_stop_context
from cw.cli.stop_hook.staged_emit import _peek_staged_emit_result
from cw.events import record_event
from cw.models import OrchestratorEventType, SessionOrigin
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import holds_staged_emit_result

if TYPE_CHECKING:
    from cw.cli.stop_hook.headless import _HeadlessResolution
    from cw.models import Session

logger = logging.getLogger(_LOGGER_NAME)


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
