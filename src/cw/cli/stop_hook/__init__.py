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
from cw._util import claude_project_dir
from cw.auto_dev_result import AutoDevResult
from cw.cli._base import handle_errors, main
from cw.cli._hook_io import _context_str
from cw.cli._sentinels import (
    _parse_sentinel_from_transcript,
    _sentinel_frame_after,
)
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
from cw.cli.stop_hook.payload import (
    _read_stop_hook_payload,
    _resolve_signal_stop_context,
)
from cw.config import (
    load_state,
    save_state,
    sessions_lock,
)
from cw.events import record_event
from cw.exceptions import EmitSessionNotFoundError, EmitValidationError
from cw.models import (
    STAGED_EMIT_RESULT_KEY,
    CompletionReason,
    LastResultSource,
    OrchestratorEventType,
    SessionOrigin,
    SessionStatus,
    read_park_comment_marker,
)
from cw.native_daemon import get_native_daemon_client
from cw.reconcile import (
    _apply_sentinel_to_task,
    _has_terminal_sentinel,
    _route_stopped_without_sentinel,
    _sentinel_partial_route_consumed,
    _stamp_sentinel_partial_route_consumed,
    find_running_task_for_session,
    holds_staged_emit_result,
    park_gate_open,
)
from cw.result import emit_result_locked, reconstruct_staged_sentinel
from cw.worktree import reconcile_result_scope, resolve_scope_guard_default_branch

if TYPE_CHECKING:
    from cw.auto_dev_result import BlockedResult
    from cw.models import CwState, ParkCommentMarker, Session, TicketTask

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


def _parse_headless_sentinel(
    session: Session,
    cwd_value: str,
    claude_session_id: object,
    ticket_id_value: object = None,
) -> AutoDevResult | BlockedResult | None:
    """Parse the transcript sentinel for a headless Stop hook.

    Issue #799: when EnterWorktree shifts the hook cwd to a nested worktree,
    ``cwd_value`` derives the wrong Claude project dir. Retry with the session's
    recorded ``worktree_path`` — the directory whose project dir holds the actual
    transcript. Returns ``None`` when neither location yields a parseable sentinel.

    ``ticket_id_value`` is the Stop hook's own ``cw-context.json`` ticket id. It
    is forwarded to both scans as the expected identity (#2515) so the
    ``worktree_path`` fallback does not depend on that directory carrying its
    own ``cw-context.json`` -- the fallback scan fails closed without an identity.

    Extracted out of :func:`signal_stop` (rather than inlined) so the #536
    emit-precedence gate could be added there without pushing the function
    over its PLR0912 branch-count ceiling.
    """
    csid = claude_session_id if isinstance(claude_session_id, str) else None
    expected_ticket_id = (
        ticket_id_value
        if isinstance(ticket_id_value, str) and ticket_id_value
        else None
    )
    parsed = _parse_sentinel_from_transcript(
        cwd_value, csid, ticket_id=expected_ticket_id
    )
    # Rescan only a *different* directory: when the hook cwd already equals the
    # recorded worktree_path, the same transcript was just read (equal strings
    # encode to the same project dir), so a second pass repeats it byte for
    # byte. A per-call skip, not a cache -- nothing outlives this invocation.
    # A sentinel landing in the few ms between the two scans is caught by the
    # next Stop, exactly like one landing just after the second scan (deferring
    # is the fail-safe direction, ADR-0003).
    if (
        parsed is None
        and session.worktree_path is not None
        and str(session.worktree_path) != cwd_value
    ):
        parsed = _parse_sentinel_from_transcript(
            str(session.worktree_path), csid, ticket_id=expected_ticket_id
        )
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
    default_branch = resolve_scope_guard_default_branch(
        session.client, log_context=f"session={session.id}"
    )
    return reconcile_result_scope(
        result,
        worktree_path=session.worktree_path,
        default_branch=default_branch,
    )


def _reconstruct_emitted_sentinel(
    session: Session,
) -> AutoDevResult | BlockedResult | None:
    """Reconstruct the authoritative sentinel from an emitted ``last_result``.

    ``_has_terminal_sentinel`` only confirms a ``"status"`` key is present —
    it does not guarantee the dict matches the schema (e.g. a stale/foreign
    shape). Returns ``None`` on a validation failure so the caller falls back
    to the transcript parse instead of raising out of the Stop hook, which must
    never block claude from exiting.

    #1762 redirected the body onto the shared
    ``cw.result.reconstruct_staged_sentinel`` and widened the return type from
    ``AutoDevResult`` alone to the full discriminated union the door itself
    validates against: a worker can die holding a parser-synthesized
    ``BlockedResult``, which the narrower check rejected — sending an
    already-authoritative emitted result back through a transcript re-parse
    that #536's emit precedence exists to skip.
    """
    reconstructed = reconstruct_staged_sentinel(session.last_result)
    if reconstructed is None:
        logger.warning(
            "session=%s emitted last_result failed sentinel validation, "
            "falling back to transcript parse",
            session.id,
        )
    return reconstructed


