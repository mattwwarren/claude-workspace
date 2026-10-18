"""Close a stranded shipped session once its own PR is open or merged (#2715).

A #2458 ``complete_session=False`` partial route routes a session's staged
``cw result emit`` result -- the ticket's row advances -- but leaves the
session ACTIVE until a later Stop completes it. When that Stop never fires,
the session holds a client-ceiling slot and its worktree indefinitely, and
:mod:`cw.reconcile.routed_result_sessions` only pages it (#2524). The stalled
sweep skips EMIT_CLI results, the idle sweep skips a consumed staged result,
and the phantom sweep skips a roster-live surface, so nothing closes it.

This sweep closes the subset whose recorded result is ``shipped`` with a PR,
once the evidence for it is complete (ADR-0006 amendment #2715):

- the session is in the #2524 population
  (:func:`~cw.reconcile.routed_result_sessions.find_stranded_routed_sessions`:
  live DAEMON, consumed marker, roster-live, no lane-occupying row bound to
  it, transcript flat past the stage's grace bucket, not draining);
- its ``last_result`` came from ``cw result emit`` and rebuilds to a
  ``shipped`` :class:`~cw.auto_dev_result.AutoDevResult` with a PR;
- the worktree's ``agent_spawn_stamp`` is readable, carries this session's
  own id, and is at least ``fix_loop_await_deadline_minutes`` old
  (:func:`_drain_quiesced`, the fail-closed twin of the draining veto);
- the sentinel's own PR is among the branch's PRs and is OPEN or MERGED.

The trigger is the recorded result plus the PR evidence. The age checks (the
transcript bucket and the drain-stamp deadline) only postpone the act: with no
PR evidence, or an unreadable drain signal, no amount of elapsed time closes
anything (ADR-0014 invariant 1).

The act completes no queue row: the route already advanced it. It flips the
session COMPLETED/NORMAL with ``reap_reason = ROUTED_RESULT_STRANDED``,
re-checking the pin against a fresh in-lock queue read under
``dev_queue_lock`` (ADR-0019 order), then emits one ``session.completed``
carrying the own-PR ``pr_url`` and queues the daemon stop on the post-lock
sink. The ``gh`` lookup runs only in the lockless pre-pass
(:func:`capture_shipped_pr_probes`); the in-lock detect only reads its
probes. A missing, stale or failed probe defers the session a tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from cw.auto_dev_result import AutoDevResult
from cw.config import load_state, save_state
from cw.dev_queue import dev_queue_lock, load_dev_queue
from cw.exceptions import CwError
from cw.gh import _GH_PR_STATE_MERGED, _GH_PR_STATE_OPEN
from cw.models import (
    DEFAULT_LANE,
    DEFAULT_STAGE,
    CompletionReason,
    LastResultSource,
    ReapReason,
    SessionStatus,
    Stage,
)
from cw.reconcile import _deps
from cw.reconcile._shared import (
    _agent_spawn_stamp_session_id,
    _unresolved_subagent_spawn_age_seconds,
    feature_branch_key,
)
from cw.reconcile.deferred import PostLockJob
from cw.reconcile.dispositions import emit_routed_sentinel_completion
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError
from cw.reconcile.routed_result_sessions import (
    StrandedRoutedSession,
    find_stranded_routed_sessions,
    session_pins_occupied_row,
)
from cw.reconcile.tasks import _client_cwd
from cw.result import reconstruct_staged_sentinel

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from cw.gh import BranchPr
    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorConfig,
        Session,
        TicketTask,
    )
    from cw.reconcile.deferred import DeferredReconcileJobs

_log = logging.getLogger(__name__)

# Most ``gh pr list`` lookups one capture pass runs. A failed lookup counts.
SHIPPED_PR_MAX_PER_TICK = 3
# Wall-clock budget for one capture pass, checked before each lookup.
SHIPPED_PR_BUDGET_SECONDS: float = 20.0
# How old a captured probe may be when the in-lock detect consumes it. Strict
# ``<``; a stale probe only defers its session a tick (ADR-0014).
SHIPPED_PR_PROBE_MAX_AGE_SECONDS: float = 120.0
_SECONDS_PER_MINUTE = 60


@dataclass(frozen=True)
class ShippedPrEvidence:
    """The sentinel's own PR as ``gh`` reported it: OPEN or MERGED."""

    number: int
    url: str
    state: str
    merged_at: str | None


