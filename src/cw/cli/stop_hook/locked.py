"""The Stop hook's ``sessions_lock`` window.

:func:`_resolve_stop_under_lock` is the only place the Stop hook takes
``sessions_lock``: the whole ``with sessions_lock():`` block -- session
lookup, idempotency and stale-hook guards, the USER-origin IDLE mark
(:func:`_handle_user_origin_stop`), and the headless resolution -- lives here
as one unit, and this is the only submodule that imports ``sessions_lock`` or
``load_state``. Imports ``headless``. Split out of the flat
``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cw.cli.stop_hook.headless import _resolve_and_complete_headless_session
from cw.config import (
    load_state,
    save_state,
    sessions_lock,
)
from cw.models import SessionOrigin, SessionStatus

if TYPE_CHECKING:
    from cw.cli.stop_hook.headless import _HeadlessResolution
    from cw.models import CwState, Session


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
