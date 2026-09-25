"""The spawn-attempt outcome record.

:class:`_SpawnOutcome` is returned by both the codex capability gate
(:mod:`cw.dispatch.claim.codex_capability`) and the spawn path
(:mod:`cw.dispatch.claim.spawn`). It lives in its own leaf module so the spawn
path can import the gate at module top without an import cycle. Extracted
verbatim from the historical flat ``cw.dispatch.claim`` module by the package
split (#2378).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class _SpawnOutcome:
    """Result of attempting to spawn one claimed task.

    ``spawned`` — True if a session was started (counters should be bumped).
    ``usage_limit_detected`` — True if a :class:`UsageLimitError` fired.
    ``spawn_error`` — True if a broad spawn failure reverted the task.
    ``occupied`` — True if the reused worktree was occupied by a live cw session
    or daemon worker (``WorktreeOccupiedError``, #2213) and the claim was
    released without a spawn -- or, since #2077, the hook-context write found
    it held by a genuinely live session (a ``HookContextConflictError`` with
    ``genuinely_live`` set, see :func:`_defer_genuinely_live_hook_conflict`),
    which reuses this same field. Deliberately decoupled from ``spawn_error``: an
    occupied worktree is a transient, per-ticket condition, not the sporadic
    backend failure the circuit breaker exists to catch, so it neither
    increments the lane's spawn-error count nor aborts the rest of the tick's
    lane/client loop.
    ``error`` — the exception string from a broad spawn failure, the codex
    capability diagnosis string when ``capability_parked`` is True, or the
    occupancy reason when ``occupied`` is True (``""`` when none of these),
    carried so the caller can stamp ``last_error`` on the per-lane
    circuit-breaker LANE_PAUSED payload (#875).
    ``capability_parked`` — True if the codex capability gate (#1238) parked
    the task BLOCKED_ON_USER before any spawn was attempted. Below
    :data:`_CODEX_CAPABILITY_PARK_CIRCUIT_THRESHOLD` consecutive parks this is
    deliberately decoupled from ``spawn_error`` (no circuit-breaker increment,
    no aborting the rest of the tick's lane/client loop) since an isolated
    park is a deterministic, per-task condition, not the sporadic failure the
    circuit breaker exists to catch. At/above the threshold, ``spawn_error``
    IS also set (see :func:`_codex_capability_gate`) as a bounded backstop
    against a systemically wrong probe verdict — ``capability_parked`` and
    ``spawn_error`` can both be True at once in that case.

    ``usage_limit_detected`` and ``spawn_error`` signal the caller to break
    out of the slot/lane loops; ``capability_parked`` alone does not (but see
    above — it can co-occur with ``spawn_error`` once the park-count
    threshold is reached).
    ``usage_limit_reset_at`` — the instant the limit lifts, when the spawn-time
    message named one that parsed (#1409); None when it did not, which the
    dispatch loop reads as "use the flat ``usage_limit_backoff_seconds``". Only
    ever set alongside ``usage_limit_detected``. Declared last so the existing
    positional-free call sites are unaffected.
    """

    spawned: bool = False
    usage_limit_detected: bool = False
    spawn_error: bool = False
    error: str = ""
    capability_parked: bool = False
    occupied: bool = False
    usage_limit_reset_at: datetime | None = None