@dataclass(frozen=True)
class ShippedReleaseCandidate:
    """One stranded shipped session the evidence says to close."""

    session_id: str
    ticket_id: str
    client: str
    evidence: ShippedPrEvidence
    # Carry the detector's queue metadata into the durable close audit.
    lane: str = DEFAULT_LANE
    stage: Stage = DEFAULT_STAGE


class ShippedPrProbeUnavailableError(Exception):
    """No usable PR probe for a session: capped, missing or stale.

    A plain ``Exception``, not a ``CwError``, so no broad ``except CwError``
    swallows it; the capture and the detect catch it by name.
    """


# (client, ticket_id, session_id): a re-claim between capture and the lock is
# a different key, so a probe never answers for another session.
type _ProbeKey = tuple[str, str, str]
# The branch's PRs, or None when gh was unavailable (no usable evidence).
type _ProbePayload = tuple[BranchPr, ...] | None


def _probe_key(hit: StrandedRoutedSession) -> _ProbeKey:
    return hit.session.client, hit.ticket_id, hit.session.id


class ShippedPrProbes:
    """Branch PR lists captured lockless, keyed by (client, ticket, session).

    :meth:`capture` runs the live ``gh`` read (lockless only); :meth:`lookup`
    never runs a subprocess. A thin adapter over
    :class:`~cw.reconcile.probe_store.BoundedProbeStore`, like
    ``gate_plan_probes.PlanProbes``.
    """

    def __init__(
        self,
        *,
        budget_seconds: float | None = None,
        max_captures: int | None = None,
    ) -> None:
        self._store: BoundedProbeStore[_ProbeKey, _ProbePayload] = BoundedProbeStore(
            budget_seconds=budget_seconds,
            max_captures=max_captures,
            max_age_seconds=SHIPPED_PR_PROBE_MAX_AGE_SECONDS,
        )

    @property
    def captures(self) -> int:
        """How many live lookups this store has attempted, failed ones included."""
        return self._store.captures

    @property
    def captured_keys(self) -> frozenset[_ProbeKey]:
        return self._store.keys

    def capture(
        self, key: _ProbeKey, *, read: Callable[[], _ProbePayload]
    ) -> _ProbePayload:
        """Run *read* and keep its result. Lockless only.

        Raises :class:`ShippedPrProbeUnavailableError` before reading once the
        budget is spent or the per-tick cap is reached.
        """
        try:
            return self._store.capture(key, read=lambda _captured_at: read())
        except ProbeStoreUnavailableError as err:
            msg = f"shipped-release PR probe not captured for {key}: {err.reason}"
            raise ShippedPrProbeUnavailableError(msg) from err

    def lookup(self, key: _ProbeKey) -> _ProbePayload:
        """Return the captured payload if still fresh. Never runs a subprocess."""
        try:
            return self._store.lookup(key)
        except ProbeStoreUnavailableError as err:
            msg = f"shipped-release PR probe unusable for {key}: {err.reason}"
            raise ShippedPrProbeUnavailableError(msg) from err


def _shipped_pr_url(session: Session) -> str | None:
    """The sentinel's own PR url when the result is an emitted ``shipped`` one.

    Requires ``last_result_source`` EMIT_CLI and a ``last_result`` that
    rebuilds to a ``shipped`` :class:`AutoDevResult` (which always carries a
    PR). Headless-ness is irrelevant here.
    """
    if session.last_result_source is not LastResultSource.EMIT_CLI:
        return None
    result = reconstruct_staged_sentinel(session.last_result)
    if not isinstance(result, AutoDevResult) or result.status != "shipped":
        return None
    return result.pr.url if result.pr is not None else None


def _drain_quiesced(
    session: Session, *, now: datetime, config: OrchestratorConfig
) -> bool:
    """True only when the drain stamp is readable, this session's, and old.

    The fail-closed twin of ``idle._detect._background_work_still_draining``
    (which reads "clear" on an absent or unreadable stamp and stays so for its
    other consumers). This sweep stops a roster-live worker, so it needs the
    opposite default: an outstanding spawn count with a parseable
    ``last_stamped_at`` at least ``fix_loop_await_deadline_minutes`` old, AND
    a stamp ``session_id`` equal to *session*'s id (a re-used worktree shows
    the newest session's stamp). Anything else holds the act this tick.
    """
    age = _unresolved_subagent_spawn_age_seconds(session.worktree_path, now)
    deadline = config.fix_loop_await_deadline_minutes * _SECONDS_PER_MINUTE
    return (
        age is not None
        and age >= deadline
        and _agent_spawn_stamp_session_id(session.worktree_path) == session.id
    )


