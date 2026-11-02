"""Main-checkout drift detection for reconcile (#925 / #940 / #1258).

Detects the isolation breach where a dispatch worker escaped its own worktree
and mutated the operator's main checkout: the session's ``worktree_path`` points
elsewhere, yet the main checkout is dirty or has commits origin does not (ahead /
diverged). Emits an advisory ``SESSION_NEEDS_ATTENTION`` so the operator inspects
before the stray state freezes dispatch via the freshness gate.

This is a *per-client* check, edge-triggered via a persisted latch
(``cw.dispatch_state.load_main_drift_latches`` / ``save_main_drift_latches``): the
attention event fires once when drift starts and stays silent on every
subsequent tick while the drift holds, resetting silently the moment the
client's main checkout goes clean again. This mirrors dispatch.py's
fleet-wide availability-outage latch (``_record_availability_block`` /
``_reset_availability_block``) rather than the per-tick consecutive-freshness
-gate-block counter (``ClientConcurrencyOverride.consecutive_freshness_blocks``,
RFC 0007 §W2), which debounces N>=2 observations instead of firing on the
first. The ``"detached"`` outcome of ``check_main_ff_safety`` is the known
adjacent mislabel bug (#940 R7) and is deliberately ignored.

The git runs in a lockless pre-pass (#2546): ``reconcile()`` calls
:func:`capture_main_drift_probes` before ``sessions_lock``, and the in-lock
detect only reads the stored classification. A git error is stored as no
drift, exactly as before; a client with no usable capture (missing, stale or
budget-exhausted) yields no candidate, so nothing fires and no latch changes
until the next tick re-captures it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from cw.dispatch_state import load_main_drift_latches, save_main_drift_latches
from cw.events import record_event
from cw.exceptions import WorktreeError
from cw.models import OrchestratorEventType, SessionOrigin
from cw.reconcile._shared import _LIVE_STATUSES, _MAIN_CHECKOUT_DRIFT_REASON
from cw.reconcile.probe_store import BoundedProbeStore, ProbeStoreUnavailableError
from cw.worktree import check_main_ff_safety, is_main_checkout_dirty

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cw.models import ClientConfig, CwState

_log = logging.getLogger(__name__)

# Human-readable drift kinds surfaced in the event breadcrumbs.
_DRIFT_DIRTY = "dirty"
_DRIFT_AHEAD = "ahead of origin"
_DRIFT_DIVERGED = "diverged from origin"

# Wall-clock budget for one capture pass, checked before each client's git.
MAIN_DRIFT_PROBE_BUDGET_SECONDS: float = 20.0
# A capture is usable for this long (see ``reconcile()``'s pre-pass notes).
MAIN_DRIFT_PROBE_MAX_AGE_SECONDS: float = 180.0

# Keyed by client name; the payload is the drift kind, or None for clean.
type MainDriftProbes = BoundedProbeStore[str, str | None]


def new_main_drift_probes(
    *, budget_seconds: float | None = MAIN_DRIFT_PROBE_BUDGET_SECONDS
) -> MainDriftProbes:
    """An empty store; ``budget_seconds=None`` means unbounded."""
    return BoundedProbeStore(
        budget_seconds=budget_seconds,
        max_captures=None,
        max_age_seconds=MAIN_DRIFT_PROBE_MAX_AGE_SECONDS,
    )


@dataclass(frozen=True)
class _ClientDriftStatus:
    """A client's main-checkout drift classification for this tick (#1258).

    One entry per checked client (dirty or clean) rather than one per live
    session — drift is a property of the client's main checkout, not any
    individual session.
    """

    client: str
    drift_kind: str | None  # None == checked and clean this tick
    workspace_path: Path
    sample_worktree_path: Path  # first live session's worktree_path (breadcrumb)


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


def _drift_targets(
    state: CwState, clients: dict[str, ClientConfig]
) -> list[tuple[str, ClientConfig, Path]]:
    """(client name, config, first live session's worktree) for each client.

    Only live (ACTIVE/IDLE) DAEMON-origin sessions with a resolved
    ``worktree_path`` count; a session whose client is absent from *clients*
    is skipped (cannot probe). Shared by capture and detect so they agree.
    """
    targets: list[tuple[str, ClientConfig, Path]] = []
    seen_clients: set[str] = set()
    for session in state.sessions:
        if session.status not in _LIVE_STATUSES:
            continue
        if session.origin is not SessionOrigin.DAEMON:
            continue
        if session.worktree_path is None:
            continue
        if session.client in seen_clients:
            continue
        client = clients.get(session.client)
        if client is None:
            continue
        seen_clients.add(session.client)
        targets.append((session.client, client, session.worktree_path))
    return targets


def _read_drift(client: ClientConfig, _captured_at: datetime) -> str | None:
    return _classify_main_drift(client)


def capture_main_drift_probes(
    state: CwState, clients: dict[str, ClientConfig], *, probes: MainDriftProbes
) -> None:
    """Lockless pre-pass: classify each target client's main checkout into
    *probes*. Stops at the first budget refusal with one warning; any
    exception other than the git errors ``_classify_main_drift`` absorbs
    propagates."""
    targets = _drift_targets(state, clients)
    for index, (name, client, _worktree) in enumerate(targets):
        try:
            probes.capture(name, read=partial(_read_drift, client))
        except ProbeStoreUnavailableError as err:
            _log.warning(
                "reconcile: main-drift probe capture stopped (%s); %d captured,"
                " %d of %d client(s) deferred to the next tick",
                err.reason,
                index,
                len(targets) - index,
                len(targets),
            )
            return


def _detect_main_drift_candidates(
    state: CwState,
    clients: dict[str, ClientConfig],
    probes: MainDriftProbes | None,
) -> list[_ClientDriftStatus]:
    """Pure classification phase for main-checkout drift. Makes zero writes.

    Reads each target client's drift from *probes* (captured before the lock,
    #2546) and never runs git. A client with no usable capture (``None``
    behaves like an empty store) is skipped with a warning: no candidate, so
    no event and no latch change this tick.

    Returns one :class:`_ClientDriftStatus` per checked client — dirty or
    clean — so the act phase can both fire a fresh drift and reset a stale
    latch when the checkout has gone clean again.
    """
    store = probes if probes is not None else new_main_drift_probes()
    candidates: list[_ClientDriftStatus] = []
    for name, client, worktree in _drift_targets(state, clients):
        try:
            drift_kind = store.lookup(name)
        except ProbeStoreUnavailableError as err:
            _log.warning(
                "main_drift_probe_unavailable client=%s reason=%s;"
                " deferring to the next tick",
                name,
                err.reason,
            )
            continue
        candidates.append(
            _ClientDriftStatus(
                client=name,
                drift_kind=drift_kind,
                workspace_path=client.workspace_path,
                sample_worktree_path=worktree,
            )
        )
    return candidates


def _act_on_main_drift_candidates(candidates: list[_ClientDriftStatus]) -> None:
    """Emit SESSION_NEEDS_ATTENTION once per drift episode, per client (#1258).

    Edge-triggered latch keyed on client name: a client with drift already
    latched is not re-fired; a client that has gone clean has its latch reset
    silently (no "cleared" event). Persists the latch map only when at least
    one entry changed this tick (single load + single save, mirroring the
    sidecar's read-merge-write discipline).
    """
    latches = load_main_drift_latches()
    changed = False
    for status in candidates:
        was_latched = latches.get(status.client, False)
        if status.drift_kind is not None:
            if was_latched:
                continue
            breadcrumbs = (
                f"main checkout {status.workspace_path} is {status.drift_kind}"
                f" (worktree at {status.sample_worktree_path})"
            )
            record_event(
                OrchestratorEventType.SESSION_NEEDS_ATTENTION,
                {
                    "session_id": "",
                    "session_name": "",
                    "client": status.client,
                    "ticket_id": None,
                    "claude_session_id": None,
                    "paused_status": _MAIN_CHECKOUT_DRIFT_REASON,
                    "breadcrumbs": breadcrumbs,
                    "crashed": False,
                },
                correlation_id=None,
            )
            latches[status.client] = True
            changed = True
        elif was_latched:
            latches[status.client] = False
            changed = True
    if changed:
        save_main_drift_latches(latches)
