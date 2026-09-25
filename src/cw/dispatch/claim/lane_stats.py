"""Per-lane occupancy snapshots for dispatch.tick payloads.

Task-join occupant lists and ``{claimed, running, blocked, signoff, pending}``
counts per lane, read by the dispatch loop and the lane gate. Extracted
verbatim from the historical flat ``cw.dispatch.claim`` module by the package
split (#2378).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.models import QueueItemStatus, occupies_lane_slot

if TYPE_CHECKING:
    from cw.models import ClientConfig, DevQueueStore


def _lane_occupants_for_client(
    client: ClientConfig, queue_snapshot: DevQueueStore
) -> dict[str, list[dict[str, str]]]:
    """Per-lane occupant ``{ticket_id, status}`` list for dispatch.tick payloads.

    Sibling of :func:`_lane_stats_for_client` -- same :func:`occupies_lane_slot`
    join over ``client``/``lane``, but returns identifying detail instead
    of counts, so a ``lane_cap_blocked`` reader can name the occupant
    instead of inferring a (possibly phantom) cross-client cap. See #1243.

    Deliberately a NEW top-level dispatch.tick payload key, never nested
    inside ``lanes`` -- orchestrate.py's ``_extract_lanes`` hard-filters
    ``lanes`` values to numerics, so a nested ticket-id string would be
    silently stripped downstream.
    """
    occupants: dict[str, list[dict[str, str]]] = {}
    for lane_cfg in client.effective_lanes:
        occupants[lane_cfg.name] = [
            {"ticket_id": t.ticket_id, "status": t.status.value}
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and occupies_lane_slot(t)
        ]
    return occupants


def _lane_stats_for_client(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    occupants: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, dict[str, int]]:
    """Per-lane ``{claimed, running, blocked, signoff, pending}`` counts for
    event payloads.

    Why task-based running: RUNNING/BLOCKED_ON_USER tasks carry ``lane``;
    sessions carry ``lane`` as of #594, but occupancy counting stays task-join
    based per ADR-0006 / Phase 4a scope (stamped-but-not-read by the
    scheduler). BLOCKED_ON_USER occupies its lane slot per ADR-0006, so
    ``running + blocked + signoff`` is the total occupied count. ``blocked``
    and ``signoff`` are split out so operators can see at a glance why
    claimed=0 when pending>0 (#588, #990). Derives running/blocked/signoff
    from :func:`_lane_occupants_for_client` -- see #1243.

    *occupants* lets a caller that already computed the occupant lookup (e.g.
    to also emit ``lane_occupants``/``occupied`` on the same dispatch.tick
    payload) pass it in and avoid a second full scan of ``queue_snapshot.tasks``.
    """
    if occupants is None:
        occupants = _lane_occupants_for_client(client, queue_snapshot)
    stats: dict[str, dict[str, int]] = {}
    for lane_cfg in client.effective_lanes:
        lane_occupants = occupants.get(lane_cfg.name, [])
        running = sum(
            1 for o in lane_occupants if o["status"] == QueueItemStatus.RUNNING.value
        )
        blocked = sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.BLOCKED_ON_USER.value
        )
        signoff = sum(
            1
            for o in lane_occupants
            if o["status"] == QueueItemStatus.AWAITING_OPERATOR_SIGNOFF.value
        )
        pending = sum(
            1
            for t in queue_snapshot.tasks
            if t.client == client.name
            and t.lane == lane_cfg.name
            and t.status == QueueItemStatus.PENDING
        )
        stats[lane_cfg.name] = {
            "claimed": 0,
            "running": running,
            "blocked": blocked,
            "signoff": signoff,
            "pending": pending,
        }
    return stats