def _candidates(
    state: CwState,
    tasks: Iterable[TicketTask],
    *,
    now: datetime,
    native_live: set[str],
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
) -> list[tuple[StrandedRoutedSession, str]]:
    """Stranded sessions with an emitted shipped result and a quiesced drain.

    Returns ``(hit, sentinel_pr_url)`` pairs. The one predicate the lockless
    capture and the in-lock detect share, so they cannot drift.
    """
    hits = find_stranded_routed_sessions(
        state,
        tasks,
        now=now,
        native_live=native_live,
        config=config,
        enabled_clients=clients.keys(),
    )
    found: list[tuple[StrandedRoutedSession, str]] = []
    for hit in hits:
        pr_url = _shipped_pr_url(hit.session)
        if pr_url is None or not _drain_quiesced(hit.session, now=now, config=config):
            continue
        found.append((hit, pr_url))
    return found


def select_own_pr(
    prs: Iterable[BranchPr], sentinel_pr_url: str
) -> ShippedPrEvidence | None:
    """The sentinel's own PR among the branch's PRs, when OPEN or MERGED.

    Matched by URL, so a different PR on the same branch never stands in for
    it, and a CLOSED-unmerged own PR is no evidence.
    """
    for pr in prs:
        if pr.url == sentinel_pr_url and pr.state in {
            _GH_PR_STATE_OPEN,
            _GH_PR_STATE_MERGED,
        }:
            return ShippedPrEvidence(
                number=pr.number, url=pr.url, state=pr.state, merged_at=pr.merged_at
            )
    return None


def _read_branch_prs(branch: str, cwd: Path | None) -> _ProbePayload:
    prs, _available = _deps.fetch_branch_prs(branch, cwd=cwd)
    return tuple(prs) if prs is not None else None


def capture_shipped_pr_probes(
    *, config: OrchestratorConfig, clients: dict[str, ClientConfig]
) -> ShippedPrProbes:
    """Lockless pre-pass: fetch each candidate's branch PRs before the lock.

    Re-runs the candidate predicate over a fresh snapshot, treating every
    recorded surface as roster-live (no roster call here; the in-lock detect
    re-applies the real roster, so this only over-captures within the cap).
    Bounded by ``SHIPPED_PR_MAX_PER_TICK`` lookups and
    ``SHIPPED_PR_BUDGET_SECONDS``. Must never fail ``reconcile()``: a state or
    dev-queue read error is logged and yields empty probes.
    """
    probes = ShippedPrProbes(
        budget_seconds=SHIPPED_PR_BUDGET_SECONDS,
        max_captures=SHIPPED_PR_MAX_PER_TICK,
    )
    if not clients:
        return probes
    try:
        state = load_state()
        tasks = load_dev_queue().tasks
    except (OSError, ValueError):
        _log.warning(
            "reconcile: shipped-release PR pre-pass could not read state;"
            " deferring every shipped-release candidate this tick",
            exc_info=True,
        )
        return probes
    surfaces = {s.surface_ref for s in state.sessions if s.surface_ref is not None}
    candidates = _candidates(
        state,
        tasks,
        now=datetime.now(UTC),
        native_live=surfaces,
        clients=clients,
        config=config,
    )
    for hit, _pr_url in candidates:
        client = hit.session.client
        branch = feature_branch_key(client, hit.ticket_id, clients)
        cwd = _client_cwd(client, clients)
        try:
            probes.capture(_probe_key(hit), read=partial(_read_branch_prs, branch, cwd))
        except ShippedPrProbeUnavailableError:
            _log.info(
                "shipped_release: PR lookup cap or budget reached; %s/%s waits a tick",
                client,
                hit.ticket_id,
            )
            break
    return probes