def _handle_headless_no_sentinel() -> bool:
    """Resolve a sentinel-less headless Stop hook: always defer.

    Historically this checked the resolved headless wall-clock budget and, on
    expiry, marked the session TIMED_OUT, reverted its task, and stopped the
    daemon — killing whatever the worker was mid-way through. That
    process-kill timeout is removed: a Stop hook with no sentinel simply
    defers, unconditionally. A later Stop hook can still land the sentinel; a
    genuinely dead worker is caught by the phantom sweep (roster absence —
    evidence, not a timer); a quiet-but-live worker surfaces to the operator
    via the liveness distress signal. Returns True — the caller must stop
    processing.
    """
    return True


def _armed_running_task(session: Session, ticket_id: str) -> TicketTask | None:
    """The RUNNING row the #2135 park may fire for, or ``None``.

    The Stop hook fires at **every** main-agent turn boundary, so the park's
    evidence -- and above all the transcript walk whose cost grows with session
    length -- must not be read on every turn, and neither should a config file.
    The preconditions are therefore ordered by cost:

    1. headless DAEMON session -- already established, since ``signal_stop``
       returns before this path otherwise. ``background_tasks`` is empty on
       this path too, with one narrow exception since #2458: a Stop with
       pending background work reaches here only when the session holds a
       staged emit_cli result that failed reconstruction and the transcript
       carries no sentinel either;
    2. the RUNNING dev-queue row this session owns (one lock-free
       ``dev_queue.json`` read). No row, or a row that is not RUNNING, means
       there is nothing to park;
    3. the park flag for that row (:func:`park_gate_open`): the master switch
       plus the per-lane / per-ticket resolution. Memoized per process and
       fail-closed -- an unreadable config, an unknown client, an undeclared
       lane or an absent lane entry all read as disabled, and none of them
       raises out of the hook.

    Returns the row rather than a bool because the caller needs its ``stage``:
    that is the one field of the marker checked against a source independent of
    the marker's own file. With the park disabled -- the shipped default -- a
    sentinel-less Stop costs the row lookup and at most one config resolution,
    and behaves exactly as the pre-#2135 unconditional defer.
    """
    task = find_running_task_for_session(ticket_id, session.id)
    return task if task is not None and park_gate_open(task) else None


def _sentinel_frame_follows_marker(
    session: Session,
    cwd_value: str,
    claude_session_id: object,
    marker: ParkCommentMarker,
) -> bool:
    """Whether a sentinel frame appears after *marker* was stamped (#2135).

    True suppresses the park. Both candidate transcripts are read -- the hook's
    ``cwd`` project dir and the session's recorded ``worktree_path`` project dir
    (issue #799, for an EnterWorktree-shifted cwd). That is the same pair
    ``_parse_headless_sentinel`` searches, but not the same stopping rule: it
    falls back to the second location whenever the first yields no *sentinel*,
    so a first transcript that exists yet carries no frame must not end this
    search either. A frame is a hit wherever it lands.

    Returns True on the first frame hit or read failure, and False only once
    every transcript that exists has been read clean. A missing Claude session
    id, or no transcript in either location, also returns True: without the
    transcript a late frame cannot be ruled out, and the cost of being wrong
    that way is a silent non-park rather than a park that hides a real blocker
    reason behind the wrong disposition.
    """
    if not isinstance(claude_session_id, str) or not claude_session_id:
        return True
    search_dirs = [cwd_value]
    if session.worktree_path is not None:
        search_dirs.append(str(session.worktree_path))
    found_transcript = False
    # ``dict.fromkeys`` drops a repeated directory (the common case: the hook's
    # cwd IS the worktree) so the hot path never reads the same file twice.
    for search_dir in dict.fromkeys(search_dirs):
        transcript_path = claude_project_dir(search_dir) / f"{claude_session_id}.jsonl"
        if not transcript_path.is_file():
            continue
        found_transcript = True
        if _sentinel_frame_after(transcript_path, marker.posted_at):
            return True
    return not found_transcript


