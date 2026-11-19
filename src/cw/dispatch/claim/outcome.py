"""The spawn-attempt outcome record and the shared pre-spawn park (#3744).

:class:`_SpawnOutcome` is returned by every pre-spawn and spawn step of one
claimed task (moved verbatim from ``codex_capability``, RFC 0019 H2b).
:func:`park_before_spawn` is the one pre-spawn park of a claimed RUNNING row:
it parks the row BLOCKED_ON_USER through the ``cw.queue_rows`` helper and,
only for an environmental park (``count_streak=True``), advances the
consecutive-park streak that engages the lane circuit breaker once parks look
systemic. A static-capability park never counts (R7): it is a deterministic
fact about the configuration, not a sign the probe verdict is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cw.queue_rows import _park_running_task_blocked_on_user

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import ClientConfig, TicketTask


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
    ``capability_parked`` — True if a pre-spawn park (the codex capability
    probe, #1238, through :func:`park_before_spawn`) parked
    the task BLOCKED_ON_USER before any spawn was attempted. Below
    :data:`_PARK_STREAK_CIRCUIT_THRESHOLD` consecutive parks this is
    deliberately decoupled from ``spawn_error`` (no circuit-breaker increment,
    no aborting the rest of the tick's lane/client loop) since an isolated
    park is a deterministic, per-task condition, not the sporadic failure the
    circuit breaker exists to catch. At/above the threshold, ``spawn_error``
    IS also set (see :func:`park_before_spawn`) as a bounded backstop
    against a systemically wrong probe verdict — ``capability_parked`` and
    ``spawn_error`` can both be True at once in that case.

    ``usage_limit_detected`` and ``spawn_error`` signal the caller to break
    out of the slot/lane loops; ``capability_parked`` alone does not (but see
    above — it can co-occur with ``spawn_error`` once the park-count
    threshold is reached).
    ``usage_limit_reset_at`` — the instant the limit lifts, when the spawn-time
    message named one that parsed (#1409); None when it did not, which the
    dispatch loop reads as "use the flat ``usage_limit_backoff_seconds``". Only
    ever set alongside ``usage_limit_detected``.
    ``worktree_failure`` (#2696, R3) -- set alongside ``spawn_error`` when the
    no-worker failure was a :class:`~cw.exceptions.WorktreeError` (or a
    subclass): the lane breaker counts it on its separate, higher-threshold
    worktree counter instead of ``consecutive_spawn_errors``. A bool while
    there are two failure classes; it becomes an enum if another failure
    class ever needs separate breaker accounting. Both are declared last so
    the existing positional-free call sites are unaffected.
    """

    spawned: bool = False
    usage_limit_detected: bool = False
    spawn_error: bool = False
    error: str = ""
    capability_parked: bool = False
    occupied: bool = False
    usage_limit_reset_at: datetime | None = None
    worktree_failure: bool = False


# Consecutive counted pre-spawn parks (across any client/task sharing this
# process) tolerated before the park also raises the generic ``spawn_error``
# signal, as a bounded backstop (#1238). Below this count a park stays
# decoupled from the per-lane circuit breaker: an isolated park must not
# durably pause an unrelated lane. At/above it the condition has stopped
# looking like an isolated blip and started looking systemic (e.g. a wrong
# TTL-cached probe verdict shared by every codex-backed task in the process),
# so the existing circuit-breaker/operator-visible-pause machinery may engage
# rather than letting every task drain into BLOCKED_ON_USER unchecked.
_PARK_STREAK_CIRCUIT_THRESHOLD = 3

# Consecutive-park counter backing _PARK_STREAK_CIRCUIT_THRESHOLD (#1238). A
# single-element list as a mutable slot, so no ``global`` statement is needed.
# Incremented on every counted park; reset by :func:`reset_park_streak` as soon
# as the environmental probe reports capable again, so this tracks a
# *consecutive* streak, not a lifetime total.
_park_streak: list[int] = [0]


def reset_park_streak() -> None:
    """Clear the consecutive-park streak (a capable probe, or test support)."""
    _park_streak[0] = 0


def park_before_spawn(
    task: TicketTask,
    client: ClientConfig,
    *,
    disposition: str,
    breadcrumbs: str,
    count_streak: bool,
) -> _SpawnOutcome:
    """Park claimed RUNNING *task* BLOCKED_ON_USER before any spawn (#3744).

    ``unproductive=False`` (no session ran) and ``created_at`` pinned, exactly
    as the codex capability gate parked (#1238). *count_streak* is True for an
    environmental park (the codex CLI probe): it advances the streak, and at
    :data:`_PARK_STREAK_CIRCUIT_THRESHOLD` the outcome also sets
    ``spawn_error``. A static-capability park passes False and never counts.
    """
    _park_running_task_blocked_on_user(
        ticket_id=task.ticket_id,
        client_name=client.name,
        disposition=disposition,
        breadcrumbs=breadcrumbs,
        unproductive=False,
        created_at=task.created_at,
    )
    breaker_engaged = False
    if count_streak:
        _park_streak[0] += 1
        breaker_engaged = _park_streak[0] >= _PARK_STREAK_CIRCUIT_THRESHOLD
    return _SpawnOutcome(
        capability_parked=True,
        spawn_error=breaker_engaged,
        error=disposition,
    )