def detect_shipped_release(
    state: CwState,
    tasks: Iterable[TicketTask],
    *,
    native_live: set[str],
    clients: dict[str, ClientConfig],
    probes: ShippedPrProbes,
    now: datetime,
    config: OrchestratorConfig,
) -> list[ShippedReleaseCandidate]:
    """Return every stranded shipped session whose own PR is OPEN or MERGED.

    Pure: no writes, no events, no subprocess. A session with no usable probe,
    a probe recording a gh outage, or no own OPEN/MERGED PR is skipped.
    """
    found: list[ShippedReleaseCandidate] = []
    for hit, pr_url in _candidates(
        state,
        tasks,
        now=now,
        native_live=native_live,
        clients=clients,
        config=config,
    ):
        try:
            prs = probes.lookup(_probe_key(hit))
        except ShippedPrProbeUnavailableError as err:
            _log.debug("shipped_release: %s; waiting a tick", err)
            continue
        evidence = select_own_pr(prs, pr_url) if prs is not None else None
        if evidence is None:
            continue
        found.append(
            ShippedReleaseCandidate(
                session_id=hit.session.id,
                ticket_id=hit.ticket_id,
                client=hit.session.client,
                evidence=evidence,
                lane=hit.lane,
                stage=hit.stage,
            )
        )
    return found


def _close_unpinned(
    state: CwState, candidates: list[ShippedReleaseCandidate], *, now: datetime
) -> list[tuple[Session, ShippedReleaseCandidate]]:
    """Flip every candidate no fresh queue row pins; persist once.

    Holds ``dev_queue_lock`` across the fresh read, the pin check and the
    ``save_state`` (legal under reconcile's ``sessions_lock``: ADR-0019 rank 0
    then 1), so no row can bind to the session in between. A queue read error
    skips the whole act this tick.
    """
    sessions_by_id = {s.id: s for s in state.sessions}
    closing: list[tuple[Session, ShippedReleaseCandidate]] = []
    with dev_queue_lock():
        try:
            fresh_tasks = load_dev_queue().tasks
        except (OSError, ValueError):
            _log.warning(
                "shipped_release: fresh dev-queue read failed; closing nothing"
                " this tick",
                exc_info=True,
            )
            return []
        for candidate in candidates:
            if session_pins_occupied_row(fresh_tasks, candidate.session_id):
                _log.info(
                    "shipped_release: session %s for %s/%s is now pinned by a"
                    " lane-occupying row; left untouched",
                    candidate.session_id,
                    candidate.client,
                    candidate.ticket_id,
                )
                continue
            closing.append((sessions_by_id[candidate.session_id], candidate))
        if closing:
            from cw.doctor.routed_result_wedge import (
                _audit_outbox_lock,
                _queue_audit_intents,
                _read_audit_outbox,
                _write_audit_outbox,
            )

            original_fields = {
                session.id: (
                    session.status,
                    session.completed_at,
                    session.completed_reason,
                    session.reap_reason,
                )
                for session, _candidate in closing
            }
            audit_hits = [
                StrandedRoutedSession(
                    session=session,
                    ticket_id=candidate.ticket_id,
                    lane=candidate.lane,
                    stage=candidate.stage,
                    row_status=None,
                    stale_minutes=0.0,
                    surface_ref=session.surface_ref or "",
                )
                for session, candidate in closing
            ]
            previous_audits: list[dict[str, Any]] | None = None
            try:
                # Keep the intent write and state commit in one durable
                # transaction. If either fails, restore both the in-memory
                # session fields and the exact prior outbox contents.
                with _audit_outbox_lock():
                    previous_audits = _read_audit_outbox()
                    _queue_audit_intents(audit_hits)
                    for session, _candidate in closing:
                        session.status = SessionStatus.COMPLETED
                        session.completed_at = now
                        session.completed_reason = CompletionReason.NORMAL
                        session.reap_reason = ReapReason.ROUTED_RESULT_STRANDED
                    save_state(state)
            except (CwError, KeyError, OSError, TypeError, ValueError):
                for session, _candidate in closing:
                    (
                        session.status,
                        session.completed_at,
                        session.completed_reason,
                        session.reap_reason,
                    ) = original_fields[session.id]
                if previous_audits is not None:
                    try:
                        with _audit_outbox_lock():
                            _write_audit_outbox(previous_audits)
                    except (CwError, KeyError, OSError, TypeError, ValueError):
                        _log.exception(
                            "shipped_release: failed to restore the audit outbox"
                        )
                _log.warning(
                    "shipped_release: durable close audit or state commit failed;"
                    " closing nothing this tick",
                    exc_info=True,
                )
                return []
    return closing


