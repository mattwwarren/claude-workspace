"""``AUTO_DEV_RESULT`` sentinel parsing, salvage and staged-emit latches.

Parses a sentinel out of a transcript (including salvage of a terminal
result and its scope check), classifies its stage position, maps a salvaged
result onto a queue status, and owns the ``session.last_result`` latches the
staged-emit and stage-refusal paths read and stamp. Imports ``_constants``
and ``_transcripts``; defers its ``cw.dispatch`` import to function level
(#698). Split out of the flat ``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cw._transcript import locate_transcript
from cw._util import _iter_sentinel_text_blocks
from cw.auto_dev_result import (
    BLOCKER_REASON_NO_RESULT_EMITTED,
    SALVAGE_TERMINAL_STATUSES,
    AutoDevResult,
    BlockedResult,
    parse_last_block_per_chunk,
    queue_status_for_terminal_sentinel,
)
from cw.models import (
    ClientConfig,
    CompletionReason,
    LastResultSource,
    QueueItemStatus,
    SessionStatus,
    Stage,
    TicketTask,
)
from cw.reconcile._shared._constants import (
    _PAUSED_STATUS_KEY,
    _SENTINEL_ADVANCE_REFUSED_KEY,
    _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
)
from cw.reconcile._shared._roster import ticket_id_for_session
from cw.reconcile._shared._transcripts import (
    _newest_surface_ref_transcript,
    _session_project_dir,
)
from cw.result import (
    EmitOutcome,
    emit_result_on_audited,
    has_terminal_result,
    reconstruct_staged_sentinel,
)
from cw.worktree import reconcile_result_scope, resolve_scope_guard_default_branch

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.dispatch import _StagePosition
    from cw.models import Session


def _queue_status_for_salvaged(result: AutoDevResult) -> QueueItemStatus:
    """Map a salvaged AutoDevResult to the appropriate QueueItemStatus.

    Delegates to the shared dispatch/salvage classifier (#1566) so this path
    cannot drift from live dispatch's Rule 1/2/5/3b hold routing again.
    """
    return queue_status_for_terminal_sentinel(result.status)


def _validate_existing_result_for_routing(
    existing_result: dict[str, Any] | None,
) -> AutoDevResult | BlockedResult | None:
    """Validate a door-refused foreign ``existing_result`` for routing, or None.

    Reuses the door's own discriminated validation
    (:func:`cw.result._validate_harvest_payload`) against a **foreign, untrusted**
    dict written by an unknown authority. A validation failure means the shape is
    unroutable, so the caller falls through to the PENDING-requeue floor.

    RFC 0012 A3 (#1459): this read-side foreign-shape check is the one deliberate
    exception to R6's "no defensive ladder around this ticket's own emit sites"
    rule -- it validates a dict this ticket did NOT construct, so a
    ``EmitValidationError`` here is a genuine "is this even routable" reading, not
    a widening of a known-valid payload.

    Promoted here from ``cw.reconcile.concierge`` (#1470) so stalled.py's
    COMPLETE_FOREIGN_RESULT detect-phase guard can reuse it without a
    cross-module private import; concierge.py's own call site now delegates here.

    #1762 moved the body itself down to ``cw.result.reconstruct_staged_sentinel``
    -- the phantom sweep and the Stop hook need the same reconstruction, and
    ``cw.result`` owns the union it validates against. This name survives as the
    reconcile-side vocabulary for the *foreign-shape* reading (see above), not as
    a second implementation.
    """
    return reconstruct_staged_sentinel(existing_result)


def _foreign_result_target_queue_status(
    validated: AutoDevResult | BlockedResult,
) -> QueueItemStatus:
    """Map a validated foreign result to its target QueueItemStatus.

    Extracted from ``cw.reconcile.concierge``'s
    ``_route_park_marker_poison_task`` isinstance/status ladder (#1470) so both
    concierge.py's park-marker-poison routing and stalled.py's
    COMPLETE_FOREIGN_RESULT routing share one mapping.

    isinstance(BlockedResult) MUST precede the delegated
    ``_queue_status_for_salvaged`` call: ``BlockedResult`` has no
    ``AutoDevResult`` fields, so the isinstance check is what proves the else
    branch's operand is an ``AutoDevResult`` for mypy --strict (RFC 0012 A3
    #1459). The former ``status == "blocked"`` special case (#1470's "round-3
    bug fix") is gone: "blocked" is in ``STAGE_FAILURE_STATUSES``, so
    ``queue_status_for_terminal_sentinel`` now routes it to BLOCKED_ON_USER
    without a special case (#1566).
    """
    if isinstance(validated, BlockedResult):
        return QueueItemStatus.BLOCKED_ON_USER
    return _queue_status_for_salvaged(validated)


# Alias so _salvage_terminal_result can reference the shared constant by the
# private-looking name used throughout this module. The real definition lives
# in auto_dev_result.py as SALVAGE_TERMINAL_STATUSES — single source of truth
# so reconcile.py and cli.py cannot drift apart. See GitHub issues #372, #431.
_SALVAGE_TERMINAL_STATUSES: frozenset[str] = SALVAGE_TERMINAL_STATUSES


def _parse_sentinel_from_blocks(
    path: Path, *, ticket_id: str | None = None
) -> AutoDevResult | BlockedResult | None:
    """Parse the LAST real sentinel block in the transcript.

    Scans candidate blocks via :func:`_iter_sentinel_text_blocks` — assistant
    text AND ``tool_result`` (Bash stdout) blocks — through
    :func:`~cw.auto_dev_result.parse_last_block_per_chunk`: the last real
    sentinel block in a chunk decides for that chunk, and the last chunk that
    decides wins (§3.1 "the LAST block wins", GitHub #591). Documented-example
    blocks (the illustrative ``pr=42 / PROJ-1234`` block in the skill prompt)
    and unresolved placeholder blocks are skipped; if only those are present,
    returns None. A chunk quoting several blocks no longer collapses to
    ``multiple_result_blocks``: its last real block is the result (GitHub
    #2515), so a salvage chunk that used to be requeued can now salvage.

    ``ticket_id`` is the current session's expected identity. Reconcile callers
    pass it from the session name so a quoted sibling-ticket result is skipped
    before salvage routing. It remains optional for direct low-level callers.

    A worker may emit the sentinel via ``cat <<EOF`` rather than as assistant
    text, landing the frame in a tool_result block; scanning only assistant
    text misses it and the stage stalls (GitHub #731). Returns ``None`` when no
    chunk carries a complete real frame; a truncated frame reads as none.
    """
    return parse_last_block_per_chunk(
        _iter_sentinel_text_blocks(path), ticket_id=ticket_id
    )


def _salvage_terminal_result(
    session: Session,
) -> tuple[AutoDevResult, str] | None:
    """Recover a terminal-success AUTO_DEV_RESULT from the session's transcript.

    A headless session that emitted a valid sentinel and then stalled (e.g.
    sitting in ``wait_for_ci``) or crashed before session lifecycle completion
    may have its disposition lost. This recovers it directly from the transcript.

    Uses the same two-layer transcript search as
    :func:`_parse_any_sentinel_from_transcript` (csid-exact, then surface_ref-
    newest fallback when the csid transcript is absent or has no sentinel) so a
    terminal sentinel written before a resume/backfill is never missed
    (GitHub #1353; mirrors the #892 fix already applied to that sibling).

    Returns ``(result, claude_session_id)`` only when the parsed result is an
    :class:`AutoDevResult` whose status is in :data:`_SALVAGE_TERMINAL_STATUSES`.
    Returns ``None`` otherwise.
    """
    parsed = _parse_any_sentinel_from_transcript(session)
    if parsed is None:
        return None
    result, csid = parsed
    if (
        isinstance(result, AutoDevResult)
        and result.status in _SALVAGE_TERMINAL_STATUSES
    ):
        return result, csid
    return None


def _verify_salvaged_scope(result: AutoDevResult, session: Session) -> AutoDevResult:
    """Correct a salvaged sentinel's self-reported scope against git facts (#1487).

    Salvage recovers a sentinel the worker wrote about itself; nothing has
    checked its ``scope.files``/``scope.lines_actual`` against the branch. A
    config failure must not cost us the sentinel, so an unresolvable client
    falls back to the ``main`` default rather than propagating.
    """
    default_branch = resolve_scope_guard_default_branch(
        session.client, log_context=f"session={session.id}"
    )
    return reconcile_result_scope(
        result,
        worktree_path=session.worktree_path,
        default_branch=default_branch,
    )


def _parse_any_sentinel_from_transcript(
    session: Session,
) -> tuple[AutoDevResult | BlockedResult, str] | None:
    """Parse any sentinel from the transcript, regardless of status.

    Like :func:`_salvage_terminal_result` but applies no status filter — returns
    the result for any valid parse including PAUSED_FOR_USER_INPUT statuses that
    :func:`_salvage_terminal_result` would skip.  Returns None when no sentinel
    framing is present (the no-frame case ``parse_stdout`` reports as
    BLOCKER_REASON_NO_RESULT_EMITTED).

    Uses a two-layer transcript search (mirrors queue_peek's locate logic):

    - Layer 1: csid-exact transcript (``<project_dir>/<csid>.jsonl``).  Returned
      immediately when a sentinel is found, so the csid transcript always wins
      when it contains the result.
    - Layer 2: surface_ref newest-only transcript (``<surface_ref>*.jsonl``).
      Fires when the csid transcript is absent **or** contains no sentinel — the
      latter catches the case where a REVIEW worker emitted the sentinel before
      spawning fanout subagents (the sentinel lives in the pre-resume V1 transcript
      while backfill has already updated ``claude_session_id`` to the resumed V2
      session that has no sentinel).  Skipped when Layer 2 would return the same
      path already tried in Layer 1.

    Used by the ROUTE_EMITTED_SENTINEL detection path for sessions where the
    sentinel was emitted but the Stop hook never fired.  See GitHub #578, #731,
    #892.
    """

    def _try(path: Path) -> tuple[AutoDevResult | BlockedResult, str] | None:
        result = _parse_sentinel_from_blocks(
            path, ticket_id=ticket_id_for_session(session.name)
        )
        if result is None or (
            isinstance(result, BlockedResult)
            and result.blocker.reason == BLOCKER_REASON_NO_RESULT_EMITTED
        ):
            return None
        if isinstance(result, AutoDevResult):
            result = _verify_salvaged_scope(result, session)
        return result, path.stem

    project_dir = _session_project_dir(session)

    # Layer 1: csid-exact (does NOT fall through to surface_ref)
    csid_transcript: Path | None = None
    if session.claude_session_id is not None and project_dir is not None:
        csid_transcript = locate_transcript(
            project_dir=project_dir,
            claude_session_id=session.claude_session_id,
            surface_ref=None,
            started_at=session.started_at,
        )
        if csid_transcript is not None:
            parsed = _try(csid_transcript)
            if parsed is not None:
                return parsed

    # Layer 2: surface_ref newest-only — fires when csid transcript absent or
    # contains no sentinel.  Skip when it resolves to the same path as Layer 1.
    if session.surface_ref is not None and project_dir is not None:
        surface_transcript = _newest_surface_ref_transcript(project_dir, session)
        if surface_transcript is not None and surface_transcript != csid_transcript:
            return _try(surface_transcript)

    return None


def classify_sentinel_stage_position(
    task: TicketTask,
    last_result: dict[str, object] | None,
    clients: dict[str, ClientConfig],
) -> tuple[_StagePosition, list[Stage] | None, int | None]:
    """Circular-safe re-export of dispatch's stage-position classifier (#1149).

    ``cw.dispatch`` imports ``cw.reconcile`` at module level, so a top-level
    import of the classifier from a reconcile sweep would create a cycle. This
    thin wrapper (co-located with ``_apply_sentinel_to_task``, which delegates to
    dispatch the same way) lets stalled.py's Path 1 backstop resolve a sentinel's
    stage position against ``task.stage`` without an inline import at its own call
    site. Returns ``(position, stages, target_idx)``; see
    ``dispatch._classify_sentinel_stage_position`` for the semantics.

    The classifier is part of the dispatch routing engine
    (``routing/stage_walk.py``), not a pure queue-row helper, so #2613 left it
    out of ``cw.queue_rows``: the engine imports ``cw.executor``, which imports
    ``cw.reconcile`` at module top (``executor/core.py``). The dependency
    inversion that would let this import move to module scope is tracked in
    #2619.
    """
    from cw.dispatch import _classify_sentinel_stage_position

    return _classify_sentinel_stage_position(task, last_result, clients)


def _apply_salvaged_completion(
    session: Session,
    result: AutoDevResult,
    claude_session_id: str,
    *,
    now: datetime,
) -> EmitOutcome:
    """Mark ``session`` COMPLETED from a salvaged sentinel (like signal_stop).

    RFC 0012 A3 (#1459): the ``last_result`` write is routed through the door
    (:func:`emit_result_on`, source=SALVAGE_TRANSCRIPT) FIRST. On a first-
    writer-wins refusal -- another authority already recorded a terminal result
    for this session -- the status/completed_at/completed_reason/cost_usd/
    claude_session_id mutations are ALL skipped and the refusal ``EmitOutcome``
    is returned so the caller can drop this candidate from its downstream
    ticket-routing / event-emission accounting. The door's own ``emit_result_on``
    warning already logs ``existing_source``/``attempted_source`` on refusal, so
    no duplicate log is emitted here.

    Returns the ``EmitOutcome`` (``refused=True`` when the door declined). All
    four callers (phantom/idle/stalled/concierge) check ``.refused``.
    """
    outcome = emit_result_on_audited(
        session,
        result.model_dump(mode="json"),
        source=LastResultSource.SALVAGE_TRANSCRIPT,
    )
    if outcome.refused:
        return outcome
    session.status = SessionStatus.COMPLETED
    session.completed_at = now
    session.completed_reason = CompletionReason.NORMAL
    if result.cost_usd is not None:
        session.cost_usd = result.cost_usd
    session.claude_session_id = claude_session_id
    return outcome


def holds_staged_emit_result(session: Session) -> bool:
    """True when *session* carries a staged, still-routable ``cw result emit`` result.

    The shared base predicate for "an emit_cli result is staged and not yet
    routed" -- EMIT_CLI source plus a terminal-shaped sentinel. Hoisted here
    (#2458) after three independent reviewers found this exact pair
    re-derived four times across three files (``cw.cli.stop_hook``'s
    ``_peek_staged_emit_result`` and ``_sentinel_unroutable``,
    ``cw.reconcile.idle._detect``'s ``_holds_staged_emit_result``, and
    ``cw.cli.spawn``'s ``_route_staged_emit_result``): this predicate is the
    load-bearing gate for the exact defect class #2458 exists to fix, so a
    future change to what counts as "staged and routable" now has one site to
    update instead of four. Each caller ANDs its own extra context-specific
    condition (DAEMON origin, no route refusal yet, etc.) on top of this base
    pair.
    """
    return (
        session.last_result_source is LastResultSource.EMIT_CLI
        and _has_terminal_sentinel(session)
        and not _sentinel_partial_route_consumed(session)
    )


def _sentinel_partial_route_consumed(session: Session) -> bool:
    """True when a #2458 partial route already routed this staged sentinel.

    See ``_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY`` for why this is a merged-in
    flag rather than a ``_PAUSED_STATUS_KEY`` replacement: the session's
    terminal-shaped ``last_result`` must stay terminal-shaped (so a later Stop
    hook can still complete the session from it) while no longer counting as
    *staged and routable* for ``holds_staged_emit_result``'s callers.
    """
    last_result = session.last_result
    return (
        isinstance(last_result, dict)
        and last_result.get(_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY) is True
    )


def _stamp_sentinel_partial_route_consumed(session: Session) -> None:
    """Merge ``_SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY`` into ``session.last_result``.

    Mutates *session* in place; the caller is responsible for ``save_state``.
    A no-op guard (``isinstance`` check) mirrors the merge-safe convention
    ``stalled/_mutations.py`` and ``phantom/_mutations.py`` already use for
    ``_SENTINEL_ADVANCE_REFUSED_KEY`` -- called only when ``last_result`` is
    already known terminal-shaped (the emit-precedence path that ran
    immediately before this call), so the ``dict`` branch is the only one that
    should ever execute; the ``else`` is defensive, not an expected path.
    """
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {
            **existing,
            _SENTINEL_PARTIAL_ROUTE_CONSUMED_KEY: True,
        }


def stage_refusal_latched(session: Session) -> bool:
    """True once a stage-mismatch refusal was latched on *session* (#1149, #2490).

    The one read side of the refusal latch the phantom, stalled and local-harvest
    sweeps (and the idle sweep's staged-result producer) all stamp: the
    single-key ``_PAUSED_STATUS_KEY`` marker, or the ``_SENTINEL_ADVANCE_REFUSED_KEY``
    flag merged into a ``last_result`` dict that already held other content.
    """
    last_result = session.last_result
    return isinstance(last_result, dict) and (
        last_result.get(_PAUSED_STATUS_KEY) == _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        or last_result.get(_SENTINEL_ADVANCE_REFUSED_KEY) is True
    )


def stamp_stage_refusal(session: Session) -> None:
    """Latch a stage-mismatch refusal on *session* (the write side).

    A refused candidate leaves the task row and the session untouched, so
    without a latch every tick re-detects it and re-refuses it forever. A
    pre-existing ``last_result`` dict is merged into under its own key, never
    overwritten: it may carry another sweep's ``paused_status`` park marker
    (idle's ``silently_idle``, salvage's ``needs_salvage``) that stalled's
    SKIP_PARKED check still has to read. Only a missing ``last_result`` gets the
    single-key stamp. Mutates *session* in place; the caller owns ``save_state``.

    The idle sweep's own stamp (``idle/_mutations.py``) is a deliberate
    single-key variant (#2458) and does not use this helper.
    """
    existing = session.last_result
    if isinstance(existing, dict):
        session.last_result = {**existing, _SENTINEL_ADVANCE_REFUSED_KEY: True}
    else:
        session.last_result = {
            _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
        }


def _has_terminal_sentinel(session: Session) -> bool:
    """True when the session has already emitted a terminal sentinel.

    A real AUTO_DEV sentinel dump always carries a ``"status"`` key; the park
    markers (``silently_idle``/``needs_salvage``) carry ``"paused_status"`` and
    no ``"status"``. Key presence — not value — is the structural discriminant,
    so a parked session is correctly NOT treated as terminal and the idle
    watchdog re-checks it for a late terminal sentinel. See #418, #497.

    Thin delegation onto ``cw.result.has_terminal_result`` (RFC 0012 S2,
    #1456), which now owns this predicate since the emit_result_locked door
    also uses it to arbitrate first-writer-wins.
    """
    return has_terminal_result(session.last_result)
