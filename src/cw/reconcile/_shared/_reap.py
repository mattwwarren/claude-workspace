"""Reap policy resolution and ``SESSION_REAP_PROPOSED`` emission.

Resolves the per-lane reap policy and attempt ceiling, derives the feature
branch key, and emits the signal-only reap proposal (ADR-0006). Imports
``_transcripts`` and ``_types``. Split out of the flat
``reconcile/_shared.py`` (#2214).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cw.config import save_state
from cw.events import record_event
from cw.models import (
    ClientConfig,
    OrchestratorConfig,
    OrchestratorEventType,
    ReapPolicy,
    ReapReason,
    TicketTask,
)
from cw.reconcile._shared._transcripts import (
    _locate_session_transcript,
    _transcript_age_seconds,
)
from cw.reconcile._shared._types import (
    ProposedAction,
    ReapCandidate,
    _apply_correction_signal_fields,
)

if TYPE_CHECKING:
    from cw.models import CwState


def resolve_reap_policy(
    candidate: ReapCandidate,
    clients: dict[str, ClientConfig],
    global_cfg: OrchestratorConfig,
) -> ReapPolicy:
    """Resolve the effective reap_policy for a candidate.

    Precedence (highest to lowest):
      1. Lane-level LaneConfig.reap_policy in the candidate's client config.
      2. Global OrchestratorConfig.reap_policy.
      3. ReapPolicy.SIGNAL_ONLY fail-safe (built into OrchestratorConfig default).

    A candidate whose client is absent from *clients* or whose lane name is not
    declared in that client's lanes falls through to the global config. This
    keeps behaviour identical to the pre-#560 flat read for any candidate that
    predates lane stamping.
    """
    client_cfg = clients.get(candidate.client) if candidate.client else None
    if client_cfg is not None:
        for lane_cfg in client_cfg.effective_lanes:
            if lane_cfg.name == candidate.lane and lane_cfg.reap_policy is not None:
                return lane_cfg.reap_policy
    return global_cfg.reap_policy


def resolve_attempt_ceiling(
    client: ClientConfig | None,
    task: TicketTask,
    global_cfg: OrchestratorConfig,
) -> int | None:
    """Resolve the effective attempt ceiling for a task's lane.

    Precedence (highest to lowest):
      1. Lane-level ``LaneConfig.attempt_ceiling`` for ``task.lane``.
      2. Global ``OrchestratorConfig.global_attempt_ceiling``.

    Returns ``None`` when the lane sets ``attempt_ceiling=False`` — an explicit
    "this lane has no ceiling", not a missing value. A supervised lane
    (``signoff: operator``) has a human answering every park, so the human IS
    the rate limiter an automated bound exists to be; that is the case #1751
    exists to express. Callers must therefore treat ``None`` as "never park on
    the ceiling", never as "fall back to some default".

    A ``None`` *client* (unresolvable / removed from clients.yaml) or a lane
    name not declared in that client's lanes falls through to the global
    config, keeping behaviour identical to the pre-#1751 flat read — the same
    fallthrough contract :func:`resolve_reap_policy` gives its own candidates.

    Takes a single ``client`` rather than the ``clients`` dict its sibling
    resolvers take: neither real call site holds a dict of every client at that
    depth (``cw.dispatch.claim`` is handed one ``ClientConfig``; the concierge
    detect functions do a per-task ``get_client()``), and threading one down
    would touch four call chains for no gain.

    Lives here, not in ``cw.dispatch``, because both consumers need it and the
    import direction only runs ``cw.dispatch -> cw.reconcile`` (#786, #1750,
    #1751).
    """
    if client is not None:
        for lane_cfg in client.effective_lanes:
            if lane_cfg.name == task.lane and lane_cfg.attempt_ceiling is not None:
                if lane_cfg.attempt_ceiling is False:
                    return None
                return lane_cfg.attempt_ceiling
    return global_cfg.global_attempt_ceiling


def feature_branch_key(
    client_name: str,
    ticket_id: str,
    clients: dict[str, ClientConfig],
) -> str:
    """Return the git branch key for a ticket, respecting feature_branch_prefix.

    Looks up the client's :attr:`ClientConfig.feature_branch_prefix` (SSOT for
    the branch name the staged pipeline provisions and the auto-dev skills push
    to). Falls back to ``"dev"`` when the client is absent from *clients* so
    behaviour is identical to the old hardcoded ``"dev/" + ticket_id``.

    See GitHub issue #728.
    """
    client = clients.get(client_name)
    prefix = client.feature_branch_prefix if client is not None else "dev"
    return f"{prefix}/{ticket_id}"


_REAP_PROPOSED_ACTIONS: frozenset[ProposedAction] = frozenset(
    {
        ProposedAction.REVERT_TASK,
        ProposedAction.CRASH_COMPLETE,
        ProposedAction.PARK_BLOCKED_ON_USER,
        ProposedAction.CLOSE_ROUTED_RESULT_SESSION,
    }
)


def _emit_reap_proposed(
    state: CwState,
    candidates: list[ReapCandidate],
    *,
    native_live: set[str],
    now: datetime | None = None,
) -> set[str]:
    """Emit SESSION_REAP_PROPOSED for reap-shaped candidates before act phase.

    Called from _reconcile_locked after each _detect_* and before the
    corresponding _act_on_*. Satisfies ADR-0006 invariant 3 (propose before act).

    Only emits for REVERT_TASK, CRASH_COMPLETE, PARK_BLOCKED_ON_USER and the
    proposal-only CLOSE_ROUTED_RESULT_SESSION (#2524) candidates.
    Dedup: sessions with reap_proposed_at already set are skipped.

    Returns the set of session_ids newly stamped in this call. Callers use this
    to gate edge-triggered events (e.g. SESSION_STAGE_TIMED_OUT_RETRIED) so they
    fire only on first detection, not on every re-detect tick. See GitHub #782.

    save_state is safe under sessions_lock — it is a raw file write, not a
    reentrant lock acquisition. See existing _act_on_stalled_candidates,
    _act_on_idle_candidates.

    evidence.transcript_age_seconds reuses the same content-aware staleness
    computation (_transcript_age_seconds) the liveness veto decided on (#1427);
    evidence.transcript_mtime_age_seconds is the raw file-mtime age, retained
    separately for diagnostics.
    """
    _now = now or datetime.now(UTC)
    session_by_id = {s.id: s for s in state.sessions}
    newly_stamped: set[str] = set()

    for candidate in candidates:
        if candidate.proposed_action not in _REAP_PROPOSED_ACTIONS:
            continue
        session = session_by_id.get(candidate.session_id)
        if session is None or session.reap_proposed_at is not None:
            continue

        in_roster = (
            session.surface_ref is not None and session.surface_ref in native_live
        )

        # Content-aware staleness — same computation the liveness veto used to
        # make its park/no-park decision (#976, #1277), so the audit evidence
        # never diverges from what was actually decided (#1427).
        transcript_age_seconds = _transcript_age_seconds(session, _now)

        # Raw mtime age, retained separately for diagnostics only — a trailing
        # metadata-only record (queue-operation/ai-title/mode/...) can bump
        # this far above transcript_age_seconds; do not confuse the two (#1427).
        transcript_mtime_age_seconds: float | None = None
        transcript_path = _locate_session_transcript(session)
        if transcript_path is not None and transcript_path.exists():
            with contextlib.suppress(OSError):
                mtime = transcript_path.stat().st_mtime
                transcript_mtime_age_seconds = _now.timestamp() - mtime

        payload: dict[str, object] = {
            "session_id": session.id,
            "session_name": session.name,
            "client": session.client,
            "ticket_id": candidate.ticket_id,
            "lane": candidate.lane,
            "proposed_action": candidate.proposed_action.value,
            "reason": candidate.reap_reason.value if candidate.reap_reason else None,
            "evidence": {
                "elapsed_seconds": candidate.elapsed_seconds,
                "in_roster": in_roster,
                "transcript_age_seconds": transcript_age_seconds,
                "transcript_mtime_age_seconds": transcript_mtime_age_seconds,
            },
        }
        # #1625: stalled_retry_cap_parked carries the correction-signal fields
        # (crashed is always False on this park path — it never corresponds to
        # a crash) so a consumer doesn't have to cross-reference the task
        # record by hand. Scoped strictly to this reap_reason — other reasons
        # (wall-clock budget, usage-limit cutoff, etc.) do not carry these keys.
        if candidate.reap_reason == ReapReason.STALLED_RETRY_CAP_PARKED:
            payload["crashed"] = False
            _apply_correction_signal_fields(payload, candidate)
        # Stamp before record_event: dedup guard fires on retry if write fails.
        session.reap_proposed_at = _now
        newly_stamped.add(candidate.session_id)
        record_event(
            OrchestratorEventType.SESSION_REAP_PROPOSED,
            payload,
            correlation_id=candidate.ticket_id or candidate.session_id,
        )

    if newly_stamped:
        save_state(state)
    return newly_stamped