def _emit_release(
    session: Session,
    candidate: ShippedReleaseCandidate,
    *,
    deferred: DeferredReconcileJobs,
) -> None:
    """Emit ``session.completed`` (with ``pr_url``), queue the stop, log it.

    The durable close audit remains pending until the deferred stop reports its
    outcome. A failed completion-event write leaves that intent for the
    existing retry path, and the leaked-worker sweep is the backstop.
    """
    evidence = candidate.evidence
    try:
        emit_routed_sentinel_completion(
            session,
            ticket_id=candidate.ticket_id,
            status="shipped",
            deferred=deferred,
            pr_url=evidence.url,
        )
    except (CwError, OSError):
        _log.exception(
            "shipped_release: closed session %s for %s/%s, but its"
            " session.completed event was not recorded",
            session.id,
            candidate.client,
            candidate.ticket_id,
        )
        return
    if session.surface_ref is None:
        _finish_release_audit(session.id, stop_succeeded=True, stop_error=None)
    else:
        _attach_release_audit_to_stop(session.id, session.surface_ref, deferred)
    _log.info(
        "shipped_release: closed stranded session %s for %s/%s: own PR %s is %s"
        " (merged_at=%s); reap_reason=%s",
        session.id,
        candidate.client,
        candidate.ticket_id,
        evidence.url,
        evidence.state,
        evidence.merged_at,
        ReapReason.ROUTED_RESULT_STRANDED.value,
    )


def _finish_release_audit(
    session_id: str, *, stop_succeeded: bool, stop_error: str | None
) -> None:
    """Finalize and deliver the close audit after the stop outcome is known."""
    try:
        from cw.doctor.routed_result_wedge import (
            _emit_audit_record,
            _finalize_audit_intent,
        )

        mutations = ["session_status_completed"]
        if stop_succeeded:
            mutations.append("daemon_stopped")
        audit = _finalize_audit_intent(
            session_id,
            mutations=mutations,
            stop_succeeded=stop_succeeded,
            stop_error=stop_error,
        )
    except (CwError, OSError, ValueError):
        _log.warning(
            "shipped_release: durable close audit could not be finalized for"
            " session %s",
            session_id,
            exc_info=True,
        )
        return
    if audit is None:
        _log.error(
            "shipped_release: durable close audit missing for session %s", session_id
        )
    elif not _emit_audit_record(audit):
        _log.warning(
            "shipped_release: close audit remains pending for session %s", session_id
        )


def _attach_release_audit_to_stop(
    session_id: str, surface_ref: str, deferred: DeferredReconcileJobs
) -> None:
    """Finalize the audit from the post-lock stop job's eventual result."""
    label = f"surface_stop:{surface_ref}"
    for index in range(len(deferred.post_lock) - 1, -1, -1):
        job = deferred.post_lock[index]
        if job.label != label:
            continue

        def _run(job: PostLockJob = job) -> object:
            try:
                result = job.run()
            except Exception as error:  # noqa: BLE001 - any stop failure must still finalize the close audit and fail closed
                _finish_release_audit(
                    session_id,
                    stop_succeeded=False,
                    stop_error=str(error) or "daemon stop failed",
                )
                return None
            _finish_release_audit(session_id, stop_succeeded=True, stop_error=None)
            return result

        deferred.post_lock[index] = PostLockJob(label=job.label, run=_run)
        return
    _log.warning(
        "shipped_release: stop job missing for session %s; close audit remains pending",
        session_id,
    )


def run_shipped_release(
    state: CwState,
    *,
    now: datetime,
    native_live: set[str],
    clients: dict[str, ClientConfig],
    config: OrchestratorConfig,
    tasks: Iterable[TicketTask],
    probes: ShippedPrProbes | None,
    deferred: DeferredReconcileJobs,
) -> list[str]:
    """Close every stranded shipped session the evidence supports.

    Runs inside ``_reconcile_locked``'s ``sessions_lock`` hold, on the
    caller's in-memory *state*. *tasks* only selects candidates; the pin
    decision is made on a fresh in-lock queue read. Returns the closed session
    ids. A no-op when *probes* is None or *clients* is empty.
    """
    if probes is None or not clients:
        return []
    candidates = detect_shipped_release(
        state,
        tasks,
        native_live=native_live,
        clients=clients,
        probes=probes,
        now=now,
        config=config,
    )
    if not candidates:
        return []
    closed = _close_unpinned(state, candidates, now=now)
    for session, candidate in closed:
        _emit_release(session, candidate, deferred=deferred)
    return [session.id for session, _candidate in closed]
