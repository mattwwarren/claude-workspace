"""Act-phase session-state and dev-queue mutations for the emitted-sentinel router.

Evidence-only since the process-kill-timeout removal: only the
ROUTE_EMITTED_SENTINEL mutation remains. ``save_state`` itself is left to the
caller in ``core``. See GitHub #105, #121, #552, #578, #1031, ADR-0006.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import (
    CompletionReason,
    SessionStatus,
)
from cw.reconcile._shared import (
    _PAUSED_STATUS_KEY,
    _SENTINEL_STAGE_MISMATCH_REFUSED_REASON,
    _apply_sentinel_to_task_audited,
    _resolve_routed_sentinel,
)
from cw.result import reconstruct_staged_sentinel

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import Session
    from cw.reconcile._shared import ReapCandidate


def _apply_idle_routed_mutations(
    session_by_id: dict[str, Session],
    routed_sentinel_candidates: list[ReapCandidate],
    *,
    now: datetime,
) -> tuple[list[ReapCandidate], bool]:
    """Apply ROUTE_EMITTED_SENTINEL mutations for alive-idle workers (#1031).

    Shares ``phantom._apply_phantom_routed_mutations``'s routing shape: both
    route the emitted advance sentinel through the shared staged-advance
    authority (``_apply_sentinel_to_task`` -> ``apply_staged_decision``) via the
    shared ``_resolve_routed_sentinel`` guard (GitHub #1762), then mark the
    session COMPLETED/NORMAL -- but only when the route was accepted.

    Since #2458 the detect phase has two producers, like phantom's: the
    transcript re-parse (whose candidates carry a paired non-``None``
    ``salvage_csid``) and the staged ``cw result emit`` producer, which reads
    ``session.last_result`` and passes ``session.claude_session_id`` straight
    back -- possibly ``None``, which the shared ``_resolve_routed_sentinel``
    guard tolerates. A staged candidate is audited rather than re-emitted
    through the door (``audit_existing_result``).

    GitHub #1031 (extends #1019's phantom-path guard): when
    ``_apply_sentinel_to_task`` reports ``routed=False`` (a stage-mismatch
    refusal, the #986 incident) and the row is not terminal (#2140, #2482),
    the session must NOT be completed here --
    ``_detect_idle_candidates`` only builds these candidates when the surface
    is still reported alive by the daemon, so an unconditional completion
    would tear down a live surface, not just orphan a task row.

    GitHub #2140: a ``not routed`` outcome can also mean
    ``task_already_terminal`` -- the dev-queue task was raced to a genuinely
    terminal status by a concurrent caller before this lookup ran, not a
    stage-mismatch refusal. That case now routes through the door
    (``emit_result_on``) and completes the session instead of falling into
    the stage-mismatch refusal-marker branch, which would otherwise orphan
    this session forever (mirrors the Stop-hook's #1692 carve-out).

    GitHub #2482: ``outcome.landed_terminal`` -- a BlockedResult that itself
    just landed the task terminal-FAILED via the attempt-cap catch-all -- is
    the second terminal cause, handled like ``task_already_terminal``: the
    dev-queue row is genuinely terminal under this exact session id, so no
    dispatch path will give the session another leg and it is completed
    NORMAL rather than stamped with the refusal marker. This mirrors the Stop
    hook's #1273 arm (``_handle_unrouted_stop``) but deliberately diverges in
    shape: this function runs under ``sessions_lock`` (ADR-0019 / #1232 forbid
    a daemon call there) and holds no daemon handle, so it never stops the
    surface itself. Appending the candidate to ``accepted`` is what makes
    ``_emit_idle_completion_events`` queue the deferred surface stop that runs
    after the lock releases. The Stop hook, which stops directly because it
    runs after the lock, leaves its session ACTIVE; completing here avoids an
    ACTIVE session whose surface was stopped (the idle detector only offers
    live surfaces, so only the phantom sweep could reclaim it). Reachable
    today via the transcript producer, whose re-parse has no status filter;
    the staged producer only reaches it if ``cw result emit``'s
    ``_validate_or_exit`` gate is widened (see that gate's docstring and
    ``tests/test_result.py``'s
    ``test_validate_or_exit_rejects_bare_blocked_result_shape``).

    Returns ``(accepted, state_mutated)``. ``accepted`` is only the candidates
    actually routed or completed on a terminal row (#2140, #2482), so the
    caller's downstream event emission fires solely for those.
    ``state_mutated`` is True when any session state changed here -- including
    a refusal-marker stamp with no accepted candidate -- so the caller persists
    the stamp even on a pure-refusal tick (the marker would otherwise be lost
    and the candidate re-fire forever, GitHub #1149).
    """
    accepted: list[ReapCandidate] = []
    state_mutated = False
    for candidate in routed_sentinel_candidates:
        routed_sentinel = _resolve_routed_sentinel(candidate)
        if routed_sentinel is None:
            continue
        session = session_by_id[candidate.session_id]
        routed = True
        task_already_terminal = False
        landed_terminal = False
        # #2458: the staged-emit producer reconstructs routed_sentinel FROM
        # session.last_result, so a fresh door emit would refuse it as an
        # overwrite of itself (first-writer-wins) -- audit the already-staged
        # result instead. Ported verbatim from phantom's #1762 comparison; see
        # _apply_phantom_routed_mutations for why sentinels, not raw dicts, are
        # compared. A transcript-derived candidate has last_result None here,
        # so the comparison is False and that path is unchanged.
        audited = _apply_sentinel_to_task_audited(
            candidate.ticket_id,
            session,
            routed_sentinel,
            # #2458: the candidate's own result_source, not a hardcoded
            # SALVAGE_TRANSCRIPT literal -- this call now serves two
            # differently-sourced producers (the transcript-salvage one, and
            # the staged-emit one below), and hardcoding one source here
            # misattributed the other's audit event.
            source=candidate.result_source,
            audit_existing_result=(
                reconstruct_staged_sentinel(session.last_result) == routed_sentinel
            ),
        )
        if audited.emit is not None and audited.emit.refused:
            # #2140: the door refused a genuine overwrite attempt -- a foreign
            # authority's already-recorded terminal result must survive
            # byte-identical (first-writer-wins), so this candidate is
            # abandoned rather than completed.
            continue
        outcome = audited.route
        if outcome is not None:
            routed = outcome.routed
            task_already_terminal = outcome.task_already_terminal
            landed_terminal = outcome.landed_terminal
        # #2482 / #1273: both flags mean the dev-queue row is genuinely terminal
        # under this session id; they are mutually exclusive and only ever pair
        # with routed=False.
        row_terminal = task_already_terminal or landed_terminal
        if not routed and not row_terminal:
            # #1149: a stage-mismatch refusal (earlier-stage replay / unresolvable
            # position) leaves the task untouched. Stamp a paused_status-only
            # marker so the next tick's `session.last_result is None` unrouted-check
            # gate (_detect_idle_candidate_for_session) stops re-proposing this same
            # doomed candidate forever. No "status" key -> _has_terminal_sentinel
            # stays False.
            #
            # #2458: a staged emit_cli candidate reaches here too, and the
            # stamp replaces its staged result; with no "status" left,
            # _holds_staged_emit_result turns False and the candidate is not
            # re-offered. Only the emit's session.result_emitted audit event
            # (status + payload digest) survives -- a merge-aware stamp would
            # keep the full result but is a new door-guard write site (#2458
            # follow-up).
            session.last_result = {
                _PAUSED_STATUS_KEY: _SENTINEL_STAGE_MISMATCH_REFUSED_REASON
            }
            state_mutated = True
            continue
        if not routed and row_terminal:
            # #2140: the shared audited seam already accepted and recorded the
            # result after discovering the raced terminal queue row.
            #
            # #2482: a BlockedResult that itself landed the row terminal-FAILED
            # (attempt-cap catch-all) also completes the session. The surface
            # stop is queued by _emit_idle_completion_events once this candidate
            # is in `accepted`, and runs after sessions_lock releases. Unlike
            # the Stop hook, which bails with rescued=None and leaves the
            # session ACTIVE, the idle sweep completes it so a stopped surface
            # never sits behind an ACTIVE session.
            session.status = SessionStatus.COMPLETED
            session.completed_at = now
            session.completed_reason = CompletionReason.NORMAL
            session.claude_session_id = candidate.salvage_csid
            accepted.append(candidate)
            state_mutated = True
            continue
        session.status = SessionStatus.COMPLETED
        session.completed_at = now
        session.completed_reason = CompletionReason.NORMAL
        # #1762: guarded for the same reason as phantom's copy -- the shared
        # guard no longer proves salvage_csid is non-None, and blanking the id
        # the transcript lookups key off would be a silent regression.
        if candidate.salvage_csid is not None:
            session.claude_session_id = candidate.salvage_csid
        accepted.append(candidate)
        state_mutated = True
    return accepted, state_mutated
