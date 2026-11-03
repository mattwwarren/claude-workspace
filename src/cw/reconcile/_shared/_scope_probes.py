"""Lockless capture of the salvaged-sentinel scope check's git reads (#2641).

A salvaged sentinel's self-reported ``scope.files`` / ``scope.lines_actual``
is corrected against the branch's real diff (#1487). That measurement runs
``git merge-base`` and ``git diff --numstat``, which must not run under
``sessions_lock``. ``reconcile()`` therefore captures it here before the lock
(``cw.reconcile.lockless_prepass.capture_salvage_scope_probes``), and the
in-lock sentinel parse only looks it up and applies it
(:func:`apply_prefetched_scope`). Capture and lookup share
:func:`_scope_target`, so they cannot disagree about which results are
measured.

The correction is cosmetic (``dispatch.routing`` reads only ``scope.tier``),
so every uncertainty fails open: a missing or stale capture, or a sentinel
that changed since it was captured, leaves the self-report uncorrected with
one WARNING and never costs the caller its sentinel.

A leaf of ``cw.reconcile._shared``: it imports ``_constants`` and ``_roster``,
never ``_sentinels`` (which imports this module).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from cw.models import SessionOrigin
from cw.reconcile._shared._constants import _LIVE_STATUSES, _LOGGER_NAME
from cw.reconcile._shared._roster import ticket_id_for_session
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError
from cw.worktree import (
    apply_measured_scope,
    compute_branch_diff_scope,
    resolve_scope_guard_default_branch,
    scope_stage_measurable,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.auto_dev_result import AutoDevResult
    from cw.models import CwState, Session
    from cw.worktree import _BranchDiffScope

_log = logging.getLogger(_LOGGER_NAME)

# Wall-clock budget for one capture pass, checked before each git measurement.
SCOPE_PROBE_BUDGET_SECONDS: float = 20.0
# A capture is usable for this long. It is taken before the harvest pass (at
# most 100 s), so a tighter bound could age it out during a slow harvest; a
# long lock wait only leaves the scope uncorrected.
SCOPE_PROBE_MAX_AGE_SECONDS: float = 180.0

_CHANGED_REASON = "changed"


class ScopeKey(NamedTuple):
    """The session and worktree a measurement belongs to."""

    session_id: str
    worktree_path: Path


@dataclass(frozen=True)
class CapturedScope:
    """One lockless measurement and the uncorrected sentinel it was taken for.

    ``measured`` is ``None`` when git could not measure the branch; applying
    such a capture warns ``scope_verification_unavailable`` and leaves the
    sentinel as reported.
    """

    result: AutoDevResult
    default_branch: str
    measured: _BranchDiffScope | None


type ScopeProbes = BoundedProbeStore[ScopeKey, CapturedScope]


def new_scope_probes(
    *, budget_seconds: float | None = SCOPE_PROBE_BUDGET_SECONDS
) -> ScopeProbes:
    """An empty store; ``budget_seconds=None`` means unbounded.

    No per-tick cap: a stable set of live sessions would otherwise starve
    every later session forever.
    """
    return BoundedProbeStore(
        budget_seconds=budget_seconds,
        max_captures=None,
        max_age_seconds=SCOPE_PROBE_MAX_AGE_SECONDS,
    )


def scope_probe_targets(state: CwState) -> list[Session]:
    """Sessions whose transcript an in-lock salvage could parse this tick.

    Live, DAEMON-origin sessions with a worktree: a superset of every in-lock
    reach point (phantom salvage and stage advance, the idle sweep's
    ROUTE_EMITTED_SENTINEL, the concierge's confirmed-dead close).
    """
    return [
        session
        for session in state.sessions
        if session.status in _LIVE_STATUSES
        and session.origin is SessionOrigin.DAEMON
        and session.worktree_path is not None
    ]


def _scope_target(session: Session, result: AutoDevResult) -> Path | None:
    """The worktree to measure *result* against; None when nothing is measurable."""
    if session.worktree_path is None or not scope_stage_measurable(result):
        return None
    return session.worktree_path


def _read_scope(
    session: Session,
    result: AutoDevResult,
    worktree_path: Path,
    _captured_at: datetime,
) -> CapturedScope:
    default_branch = resolve_scope_guard_default_branch(
        session.client, log_context=f"session={session.id}"
    )
    return CapturedScope(
        result=result,
        default_branch=default_branch,
        measured=compute_branch_diff_scope(worktree_path, default_branch),
    )


def capture_scope_probe(
    probes: ScopeProbes, session: Session, result: AutoDevResult
) -> bool:
    """Measure *session*'s branch for *result* into *probes*, lock-free.

    Returns False, running no git, when *result* has nothing to measure (a
    pre-impl stage, or no worktree). The client's ``default_branch`` is
    resolved inside the read, falling back to ``main`` on any config failure;
    a git failure stores ``measured=None``. A budget refusal escapes as
    :class:`~cw.reconcile.probe_store.ProbeStoreUnavailableError` and stores
    nothing.
    """
    worktree_path = _scope_target(session, result)
    if worktree_path is None:
        return False
    probes.capture(
        ScopeKey(session.id, worktree_path),
        read=partial(_read_scope, session, result, worktree_path),
    )
    return True


def _warn_unavailable(session: Session, reason: str) -> None:
    _log.warning(
        "salvaged_scope_probe_unavailable session=%s ticket=%s client=%s"
        " reason=%s; sentinel scope left uncorrected",
        session.id,
        ticket_id_for_session(session.name),
        session.client,
        reason,
    )


def apply_prefetched_scope(
    result: AutoDevResult, session: Session, probes: ScopeProbes | None
) -> AutoDevResult:
    """The in-lock half: correct *result*'s scope from a capture, never git.

    ``probes=None`` means "do not verify": *result* is returned silently. A
    result with nothing to measure is returned without consulting the store.
    Otherwise a missing or stale capture, or one taken for a different
    sentinel than *result* (the worker emitted again after capture), logs one
    ``salvaged_scope_probe_unavailable`` WARNING and returns *result*
    unchanged.
    """
    if probes is None:
        return result
    worktree_path = _scope_target(session, result)
    if worktree_path is None:
        return result
    try:
        captured = probes.lookup(ScopeKey(session.id, worktree_path))
    except ProbeStoreUnavailableError as err:
        _warn_unavailable(session, err.reason)
        return result
    if captured.result != result:
        _warn_unavailable(session, _CHANGED_REASON)
        return result
    return apply_measured_scope(
        result,
        captured.measured,
        worktree_path=worktree_path,
        default_branch=captured.default_branch,
    )
