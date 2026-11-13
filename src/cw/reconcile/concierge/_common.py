"""Names the concierge recipes share: recipe keys, the candidate type, lookups.

Recipe 1 (``false_park``) and recipe 2 (``park_marker_poison``) both resolve
the owning session by name, apply the same fail-toward-dead liveness check,
split their populations on the same park markers, and refuse at the same
attempt ceiling. Recipe 3 (``cancelled_row``) and ``core`` use the recipe
keys and :class:`ConciergeCandidate`. A leaf: it imports no sibling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cw.config import get_client
from cw.exceptions import CwError
from cw.reconcile._shared import (
    _NEEDS_SALVAGE_REASON,
    _SILENTLY_IDLE_REASON,
    resolve_attempt_ceiling,
    ticket_id_for_session,
)
from cw.reconcile._shared._roster import session_may_postdate_roster

if TYPE_CHECKING:
    from datetime import datetime

    from cw.models import (
        ClientConfig,
        CwState,
        OrchestratorConfig,
        Session,
        TicketTask,
    )

# Recipe name constants — the recognised keys of
# OrchestratorConfig.concierge_recoveries (Q7).
RECIPE_FALSE_PARK_REQUEUE = "false_park_requeue"
RECIPE_PARK_MARKER_POISON_CLEAR = "park_marker_poison_clear"
RECIPE_CANCELLED_ROW_RESTORE = "cancelled_row_restore"

# All 3 recipes default to enabled; concierge_recoveries is a sparse override
# dict merged onto this default (NOT a full-replace), so setting one key in
# config never silently disables the other two (Q7).
DEFAULT_CONCIERGE_RECOVERIES: dict[str, bool] = {
    RECIPE_FALSE_PARK_REQUEUE: True,
    RECIPE_PARK_MARKER_POISON_CLEAR: True,
    RECIPE_CANCELLED_ROW_RESTORE: True,
}


def resolve_concierge_recipe_enabled(
    config: OrchestratorConfig, recipe_name: str
) -> bool:
    """Return whether *recipe_name* is enabled, merging config onto the defaults.

    An operator-supplied ``concierge_recoveries`` dict only ever *overrides*
    individual keys; a recipe absent from it keeps its
    ``DEFAULT_CONCIERGE_RECOVERIES`` value (Q7).
    """
    return config.concierge_recoveries.get(
        recipe_name, DEFAULT_CONCIERGE_RECOVERIES[recipe_name]
    )


@dataclass(frozen=True)
class ConciergeCandidate:
    """Classification result from a concierge recipe's detect phase.

    ``refused_ceiling`` is True when the row's ``attempts`` is already at or
    past ``global_attempt_ceiling`` — the act phase leaves the row parked
    rather than requeuing it (A1/A2): a ceiling-refused
    ``stalled_retry_cap_parked`` row is itself part of the escalation-eligible
    set (see ``cw.reconcile.escalation``), so refusing here is not silent.

    ``refused_hook_context_conflict`` is the same shape for a different,
    likewise-futile requeue (GitHub #1674) — see the field comment below. A
    refused row keeps its disposition, so it too stays escalation-eligible.
    """

    ticket_id: str
    client: str
    recipe: str
    evidence: dict[str, object] = field(default_factory=dict)
    session_id: str | None = None
    refused_ceiling: bool = False
    # Recipe 1 only (GitHub #1030): True when this cycle's session shows the
    # dead-on-arrival signature — the previous mechanical recovery produced a
    # session that died within seconds of spawn. Guarded to False when
    # evidence is missing (no session, or transcript age unlocatable) so
    # missing evidence never arms the backoff. Consumed by the act phase to
    # arm/reset the false_park_recovery_count / next_eligible_at backoff.
    dead_on_arrival: bool = False
    # Recipe 1 only (GitHub #1674): True when this row's currently-resolved
    # session IS the session that already made a spawn attempt raise
    # HookContextConflictError (task.hook_context_conflict_session_id), and
    # that session is still non-terminal. Its cw-context.json still owns the
    # worktree, so a requeue can only fail the same way again and burn another
    # attempt. Unlike dead_on_arrival's backoff (a deferral of the next cycle),
    # the act phase skips the requeue entirely — elapsed time cannot clear this
    # condition, only an operator closing the session or a newer session
    # superseding it.
    refused_hook_context_conflict: bool = False


def _find_session_for_ticket(
    state: CwState, client: str, ticket_id: str
) -> Session | None:
    """Reverse-lookup the newest session for (client, ticket_id).

    Both stalled-park paths (retry-cap park, gh-blocked park) clear
    ``task.session_id`` on parking, so recipe 1/2 cannot dereference
    ``task.session_id`` directly and must instead scan ``state.sessions`` by
    name (mirroring ``ticket_id_for_session``'s forward direction in
    reverse). Picks the most-recently-started match so a stale historical
    session for the same ticket id never shadows the live one.
    """
    matches = [
        s
        for s in state.sessions
        if s.client == client and ticket_id_for_session(s.name) == ticket_id
    ]
    if not matches:
        return None
    return max(matches, key=lambda s: s.started_at)


def _is_session_dead(
    session: Session | None,
    native_live: set[str],
    *,
    roster_captured_at: datetime | None = None,
) -> bool:
    """Mirror doctor._is_dead_session_task's fail-toward-dead stance.

    True when there's no session at all, or its ``surface_ref`` is unset or
    absent from the live daemon roster. Missing evidence counts as dead — the
    concierge recipes only act when they can't prove the session is still
    alive.

    *roster_captured_at* is the capture time of the lockless roster
    *native_live* came from (#2550). When given, a session born (spawned,
    resumed or bound) within ``ROSTER_REGISTRATION_SLACK_SECONDS`` of it (or
    after it) is not dead: its ref may postdate the capture, so absence proves
    nothing.
    ``None`` keeps the roster-only test.
    """
    if session is None:
        return True
    if session_may_postdate_roster(session, roster_captured_at):
        return False
    if session.surface_ref is None:
        return True
    return session.surface_ref not in native_live


# Session-level park markers (recipe 2's own domain — see _has_park_marker
# below). A null-disposition BLOCKED_ON_USER row behind one of these markers
# is deliberately excluded from recipe 1: it needs recipe 2's stricter
# evidence gate (consecutive_salvage_skips >= 1 AND per-stage-floor 45m
# staleness), not recipe 1's plain 5-minute flatness check. Without this
# exclusion the two recipes would race for the same row and recipe 1's
# looser gate would always win first.
_PARK_MARKER_REASONS: frozenset[str] = frozenset(
    {_SILENTLY_IDLE_REASON, _NEEDS_SALVAGE_REASON}
)


def _has_park_marker(session: Session) -> bool:
    """True when session.last_result carries a park-marker paused_status."""
    return (
        isinstance(session.last_result, dict)
        and session.last_result.get("paused_status") in _PARK_MARKER_REASONS
    )


def _refused_at_attempt_ceiling(task: TicketTask, config: OrchestratorConfig) -> bool:
    """True when *task* sits at or above its effective attempt ceiling.

    GitHub #1750: mirrors the dispatch claim path's ceiling — both must read
    the same counter or the concierge would refuse a requeue the claim path
    would have allowed. GitHub #1751 extends that agreement to the *number*:
    the ceiling is now lane-scoped, so both layers route through
    :func:`~cw.reconcile._shared.resolve_attempt_ceiling` rather than reading
    ``global_attempt_ceiling`` flat. A lane that disables the ceiling resolves
    to ``None`` and is never refused here.

    Single copy shared by recipes 1 and 2 precisely because the invariant is
    "these two agree" — two in-place copies of an agreement rule are two
    places for it to drift.

    An unknown/removed client raises :class:`~cw.exceptions.CwError` from
    ``get_client``; that falls through to the global ceiling, preserving the
    pre-#1751 behaviour for every row whose client isn't in clients.yaml. Same
    ``try``/``except CwError`` idiom as recipe 3's ``cancelled_row_targets``.
    """
    try:
        client_cfg: ClientConfig | None = get_client(task.client)
    except CwError:
        client_cfg = None
    ceiling = resolve_attempt_ceiling(client_cfg, task, config)
    return ceiling is not None and task.unproductive_attempts >= ceiling
