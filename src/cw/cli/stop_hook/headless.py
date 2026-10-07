"""Headless Stop resolution: sentinel lookup, task routing, session completion.

:func:`_resolve_and_complete_headless_session` runs under ``sessions_lock``
(via ``locked._resolve_stop_under_lock``): it resolves the headless sentinel
(#536 emit precedence, then the transcript parse), routes it through the #251
staged-advance authority, and marks the session COMPLETED, returning a
:class:`_HeadlessResolution` that tells ``signal_stop`` what to do once the
lock releases. Imports ``sentinel``, ``park`` and ``staged_emit``. Split out
of the flat ``cli/stop_hook.py`` (#2496).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from cw._hook_context import _write_cw_context_locked
from cw.cli.stop_hook.park import _park_if_abandoned
from cw.cli.stop_hook.sentinel import (
    _handle_headless_no_sentinel,
    _harvest_last_result_through_door,
    _parse_headless_sentinel,
    _reconstruct_emitted_sentinel,
)
from cw.cli.stop_hook.staged_emit import (
    _clear_staged_emit_result_marker,
    _maybe_clear_staged_emit_result,
    _maybe_stamp_sentinel_unroutable_paged,
    _restore_staged_route_outcome,
    _sentinel_unroutable_already_paged,
    _stamp_staged_route_outcome,
)
from cw.config import save_state
from cw.models import CompletionReason, SessionStatus
from cw.reconcile import (
    _apply_sentinel_to_task,
    _has_terminal_sentinel,
    _sentinel_partial_route_consumed,
    holds_staged_emit_result,
)

if TYPE_CHECKING:
    from datetime import datetime

    from cw.auto_dev_result import AutoDevResult, BlockedResult
    from cw.models import CwState, Session


class _HeadlessResolution(NamedTuple):
    """Result of ``_resolve_and_complete_headless_session`` (#1273).

    ``rescued`` is ``None`` when the caller must bail without further action
    (see that function's docstring); ``landed_terminal`` is ``True`` only
    when the bail was caused by a BlockedResult that itself just landed the
    task terminal-FAILED (``SentinelRouteOutcome.landed_terminal``) — the
    signal for ``signal_stop`` to stop the now-leaked DAEMON worker.

    ``task_already_terminal`` (GitHub #1692) mirrors
    ``SentinelRouteOutcome.task_already_terminal``: True when a same-ticket/
    session task had already been landed terminal by a concurrent caller
    before this call's own lookup ran. Unlike a stage-mismatch refusal, this
    sub-cause is safe to complete the session on — no dispatch path will ever
    give this session another leg for this ticket — so it is threaded through
    to ``_build_completed_payload`` rather than causing a bail.

    ``stage_mismatch_refused`` and ``parked_abandoned`` (#2458) tell the two
    ``rescued=None`` bails apart from a genuinely unroutable sentinel, so
    ``signal_stop`` pages ``sentinel_unroutable`` only for the latter:
    ``stage_mismatch_refused`` is True when the shared authority refused the
    route without landing the task terminal (a #1031 stage mismatch, which
    already fired ``SENTINEL_STAGE_MISMATCH``, or a non-terminal excluded row
    match); ``parked_abandoned`` is True when the #2135 park ran, which pages
    ``stopped_without_sentinel`` on its own.

    ``sentinel_unroutable_already_paged`` (#2458 fix cycle 6) is True when the
    no-sentinel/not-parked bail was *already* pageable on a previous call --
    i.e. ``_SENTINEL_UNROUTABLE_PAGED_KEY`` was already stamped on
    ``session.last_result`` before this call ran. It is computed from that
    pre-stamp state, not re-derived from ``session`` after this function
    returns, because the same bail also stamps the flag (see
    ``_maybe_stamp_sentinel_unroutable_paged``) before returning -- a
    post-hoc re-derivation in ``signal_stop`` would always see its own
    just-written stamp and never page even the first time. ``signal_stop``'s
    ``_sentinel_unroutable`` ANDs this field's negation into its page
    decision so a drained-transition (``background_tasks`` empty) Stop that
    re-lands on the same still-unroutable bail pages exactly once, not once
    per Stop -- unlike the other three fields above, a bg_tasks-pending Stop
    never even reaches this bail's resolution step at all once
    ``_peek_staged_emit_result`` starts answering False, so this field only
    ever matters for the bg_count==0 path that peek does not cover.
    """

    rescued: bool | None
    landed_terminal: bool
    task_already_terminal: bool = False
    stage_mismatch_refused: bool = False
    parked_abandoned: bool = False
    sentinel_unroutable_already_paged: bool = False


def _resolve_and_complete_headless_session(
    state: CwState,
    session: Session,
    *,
    context: dict[str, object],
    cwd_value: str,
    claude_session_id: object,
    ticket_id_value: object,
    is_headless: bool,
    now: datetime,
    complete_session: bool = True,
) -> _HeadlessResolution:
    """Resolve the headless sentinel and mark the session COMPLETED (#176, #251).

    Extracted from ``signal_stop`` to stay under the branch/return caps; owns
    the sentinel lookup, the #251 staged-advance routing, and the terminal
    session mutation + ``save_state``.

    Returns a ``_HeadlessResolution`` with ``rescued=None`` when the caller
    must bail without any further action: either no sentinel was found
    (``_handle_headless_no_sentinel`` defers — there is no wall-clock budget
    anymore — unconditionally *unless* ``_park_if_abandoned`` finds the
    worker's own recorded park marker in *context*, #2135, in which case the
    ticket's row is parked BLOCKED_ON_USER while the session itself is still
    left untouched),
    or the shared staged-advance authority refused the route on a stage
    mismatch (GitHub #1031, the #986 incident — extends #1019's phantom-path
    guard to the Stop-hook path). A stage-mismatch refusal leaves session and
    task completely untouched so a later reconcile tick or Stop hook can
    re-observe them — except when the refusal was itself a BlockedResult
    landing the task terminal-FAILED, signaled via ``landed_terminal=True``
    (#1273), which leaves the daemon worker leaked.

    GitHub #1692: a #1189 raced-to-terminal lookup miss (``outcome.
    task_already_terminal``) is NOT a bail — the task row is left untouched
    (a concurrent caller already landed it terminal), but the session
    completes normally below, since no dispatch path will ever give this
    session another leg for this ticket once its task is genuinely terminal.
    This closes the gap where such a session was previously left orphaned
    until the wall-clock reaper noticed it.

    Returns ``_HeadlessResolution(rescued=<bool>)`` once the session has been
    marked COMPLETED and persisted.

    #2458: ``complete_session=False`` (a Stop whose ``background_tasks`` is
    still non-empty) runs the sentinel resolution and task routing exactly
    as above -- including both bails -- but then returns WITHOUT the session's
    own completion: no status/``completed_at``/``completed_reason``/
    ``claude_session_id`` mutation, no ``save_state``, no harvest. Safe to
    split because ``_apply_sentinel_to_task`` takes its own
    ``dev_queue_lock``, persists its own queue write, and never mutates
    *session*. A later Stop (background work drained) completes the session.
    The idle sweep does not: once this route stamps the consumed marker,
    ``holds_staged_emit_result`` is False. If no later Stop ever fires, the
    stranded-routed-result detector (``cw.reconcile.routed_result_sessions``,
    #2524) pages the operator once, and the operator closes it with
    ``cw doctor --reap`` or ``cw spawn close``.
    """
    parsed_sentinel: AutoDevResult | BlockedResult | None = None
    # Issue #536: emit precedence. When the producer already pushed a
    # terminal result via ``cw result emit`` (session.last_result carries a
    # "status"), that value is authoritative — reconstruct it and skip the
    # transcript re-parse entirely. The transcript sentinel is demoted to a
    # forensic fallback: it still runs (and is still authoritative) whenever
    # no emitted result exists, so a worker that never emits is unaffected.
    emit_terminal = False
    # #2458 fix cycle 5, Action 1: captured once, ahead of the reconstruction
    # attempt, so both rescued=None bails below can gate their own staged-
    # emit-result neutralization on "was a staged result present at all"
    # rather than on emit_terminal, which is False on both the plain
    # never-emitted case AND the reconstruction-failure case below -- the
    # latter still needs the neutralization even though emit_terminal itself
    # stays False.
    has_staged_emit_source = is_headless and _has_terminal_sentinel(session)
    if has_staged_emit_source:
        parsed_sentinel = _reconstruct_emitted_sentinel(session)
        emit_terminal = parsed_sentinel is not None
    # #2458 fix cycle 4, Action 1: a prior complete_session=False partial
    # route already routed this emitted sentinel to its task and merged
    # _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY into last_result (see the
    # ``if not complete_session:`` branch below). Recognize that here so this
    # call -- a later Stop once background_tasks drains, or a repeat
    # partial-route Stop racing the context-flag clear -- skips
    # _apply_sentinel_to_task instead of re-deriving and re-routing the same
    # already-consumed sentinel a second time (duplicate PARK/terminal
    # events, spurious SENTINEL_RACE_MISS). last_result stays terminal-shaped
    # throughout, so emit_terminal/parsed_sentinel above are unaffected --
    # this call still completes the session from it normally.
    already_routed = emit_terminal and _sentinel_partial_route_consumed(session)
    if not emit_terminal and is_headless:
        parsed_sentinel = _parse_headless_sentinel(
            session, cwd_value, claude_session_id, ticket_id_value
        )
        if parsed_sentinel is None:
            _handle_headless_no_sentinel()
            parked = _park_if_abandoned(
                session, context, cwd_value, claude_session_id, ticket_id_value
            )
            # #2458 fix cycle 5, Action 1: left uncleared, a later
            # background_tasks-pending Stop re-peeks True and re-reaches this
            # same bail every turn, re-paging sentinel_unroutable each time
            # via _handle_unrouted_stop / _sentinel_unroutable's own
            # holds_staged_emit_result read. See
            # _maybe_clear_staged_emit_result. This only covers the
            # bg_count>0 path -- it does nothing for a bg_count==0
            # drained-transition Stop, which never consults the peek flag at
            # all (see signal_stop) and reaches this same bail unconditionally
            # every time background_tasks happens to be empty. The
            # already-paged stamp below closes that second path.
            _maybe_clear_staged_emit_result(has_staged_emit_source, cwd_value)
            # #2458 fix cycle 6: dedup the sentinel_unroutable page itself,
            # independently of the peek-flag clear above (which only ever
            # suppresses the bg_count>0 fast path). Computed and stamped here,
            # inside sessions_lock (via this function's caller), rather than
            # in _page_sentinel_unroutable itself (which runs after the lock
            # releases) -- see _SENTINEL_UNROUTABLE_PAGED_KEY and
            # _maybe_stamp_sentinel_unroutable_paged for why a post-lock
            # stamp-and-check would race its own write.
            already_paged = _sentinel_unroutable_already_paged(session)
            pageable = not parked and holds_staged_emit_result(session)
            _maybe_stamp_sentinel_unroutable_paged(
                state, session, already_paged=already_paged, pageable=pageable
            )
            return _HeadlessResolution(
                rescued=None,
                landed_terminal=False,
                parked_abandoned=parked,
                sentinel_unroutable_already_paged=already_paged,
            )

    # Issue #251: directly update the dev-queue task *before* marking the
    # session COMPLETED. This closes the race where revert_completed_silent_tasks
    # sees a COMPLETED session with a still-RUNNING task and reverts it to
    # PENDING before consume_completed_sessions can process the event — causing
    # no_op and similar terminal outcomes to trigger infinite re-dispatch.
    rescued = False
    task_already_terminal = False
    if already_routed:
        # #2458 fix cycle 5, Action 2: the accepted-route complete_session=
        # False call below stamped its outcome alongside the consumed flag --
        # restore it here instead of leaving rescued/task_already_terminal at
        # their init-False defaults, so this completing call's
        # SESSION_COMPLETED payload still reports a #918 late-sentinel rescue
        # accurately.
        rescued, task_already_terminal = _restore_staged_route_outcome(session)
    elif (
        is_headless and parsed_sentinel is not None and isinstance(ticket_id_value, str)
    ):
        outcome = _apply_sentinel_to_task(ticket_id_value, session, parsed_sentinel)
        rescued = outcome.rescued
        task_already_terminal = outcome.task_already_terminal
        if not outcome.routed and not outcome.task_already_terminal:
            # A refusal that did not itself land the task terminal (#1031
            # stage mismatch) already fired its own event (#2458). Fix cycle
            # 5, Action 1: also clear the staged-emit-result context flag --
            # left set, a later background_tasks-pending Stop re-peeks True,
            # re-derives the same staged sentinel (already_routed stays False
            # since it was never routed), and re-calls
            # _apply_sentinel_to_task, which unconditionally re-fires
            # SENTINEL_STAGE_MISMATCH via
            # cw.dispatch.routing._route_staged_decision on every turn.
            _maybe_clear_staged_emit_result(has_staged_emit_source, cwd_value)
            return _HeadlessResolution(
                rescued=None,
                landed_terminal=outcome.landed_terminal,
                stage_mismatch_refused=not outcome.landed_terminal,
            )

    if not complete_session:
        # #2458: the task is routed; the session's own completion waits for
        # its background work to drain (see this function's docstring).
        # Round-2 fix: clear the staged-emit-result peek flag now that the
        # route has landed -- left set, a later Stop with background_tasks
        # still non-empty re-derives the same already-consumed sentinel and
        # re-calls _apply_sentinel_to_task, which no longer finds the task
        # under this session (routed or landed terminal here) and logs a
        # spurious sentinel_race_miss_detected / SENTINEL_RACE_MISS on every
        # subsequent turn. Best-effort like every other context write in
        # this module: a missed clear just means the peek re-fires next
        # turn, exactly as it did before this fix.
        _write_cw_context_locked(cwd_value, _clear_staged_emit_result_marker)
        # Round-4 fix (#2458 Action 1): also neutralize the staged sentinel on
        # the authoritative Session model, not just the ephemeral context
        # peek flag -- a later Stop hook (once background_tasks finally
        # drains to []) and the idle sweep's holds_staged_emit_result
        # candidacy check both read session.last_result directly and would
        # otherwise still see this consumed sentinel as live. No-op when this
        # call never resolved an emitted sentinel (emit_terminal False -- the
        # transcript-only path never reaches complete_session=False, see this
        # function's docstring).
        #
        # Fix cycle 5, Action 3: guarded with `not already_routed` too -- a
        # repeat partial-route Stop (already_routed True, the narrow race the
        # comment below the already_routed assignment above describes) was
        # previously re-merging this same already-True flag and taking a
        # full fleet-wide save_state() purely to re-persist an unchanged
        # value every time. already_routed True also means rescued/
        # task_already_terminal were just restored from the earlier stamp
        # rather than freshly derived, so re-stamping them here would be
        # redundant for the same reason.
        if emit_terminal and not already_routed:
            _stamp_staged_route_outcome(
                state,
                session,
                rescued=rescued,
                task_already_terminal=task_already_terminal,
            )
        return _HeadlessResolution(
            rescued=rescued,
            landed_terminal=False,
            task_already_terminal=task_already_terminal,
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
    return _HeadlessResolution(
        rescued=rescued,
        landed_terminal=False,
        task_already_terminal=task_already_terminal,
    )
