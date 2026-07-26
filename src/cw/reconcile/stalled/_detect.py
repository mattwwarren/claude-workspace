"""Detect-phase classification for the stalled-headless sweep.

Pure classification helpers, extracted verbatim from the historical flat
``cw.reconcile.stalled`` module by the #1484 package split. Every function
here is read-only: zero writes to state, queue, or event bus. See GitHub
#185, #552, ADR-0006.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.auto_dev_result import (
    INTERMEDIATE_ADVANCE_STATUSES,
    AutoDevResult,
)
from cw.gh import pr_exists_for_branch
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    LivenessBucket,
    ReapReason,
    SessionOrigin,
    Stage,
)
from cw.reconcile import _deps, _shared
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _NEEDS_SALVAGE_REASON,
    _SILENTLY_IDLE_REASON,
    _STALLED_CAP_PARKED_REASON,
    ProposedAction,
    ReapCandidate,
    _has_terminal_sentinel,
    _is_headless,
    _parse_any_sentinel_from_transcript,
    _transcript_age_seconds,
    _validate_existing_result_for_routing,
    classify_sentinel_stage_position,
    feature_branch_key,
    resolve_headless_budget,
    resolve_stalled_retry_cap,
    ticket_id_for_session,
)
from cw.reconcile.liveness import _classify_liveness_bucket
from cw.reconcile.tasks import _client_cwd, _is_dangling_client
from cw.worktree import _has_commits_beyond_base

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorConfig,
        Session,
        TicketTask,
    )

_log = logging.getLogger(__name__)

_ACTIVE_WRITE_GRACE_WINDOW_SECONDS = 120
_ACTIVE_WRITE_HARD_CAP_MULTIPLIER = 2.0


def _resolve_finalize_blocked_condition(
    task: TicketTask | None,
    session: Session,
    wt_path: Path,
    default_branch: str,
    *,
    clients: dict[str, ClientConfig] | None = None,
    finalize_pr_by_branch: dict[str, tuple[bool | None, bool]] | None = None,
) -> tuple[bool, str | None]:
    """Return (is_finalize_blocked, branch_name_or_none).

    True when all conditions hold:
    - task.stage == Stage.FINALIZE
    - worktree has commits beyond origin/<default_branch>
    - no open PR exists for the feature branch
    - gh is available (gh-unavailable falls through to REVERT_TASK)

    clients: pre-loaded effective clients dict; loaded lazily if None.
    finalize_pr_by_branch: pre-computed pr_exists_for_branch results keyed by
      branch name, computed in reconcile()'s lockless pre-pass to avoid calling
      gh under sessions_lock (#485). Falls back to a direct call when None
      (used by tests and revert_stalled_headless_sessions).

    Returns (False, None) for any short-circuit.
    """
    if task is None or task.stage != Stage.FINALIZE:
        return False, None
    if not _has_commits_beyond_base(wt_path, default_branch):
        return False, None
    effective_clients = (
        clients if clients is not None else _deps.load_effective_clients()
    )
    branch = feature_branch_key(session.client, task.ticket_id, effective_clients)
    if finalize_pr_by_branch is not None:
        pr_result, gh_available = finalize_pr_by_branch.get(branch, (None, False))
    else:
        # Dangling client (clients.yaml populated but missing session.client) →
        # skip the unscoped gh call and fall through to REVERT_TASK, mirroring
        # this function's "any short-circuit → (False, None)" contract
        # (GitHub #1269/#1279 R7). The precomputed-map branch above is already
        # scoped upstream via core.py's _build_finalize_pr_map.
        if _is_dangling_client(session.client, effective_clients):
            _log.warning(
                "finalize_blocked_check_client_dangling ticket=%s client=%s: "
                "client missing from clients.yaml (config drift) -- gh call "
                "skipped, falling through to REVERT_TASK",
                task.ticket_id,
                session.client,
            )
            return False, None
        # Why: None means caller is outside sessions_lock (e.g.
        # revert_stalled_headless_sessions). Direct gh call is safe there.
        # _reconcile_locked converts None → {} so gh never runs under the lock
        # (#816 SHOULD_FIX 1).
        cwd = _client_cwd(session.client, effective_clients)
        pr_result, gh_available = pr_exists_for_branch(branch, cwd=cwd)
    # gh absent, PR already open, or transient error — fall through to REVERT_TASK.
    # Only pr_result is False (confirmed no open PR) triggers FINALIZE_BLOCKED.
    if not gh_available or pr_result is not False:
        return False, None
    return True, branch


def _maybe_append_salvage_skip_reset(
    candidates: list[ReapCandidate],
    session: Session,
    ticket_id: str | None,
) -> None:
    """Append a RESET_SALVAGE_SKIP_COUNTER candidate when the latch is nonzero.

    Shared by all 6 non-SKIP_PARKED detect-phase exits in
    _detect_stalled_candidates (#974, #1470). Gated at detect time on
    session.consecutive_salvage_skips != 0 so a session whose counter is
    already 0 never grows the per-tick candidate list. A session can thus
    yield TWO ReapCandidates in one pass — its own disposition candidate plus
    this reset — which is novel for this ticket.
    """
    if session.consecutive_salvage_skips != 0:
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.RESET_SALVAGE_SKIP_COUNTER,
                ticket_id=ticket_id,
                client=session.client,
            )
        )


def _stalled_advance_sentinel_candidate(
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    clients: dict[str, ClientConfig],
) -> ReapCandidate | None:
    """ROUTE_EMITTED_SENTINEL candidate for a wall-clock-expired stage-advance session.

    Path 1 backstop (GitHub #1149, R5). ``salvage_terminal_result`` deliberately
    excludes ``stage_complete`` (not in ``SALVAGE_TERMINAL_STATUSES``), so a
    ``stage_complete`` sentinel on a stalled session is invisible to stalled.py's
    own salvage check and the session is wall-clock-reverted despite having
    finished its stage cleanly. This mirrors ``phantom._phantom_advance_sentinel_
    candidate`` (parse any sentinel, filter to ``INTERMEDIATE_ADVANCE_STATUSES``)
    but ALSO resolves the sentinel's stage position against ``task.stage`` (R2):
    only a *same*- or *later*-stage sentinel is harvested here, where routing is
    expected to succeed (or park at a signoff gate) at apply time. An *earlier*-
    stage or unresolvable-position sentinel (a stale replay, #1019) returns
    ``None`` so detection falls through to the existing finalize-blocked /
    retry-cap / wall-clock-revert chain unchanged, and this backstop never
    intercepts stalled.py's fallback reap path for a stale sentinel.
    """
    if task is None:
        return None
    parsed = _parse_any_sentinel_from_transcript(session)
    if parsed is None:
        return None
    result, csid = parsed
    if (
        not isinstance(result, AutoDevResult)
        or result.status not in INTERMEDIATE_ADVANCE_STATUSES
    ):
        return None
    position, _stages, _target_idx = classify_sentinel_stage_position(
        task, result.model_dump(mode="json"), clients
    )
    if position not in ("same", "later"):
        return None
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.ROUTE_EMITTED_SENTINEL,
        ticket_id=ticket_id,
        routed_sentinel=result,
        salvage_csid=csid,
        elapsed_seconds=elapsed,
        lane=task.lane,
        client=session.client,
    )


def _append_routed_advance_candidate(
    candidates: list[ReapCandidate],
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    clients: dict[str, ClientConfig],
) -> bool:
    """Append a Path 1 backstop candidate if one applies; return whether it did.

    Mirrors the other recovery exits in ``_detect_stalled_candidates``: on a
    hit, also appends the RESET_SALVAGE_SKIP_COUNTER reset when the latch is
    nonzero (#974). Extracted to keep the detect loop under the statement cap;
    the caller ``continue``s when this returns True. See GitHub #1149.
    """
    routed_advance = _stalled_advance_sentinel_candidate(
        session, task, ticket_id, elapsed, clients
    )
    if routed_advance is None:
        return False
    _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
    candidates.append(routed_advance)
    return True


def _append_foreign_result_candidate(
    candidates: list[ReapCandidate],
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
) -> bool:
    """Append a COMPLETE_FOREIGN_RESULT candidate; return whether the guard fired.

    A live session whose ``last_result`` already carries a terminal sentinel
    from another authority (e.g. an out-of-band ``cw result emit``, which by
    contract never flips ``session.status``) must not be re-offered to the
    budget / transcript-salvage / retry-cap chain below -- that chain can only
    ever end in a door-refused SALVAGE_COMPLETION candidate silently dropped
    every tick (the exact unbounded-reoffer defect this ticket closes). Mirrors
    ``_append_routed_advance_candidate``: on a guard fire (True), also appends
    the RESET_SALVAGE_SKIP_COUNTER reset when the latch is nonzero (#974). The
    caller ``continue``s whenever this returns True, REGARDLESS of whether a
    disposition candidate was appended -- an unroutable/invalid foreign result
    still short-circuits the chain (drop-only fallthrough, R4) rather than
    being re-offered to it; it is not this ticket's scope to disposition an
    unroutable foreign write, only to stop re-parsing the transcript for one.
    See GitHub #1470.
    """
    if not _has_terminal_sentinel(session):
        return False
    _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
    validated_foreign = _validate_existing_result_for_routing(session.last_result)
    if validated_foreign is not None:
        candidates.append(
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.COMPLETE_FOREIGN_RESULT,
                ticket_id=ticket_id,
                routed_sentinel=validated_foreign,
                lane=task.lane if task else DEFAULT_LANE,
                client=session.client,
            )
        )
    return True


def _append_skip_parked_candidate(
    candidates: list[ReapCandidate],
    session: Session,
    task_by_ticket: dict[str, TicketTask],
) -> bool:
    """Append a SKIP_PARKED candidate for a session already parked by the idle
    watchdog; return whether the park-marker guard fired.

    Extracted from ``_detect_stalled_candidates``'s loop body to keep that
    function under the branch/statement cap (#1470). Detect returns the
    SKIP_PARKED candidate; act emits the skip event.
    """
    if not (
        isinstance(session.last_result, dict)
        and session.last_result.get("paused_status")
        in (_SILENTLY_IDLE_REASON, _NEEDS_SALVAGE_REASON)
    ):
        return False
    actual_paused_status = session.last_result.get("paused_status")
    ticket_id = ticket_id_for_session(session.name)
    # Stamp lane for SKIP_PARKED too so act phase has a consistent candidate.
    skip_task = task_by_ticket.get(ticket_id) if ticket_id else None
    candidates.append(
        ReapCandidate(
            session_id=session.id,
            proposed_action=ProposedAction.SKIP_PARKED,
            ticket_id=ticket_id,
            paused_status=str(actual_paused_status) if actual_paused_status else None,
            lane=skip_task.lane if skip_task else DEFAULT_LANE,
            client=session.client,
        )
    )
    return True


def _append_finalize_blocked_candidate(
    candidates: list[ReapCandidate],
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    effective_clients: dict[str, ClientConfig],
    finalize_pr_by_branch: dict[str, tuple[bool | None, bool]] | None,
) -> bool:
    """Append a PARK_FINALIZE_BLOCKED candidate if one applies; return whether it did.

    Extracted from ``_detect_stalled_candidates``'s loop body to keep that
    function under the branch/statement cap (#1470). Branch pushed but
    finalize failed before opening a PR (GitHub #812). Must run BEFORE the
    retry-cap check so the task is parked (preserving the worktree) rather
    than capped-and-abandoned. On a hit, also appends the
    RESET_SALVAGE_SKIP_COUNTER reset when the latch is nonzero (#974).
    """
    if session.worktree_path is None or task is None:
        return False
    fb_client_cfg = effective_clients.get(session.client)
    if fb_client_cfg is None:
        _log.warning(
            "finalize_blocked: unknown client %r for session %s — skipping",
            session.client,
            session.id,
        )
        return False
    fb_blocked, fb_branch = _resolve_finalize_blocked_condition(
        task,
        session,
        session.worktree_path,
        fb_client_cfg.default_branch,
        clients=effective_clients,
        finalize_pr_by_branch=finalize_pr_by_branch,
    )
    if not (fb_blocked and fb_branch is not None):
        return False
    # Why: recovery via finalize-park — a session can yield both its
    # PARK_FINALIZE_BLOCKED disposition and a RESET_SALVAGE_SKIP_COUNTER
    # candidate in the same pass (#974).
    _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
    candidates.append(
        ReapCandidate(
            session_id=session.id,
            proposed_action=ProposedAction.PARK_FINALIZE_BLOCKED,
            ticket_id=ticket_id,
            elapsed_seconds=elapsed,
            reap_reason=ReapReason.FINALIZE_BLOCKED,
            lane=task.lane,
            client=session.client,
            stage=task.stage,
            worktree_path=session.worktree_path,
            branch=fb_branch,
        )
    )
    return True


def _detect_stalled_candidates(
    state: CwState,
    *,
    now: datetime,
    config: OrchestratorConfig,
    task_by_ticket: dict[str, TicketTask],
    finalize_pr_by_branch: dict[str, tuple[bool | None, bool]] | None = None,
) -> list[ReapCandidate]:
    """Pure classification phase for stalled headless DAEMON sessions.

    Returns a list of ReapCandidate objects. Makes zero writes to state,
    queue, or event bus. See GitHub #552, ADR-0006.
    """
    # Load clients once for finalize-blocked branch-key resolution across all
    # sessions in this pass, rather than per-session inside the loop.
    effective_clients = _deps.load_effective_clients()
    candidates: list[ReapCandidate] = []
    for session in state.sessions:
        # Only live, headless DAEMON sessions are eligible for the stalled sweep.
        if (
            session.status not in _LIVE_STATUSES
            or session.origin is not SessionOrigin.DAEMON
            or not _is_headless(session)
        ):
            continue
        # Park-marker check: sessions already parked by the idle watchdog.
        if _append_skip_parked_candidate(candidates, session, task_by_ticket):
            continue
        ticket_id = ticket_id_for_session(session.name)
        task = task_by_ticket.get(ticket_id) if ticket_id else None
        # COMPLETE_FOREIGN_RESULT guard (#1470): runs BEFORE the budget gate so
        # a session already carrying a foreign terminal result is completed
        # immediately rather than only after its wall-clock budget expires
        # (R2). See _append_foreign_result_candidate's docstring.
        if _append_foreign_result_candidate(candidates, session, task, ticket_id):
            continue
        budget = resolve_headless_budget(task, session, config)
        elapsed = (now - session.started_at).total_seconds()
        if elapsed < budget:
            # Why: a session that recovers (elapsed back under budget after a
            # prior SKIP_PARKED streak isn't possible via this branch alone,
            # but a session can carry a nonzero latch from an earlier tick and
            # then clear the park-marker condition above; this is one of the
            # 5 non-SKIP_PARKED detect-phase exits that must reset the latch.
            # A session can therefore yield a RESET_SALVAGE_SKIP_COUNTER
            # candidate here with no disposition candidate alongside it (#974).
            _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
            continue
        # Try terminal-sentinel salvage before declaring timeout.
        salvage = _shared.salvage_terminal_result(session)
        if salvage is not None:
            result, claude_session_id = salvage
            # Why: recovery via salvage — this session yields TWO candidates
            # this pass (its own SALVAGE_COMPLETION disposition plus a
            # RESET_SALVAGE_SKIP_COUNTER reset), novel for this ticket (#974).
            _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.SALVAGE_COMPLETION,
                    ticket_id=ticket_id,
                    salvage_result=result,
                    salvage_csid=claude_session_id,
                    elapsed_seconds=elapsed,
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                )
            )
            continue
        # Path 1 backstop (#1149): a stage_complete advance sentinel is excluded
        # from SALVAGE_TERMINAL_STATUSES, so the salvage check above never sees
        # it. Harvest a same-/later-stage advance sentinel here (routing it via
        # the shared authority) BEFORE any finalize-blocked / cap / wall-clock
        # revert candidate is constructed, so a cleanly-completed stage is not
        # reverted. Earlier-stage / unresolvable sentinels fall through unchanged.
        if _append_routed_advance_candidate(
            candidates, session, task, ticket_id, elapsed, effective_clients
        ):
            continue
        # Finalize-blocked check: branch pushed but finalize failed before opening
        # a PR (GitHub #812). Must run BEFORE the retry-cap check so the task
        # is parked (preserving the worktree) rather than capped-and-abandoned.
        if _append_finalize_blocked_candidate(
            candidates,
            session,
            task,
            ticket_id,
            elapsed,
            effective_clients,
            finalize_pr_by_branch,
        ):
            continue
        cap = resolve_stalled_retry_cap(task, config)
        # Why: task.attempts is the shared counter for both this per-tier stalled cap
        # and the global attempt ceiling (#786). Do not add a parallel counter.
        if task is not None and task.attempts >= cap:
            # #1277: check the liveness veto FIRST — a session that is
            # demonstrably still making progress (fresh transcript) must not
            # be parked just because it also happens to be at the retry cap.
            # Previously the veto was only ever consulted from the ordinary
            # wall-clock revert path below, which this branch always
            # short-circuits via `continue`, making the veto structurally
            # unreachable for cap-exceeded sessions. Stamping
            # STALLED_RETRY_CAP_PARKED (rather than WALL_CLOCK_BUDGET) keeps
            # the emitted session.park_vetoed event's reason attributable to
            # the branch that actually produced it.
            cap_veto, veto_cap_exhausted = _liveness_veto_candidate(
                session,
                task,
                ticket_id,
                elapsed,
                now=now,
                config=config,
                reap_reason=ReapReason.STALLED_RETRY_CAP_PARKED,
                hard_elapsed_cap_seconds=budget * _ACTIVE_WRITE_HARD_CAP_MULTIPLIER,
            )
            if cap_veto is not None:
                _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
                candidates.append(cap_veto)
                continue
            # Why: recovery via retry-cap park — a session can yield both its
            # PARK_BLOCKED_ON_USER disposition and a RESET_SALVAGE_SKIP_COUNTER
            # candidate in the same pass (#974).
            _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
            # Why: this branch never previously computed usage_limit_detected —
            # unlike the revert path below, which mirrors idle.py's existing
            # usage-limit precedent, this cap-park usage-limit branch is new
            # logic (GitHub #1030).
            cap_usage_limit_detected = _shared.usage_limit_is_recent(
                _shared.detect_usage_limit(session),
                window_seconds=_shared.USAGE_LIMIT_BACKOFF_WINDOW_SECONDS,
            )
            candidates.append(
                ReapCandidate(
                    session_id=session.id,
                    proposed_action=ProposedAction.PARK_BLOCKED_ON_USER,
                    ticket_id=ticket_id,
                    elapsed_seconds=elapsed,
                    reap_reason=(
                        ReapReason.USAGE_LIMIT_CUTOFF
                        if cap_usage_limit_detected
                        else ReapReason.STALLED_RETRY_CAP_PARKED
                    ),
                    lane=task.lane if task else DEFAULT_LANE,
                    client=session.client,
                    stage=task.stage if task else DEFAULT_STAGE,
                    attempts=task.attempts if task else 0,
                    paused_status=_STALLED_CAP_PARKED_REASON,
                    usage_limit_detected=cap_usage_limit_detected,
                    # #1445: True only when the veto declined because the cap was
                    # reached on a still-LIVE session (not a genuine timeout).
                    veto_cap_exhausted=veto_cap_exhausted,
                )
            )
            continue
        # Why: recovery via ordinary wall-clock revert — a session can yield
        # both its REVERT_TASK disposition and a RESET_SALVAGE_SKIP_COUNTER
        # candidate in the same pass (#974). This is the 5th and final
        # non-SKIP_PARKED detect-phase exit (loop falls through, no continue).
        # The liveness veto (#976, #1277) is also checked from inside the
        # cap-exceeded branch above (with a different reap_reason) — this
        # call below only runs when that branch was not taken at all (task
        # is below the retry cap), not as a second chance for a cap-exceeded
        # session that already declined the veto.
        _maybe_append_salvage_skip_reset(candidates, session, ticket_id)
        candidates.append(
            _resolve_wall_clock_candidate(
                session, task, ticket_id, elapsed, now=now, config=config
            )
        )
    return candidates


def _liveness_veto_candidate(
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    *,
    now: datetime,
    config: OrchestratorConfig,
    reap_reason: ReapReason,
    hard_elapsed_cap_seconds: float | None = None,
) -> tuple[ReapCandidate | None, bool]:
    """Return ``(veto_candidate_or_None, cap_exhausted)`` for a stalled session.

    Computes fresh transcript-mtime staleness via :func:`_transcript_age_seconds`.
    A transcript active inside ``_ACTIVE_WRITE_GRACE_WINDOW_SECONDS`` gets a
    hard grace veto that is not charged against ``park_veto_cap`` until
    ``hard_elapsed_cap_seconds`` is reached: actively writing sessions must not
    be killed just because their wall-clock budget expired, but a continuously
    writing livelock still dies at the caller-provided hard cap (#1471).
    Less-fresh but still-LIVE sessions are classified through the same
    per-stage-floor liveness ladder the observability sweep
    (``cw.reconcile.liveness``) uses, and keep the bounded veto behavior from
    #976/#1445.

    The veto is bounded (#1445): once ``session.consecutive_park_vetoes`` reaches
    the cap, this returns ``(None, True)`` — no veto candidate, and the ``True``
    flag tells the caller the fallthrough park is due to *cap exhaustion* (a
    still-live session that has exhausted its veto budget) rather than an ordinary
    timeout, so the caller can escalate to the operator. The three return shapes:

    - ``(candidate, False)`` — actively writing inside the hard grace window:
      veto, with ``new_veto_count`` preserving the current counter.
    - ``(candidate, False)`` — LIVE and under the cap: veto, with the candidate's
      ``new_veto_count`` set to ``consecutive_park_vetoes + 1``.
    - ``(None, True)``       — LIVE and the count is *exactly* at the cap this
      tick (the first tick cap-exhaustion is observed): escalate. Deliberately
      ``==`` rather than ``>=`` — the wall-clock/SIGNAL_ONLY call site persists
      a bumped counter (``park_veto_cap + 1``) once it escalates (see
      ``_resolve_wall_clock_candidate`` and the act-phase loop that applies it),
      so a still-LIVE session that already escalated reads back ``> cap`` on
      every subsequent tick and this returns ``(None, False)`` instead —
      otherwise a session that stays LIVE past its cap would re-escalate every
      tick forever, reproducing the exact unbounded-side-effect defect class
      this ticket exists to close, just on the escalation channel. The
      cap-exceeded/retry-cap-park call site does not need this distinction —
      that branch's session is durably parked (removed from ``_LIVE_STATUSES``)
      the same tick it fires, so it can only ever observe the boundary once.
    - ``(None, False)``      — not LIVE, or transcript unlocatable (fail-toward-
      park): an ordinary timeout, NOT a cap-fire. A genuinely-dead session must
      never be misreported as "cap fired" even if its counter happens to sit at
      the cap from earlier ticks.

    Shared by two call sites (GitHub #1277): the ordinary wall-clock revert
    path (``reap_reason=WALL_CLOCK_BUDGET``) and the stalled-retry-cap park
    branch (``reap_reason=STALLED_RETRY_CAP_PARKED``). Detect-phase purity is
    preserved — only reads are added, never a write. See GitHub #976, #1445.
    """
    stage = task.stage if task is not None else DEFAULT_STAGE
    stale_seconds = _transcript_age_seconds(session, now)
    if stale_seconds is None:
        return None, False
    stale_minutes = stale_seconds / 60.0
    active_write_grace_allowed = (
        hard_elapsed_cap_seconds is None or elapsed < hard_elapsed_cap_seconds
    )
    if stale_seconds < _ACTIVE_WRITE_GRACE_WINDOW_SECONDS:
        if not active_write_grace_allowed:
            return None, False
        return (
            ReapCandidate(
                session_id=session.id,
                proposed_action=ProposedAction.PARK_VETOED,
                ticket_id=ticket_id,
                elapsed_seconds=elapsed,
                reap_reason=reap_reason,
                lane=task.lane if task else DEFAULT_LANE,
                client=session.client,
                stage=stage,
                attempts=task.attempts if task else 0,
                stale_minutes=stale_minutes,
                new_veto_count=session.consecutive_park_vetoes,
            ),
            False,
        )
    bucket = _classify_liveness_bucket(stale_minutes, stage=stage, config=config)
    if bucket is not LivenessBucket.LIVE:
        return None, False
    # LIVE — bound the veto (#1445). At/over the cap, decline the veto. Flag
    # exhaustion only on the exact tick the count reaches the cap (`==`, not
    # `>=`) so a call site that persists a past-cap bump after escalating
    # (see the wall-clock/SIGNAL_ONLY path) gets an edge-triggered signal
    # instead of a per-tick one.
    if session.consecutive_park_vetoes >= config.park_veto_cap:
        return None, session.consecutive_park_vetoes == config.park_veto_cap
    return (
        ReapCandidate(
            session_id=session.id,
            proposed_action=ProposedAction.PARK_VETOED,
            ticket_id=ticket_id,
            elapsed_seconds=elapsed,
            reap_reason=reap_reason,
            lane=task.lane if task else DEFAULT_LANE,
            client=session.client,
            stage=stage,
            attempts=task.attempts if task else 0,
            stale_minutes=stale_minutes,
            new_veto_count=session.consecutive_park_vetoes + 1,
        ),
        False,
    )


def _resolve_wall_clock_candidate(
    session: Session,
    task: TicketTask | None,
    ticket_id: str | None,
    elapsed: float,
    *,
    now: datetime,
    config: OrchestratorConfig,
) -> ReapCandidate:
    """Return PARK_VETOED when the session is still LIVE, else REVERT_TASK.

    Delegates the liveness check to :func:`_liveness_veto_candidate` with
    ``reap_reason=ReapReason.WALL_CLOCK_BUDGET``. See GitHub #976, #1277.
    """
    veto, veto_cap_exhausted = _liveness_veto_candidate(
        session,
        task,
        ticket_id,
        elapsed,
        now=now,
        config=config,
        reap_reason=ReapReason.WALL_CLOCK_BUDGET,
        hard_elapsed_cap_seconds=resolve_headless_budget(task, session, config)
        * _ACTIVE_WRITE_HARD_CAP_MULTIPLIER,
    )
    if veto is not None:
        return veto
    stage = task.stage if task is not None else DEFAULT_STAGE
    # #1030: branch reap_reason here (not after) so the SESSION_REAP_PROPOSED
    # audit event (emitted from the candidate before the apply phase runs)
    # reports the correct cause — matching the cap-park branch's pattern below.
    revert_usage_limit_detected = _shared.usage_limit_is_recent(
        _shared.detect_usage_limit(session),
        window_seconds=_shared.USAGE_LIMIT_BACKOFF_WINDOW_SECONDS,
    )
    return ReapCandidate(
        session_id=session.id,
        proposed_action=ProposedAction.REVERT_TASK,
        ticket_id=ticket_id,
        elapsed_seconds=elapsed,
        reap_reason=(
            ReapReason.USAGE_LIMIT_CUTOFF
            if revert_usage_limit_detected
            else ReapReason.WALL_CLOCK_BUDGET
        ),
        lane=task.lane if task else DEFAULT_LANE,
        client=session.client,
        stage=stage,
        attempts=task.attempts if task else 0,
        usage_limit_detected=revert_usage_limit_detected,
        # #1445: True only when the veto declined because the cap was reached
        # on a still-LIVE session — routes this REVERT to an immediate operator
        # escalation under SIGNAL_ONLY instead of a silent BLOCKED_ON_USER.
        veto_cap_exhausted=veto_cap_exhausted,
        # #1445: stamp the post-escalation counter value (cap + 1) so the act
        # phase can persist it BEFORE the veto is re-checked next tick. This is
        # what makes the escalation edge-triggered: next tick reads back a
        # count strictly greater than the cap, so _liveness_veto_candidate's
        # `==` check no longer reports exhaustion and the escalation does not
        # re-fire. Meaningless (left 0) when veto_cap_exhausted is False.
        new_veto_count=(config.park_veto_cap + 1) if veto_cap_exhausted else 0,
    )
