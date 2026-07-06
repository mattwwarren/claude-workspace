"""Main-checkout drift detection for reconcile (#925 / #940).

Detects the isolation breach where a dispatch worker escaped its own worktree
and mutated the operator's main checkout: the session's ``worktree_path`` points
elsewhere, yet the main checkout is dirty or has commits origin does not (ahead /
diverged). Emits an advisory ``SESSION_NEEDS_ATTENTION`` so the operator inspects
before the stray state freezes dispatch via the freshness gate.

This is a *per-session state check* that re-fires every tick while the drift
holds — it is NOT the per-tick consecutive-freshness-gate-block counter (see
``ClientConcurrencyOverride.consecutive_freshness_blocks``, RFC 0007 §W2) —
that counter persists an edge-triggered latch; this check re-fires every tick
by design. The ``"detached"`` outcome of ``check_main_ff_safety`` is the known
adjacent mislabel bug (#940 R7) and is deliberately ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.events import record_event
from cw.exceptions import WorktreeError
from cw.models import OrchestratorEventType, SessionOrigin
from cw.reconcile._shared import (
    _LIVE_STATUSES,
    _MAIN_CHECKOUT_DRIFT_REASON,
    ticket_id_for_session,
)
from cw.worktree import check_main_ff_safety, is_main_checkout_dirty

if TYPE_CHECKING:
    from pathlib import Path

    from cw.models import ClientConfig, CwState, Session

# Human-readable drift kinds surfaced in the event breadcrumbs.
_DRIFT_DIRTY = "dirty"
_DRIFT_AHEAD = "ahead of origin"
_DRIFT_DIVERGED = "diverged from origin"


@dataclass(frozen=True)
class _MainDriftCandidate:
    """A live worktree worker whose main checkout has drifted (#940)."""

    session: Session
    drift_kind: str
    workspace_path: Path


def _classify_main_drift(client: ClientConfig) -> str | None:
    """Return the main checkout's drift kind for *client*, or None if clean.

    Never raises: any git error is treated as no-drift (mirrors
    ``is_main_checkout_dirty``'s fail-safe posture) so a transient git failure
    never emits a spurious attention event. ``"detached"`` is ignored (#940 R7).
    """
    try:
        if is_main_checkout_dirty(client):
            return _DRIFT_DIRTY
        safety = check_main_ff_safety(client)
    except (OSError, WorktreeError):
        return None
    if safety == "ahead":
        return _DRIFT_AHEAD
    if safety == "diverged":
        return _DRIFT_DIVERGED
    return None


def _detect_main_drift_candidates(
    state: CwState,
    clients: dict[str, ClientConfig],
) -> list[_MainDriftCandidate]:
    """Pure classification phase for main-checkout drift. Makes zero writes.

    Considers only live (ACTIVE/IDLE) DAEMON-origin sessions with a resolved
    ``worktree_path``; each is checked against its client's main checkout. A
    session whose client is absent from *clients* is skipped (cannot probe).

    Drift is a property of the *client's* main checkout, not the session, so
    ``_classify_main_drift`` is memoized per client name — multiple live
    sessions on the same client (e.g. impl + idea) probe git at most once per
    tick rather than once per session.
    """
    candidates: list[_MainDriftCandidate] = []
    drift_by_client: dict[str, str | None] = {}
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.worktree_path is None:
            continue
        client = clients.get(session.client)
        if client is None:
            continue
        if session.client not in drift_by_client:
            drift_by_client[session.client] = _classify_main_drift(client)
        drift_kind = drift_by_client[session.client]
        if drift_kind is None:
            continue
        candidates.append(
            _MainDriftCandidate(
                session=session,
                drift_kind=drift_kind,
                workspace_path=client.workspace_path,
            )
        )
    return candidates


def _act_on_main_drift_candidates(candidates: list[_MainDriftCandidate]) -> None:
    """Emit a SESSION_NEEDS_ATTENTION event per drift candidate (no state writes)."""
    for candidate in candidates:
        session = candidate.session
        ticket_id = ticket_id_for_session(session.name)
        breadcrumbs = (
            f"main checkout {candidate.workspace_path} is {candidate.drift_kind}"
            f" (worktree at {session.worktree_path})"
        )
        record_event(
            OrchestratorEventType.SESSION_NEEDS_ATTENTION,
            {
                "session_id": session.id,
                "session_name": session.name,
                "client": session.client,
                "ticket_id": ticket_id,
                "claude_session_id": session.claude_session_id,
                "paused_status": _MAIN_CHECKOUT_DRIFT_REASON,
                "breadcrumbs": breadcrumbs,
                "crashed": False,
            },
            correlation_id=ticket_id,
        )