def _park_if_abandoned(
    session: Session,
    context: dict[str, object],
    cwd_value: str,
    claude_session_id: object,
    ticket_id_value: object,
) -> bool:
    """Park the ticket's row when this Stop looks like an abandoned exit (#2135).

    Returns True when the park was attempted -- every precondition held and
    ``_route_stopped_without_sentinel`` ran, which pages on its own -- so the
    caller does not page ``sentinel_unroutable`` a second time (#2458). False
    on every fail-closed early return.

    Called only after the sentinel parse has already returned ``None``. The
    park ships **dark**: with ``park_on_abandoned_exit_enabled`` false (the
    default) :func:`_armed_running_task` returns ``None`` before the marker is
    read or any transcript is opened, and the caller defers exactly as it did
    before #2135.

    The evidence is the worker's own ``park_comment_marker``, written by ``cw
    signal-park`` after its park comment posted -- a RECORDED CLAIM by the
    producer, never an observation by cw that a tracker comment exists. It must
    cover this session id, this ticket id and the RUNNING row's stage; a
    malformed marker is an absent marker.

    The transcript is then consulted for NEGATIVE evidence only
    (:func:`_sentinel_frame_follows_marker`): a frame marker at or after the
    stamp suppresses the park, because a truncated, unpaired or placeholder
    frame parses to ``None`` and would otherwise be stamped
    ``stopped_without_sentinel``, hiding the worker's real blocker reason. It
    can never cause a park, and any read or parse trouble counts as a frame.

    Order (cheapest first): ticket id, RUNNING row, park flag, marker (an
    in-memory lookup in the context dict the hook already parsed), transcript.
    Fail-closed at every step -- each returns without parking, and none raises.

    Accepted limitation: a worker that dies between deciding its exit and
    running the stamp leaves no marker, and this defers exactly as it did
    before #2135.
    """
    if not isinstance(ticket_id_value, str) or not ticket_id_value:
        return False
    task = _armed_running_task(session, ticket_id_value)
    if task is None:
        return False
    marker = read_park_comment_marker(context)
    if marker is None or not marker.covers(
        session_id=session.id, ticket_id=ticket_id_value, stage=task.stage
    ):
        return False
    if _sentinel_frame_follows_marker(session, cwd_value, claude_session_id, marker):
        return False
    _route_stopped_without_sentinel(ticket_id_value, session)
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

    Best-effort: a validation failure, missing session, or state read/write
    failure is logged and swallowed, never raised -- the Stop hook must never
    block claude from exiting. There is no fallback write; a failure here just
    means ``last_result`` stays whatever it already was. A failed
    ``session.result_emitted`` audit append is not among these: the door
    already logs it and still persists the result (#2465), so it never reaches
    this handler. A refusal (terminal result already present) is not logged
    again here -- ``emit_result_locked`` already emits its own warning on
    refusal.
    """
    try:
        emit_result_locked(
            sentinel.model_dump(mode="json"),
            session_id,
            source=LastResultSource.STOP_HOOK_HARVEST,
        )
    except OSError as exc:
        logger.warning(
            "stop-hook harvest state read/write failed for session %s; "
            "allowing the Stop hook to exit: %s",
            session_id,
            exc,
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


def _maybe_clear_staged_emit_result(
    has_staged_emit_source: bool, cwd_value: str
) -> None:
    """Clear the staged-emit-result context flag on a ``rescued=None`` bail.

    #2458 fix cycle 5, Action 1: shared by both bail branches in
    ``_resolve_and_complete_headless_session`` below -- left set, a later
    background_tasks-pending Stop re-peeks True and re-reaches the same bail
    every turn, re-firing that bail's event each time (a spurious
    ``SENTINEL_STAGE_MISMATCH`` re-derivation, or a repeat ``sentinel_
    unroutable`` page). Gated on *has_staged_emit_source* so the common
    ordinary Stop-hook turn for a session that never called ``cw result
    emit`` never pays a context-lock write for a no-op clear.
    """
    if has_staged_emit_source:
        _write_cw_context_locked(cwd_value, _clear_staged_emit_result_marker)


def _restore_staged_route_outcome(session: Session) -> tuple[bool, bool]:
    """Restore ``(rescued, task_already_terminal)`` stamped by a prior
    ``complete_session=False`` partial route (#2458 fix cycle 5, Action 2).

    The completing call's ``already_routed`` short-circuit skips
    ``_apply_sentinel_to_task`` entirely, so without this the eventual
    ``SESSION_COMPLETED`` payload would silently drop a #918 late-sentinel
    rescue it never re-derived.
    """
    staged_result = session.last_result
    if isinstance(staged_result, dict):
        return (
            bool(staged_result.get(_STAGED_ROUTE_RESCUED_KEY, False)),
            bool(staged_result.get(_STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY, False)),
        )
    return False, False


def _stamp_staged_route_outcome(
    state: CwState, session: Session, *, rescued: bool, task_already_terminal: bool
) -> None:
    """Merge the consumed flag and this call's routing outcome, then persist.

    #2458 fix cycle 5, Actions 2 and 3: the consumed flag (round 4) lets a
    later completing call skip re-deriving an already-routed sentinel; the
    outcome alongside it (this cycle) lets that same later call restore
    ``rescued``/``task_already_terminal`` via
    :func:`_restore_staged_route_outcome` instead of leaving them at their
    init-False defaults. Caller guards this with ``not already_routed`` so a
    repeat partial-route Stop skips the redundant merge and fleet-wide
    ``save_state``.
    """
    _stamp_sentinel_partial_route_consumed(session)
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {
            **existing,
            _STAGED_ROUTE_RESCUED_KEY: rescued,
            _STAGED_ROUTE_TASK_ALREADY_TERMINAL_KEY: task_already_terminal,
        }
    save_state(state)


def _sentinel_unroutable_already_paged(session: Session) -> bool:
    """True when a prior bail already paged ``sentinel_unroutable`` (#2458 fix cycle 6).

    Reads ``_SENTINEL_UNROUTABLE_PAGED_KEY``. Computed *before* this call's own
    bail decides whether to stamp the flag -- see
    :func:`_maybe_stamp_sentinel_unroutable_paged` -- so a fresh
    ``_HeadlessResolution`` field can carry the pre-stamp state through to
    ``signal_stop``'s outside-the-lock ``_sentinel_unroutable`` check without
    that check racing its own just-written stamp.
    """
    last_result = session.last_result
    return (
        isinstance(last_result, dict)
        and last_result.get(_SENTINEL_UNROUTABLE_PAGED_KEY) is True
    )


def _maybe_stamp_sentinel_unroutable_paged(
    state: CwState, session: Session, *, already_paged: bool, pageable: bool
) -> None:
    """Merge ``_SENTINEL_UNROUTABLE_PAGED_KEY`` in once, the first time this
    bail is pageable (#2458 fix cycle 6).

    No-op when already paged (nothing to add) or not pageable (the #2135 park
    ran, or nothing is actually staged -- ``holds_staged_emit_result`` is
    False -- in which case ``_sentinel_unroutable`` will refuse to page on its
    own conditions regardless, so stamping here would be a needless
    fleet-wide ``save_state`` for a page that was never going to fire).
    """
    if already_paged or not pageable:
        return
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {**existing, _SENTINEL_UNROUTABLE_PAGED_KEY: True}
        save_state(state)


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


def _peek_staged_emit_result(context: dict[str, object]) -> bool:
    """Lock-free peek: does this session hold a staged emit_cli result? (#2458)

    Reads the ``staged_emit_result`` flag ``cw result emit`` stamps into this
    worktree's own ``cw-context.json`` (#2458) -- never ``load_state()``,
    whose fleet-wide ``sessions.json`` this peek exists specifically to avoid
    loading on every Stop-hook fire with pending ``background_tasks``. A
    stale answer cannot corrupt anything: True just means the caller takes
    the lock and re-makes every decision from a freshly loaded session; False
    means it defers exactly as it did before #2458, which a later Stop or the
    idle sweep recovers from.
    """
    if not context.get("headless"):
        return False
    return bool(context.get(STAGED_EMIT_RESULT_KEY))


def _clear_staged_emit_result_marker(context: dict[str, object]) -> dict[str, object]:
    """Drop ``STAGED_EMIT_RESULT_KEY`` -- the counterpart of the stamp
    ``cw.result._stamp_staged_emit_result`` writes.

    Called once a ``complete_session=False`` partial route has consumed the
    staged result (see ``_resolve_and_complete_headless_session``), so a
    later ``_peek_staged_emit_result`` on the same still-deferred session
    answers False instead of re-triggering a re-derivation of an
    already-routed sentinel.
    """
    return {k: v for k, v in context.items() if k != STAGED_EMIT_RESULT_KEY}


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
