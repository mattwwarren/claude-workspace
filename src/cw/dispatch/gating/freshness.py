"""Main-branch freshness preflight gate for the dispatch loop.

Part of the ``cw.dispatch.gating`` package split (#2503): the per-client
check that the local default branch is current with origin, the auto
fast-forward path with its non-main-head / diverged / dirty / detached
refusals, and the ``ticket.needs_sync`` + ``dispatch.tick`` skip events for a
freshness-gated client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cw.dispatch.gating.context_json import _LOGGER_NAME
from cw.events import record_event
from cw.exceptions import (
    MissingWorkspaceError,
    WorktreeError,
)
from cw.models import (
    DispatchSkipReason,
    OrchestratorEventType,
    QueueItemStatus,
)
from cw.worktree import (
    check_main_ff_safety,
    fast_forward_main,
    get_head_branch,
    is_main_behind_origin,
    is_main_checkout_dirty,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cw.models import (
        ClientConfig,
        DevQueueStore,
    )
    from cw.worktree import FetchWarningKey
from cw.dispatch.claim import _lane_occupants_for_client, _lane_stats_for_client

_log = logging.getLogger(_LOGGER_NAME)


FRESHNESS_NON_MAIN_HEAD = "non_main_head"


FRESHNESS_MAIN_BEHIND = "main_behind_origin"


FRESHNESS_MAIN_DIRTY_CHECKOUT = "main_dirty_checkout"


FRESHNESS_MAIN_DIVERGED = "main_diverged_from_origin"


FRESHNESS_MAIN_DETACHED = "main_detached_head"


def _resolve_freshness(
    client: ClientConfig,
    *,
    auto_ff: bool,
    warned_fetch_fail: set[FetchWarningKey] | None,
) -> tuple[bool, str | None]:
    """Run the freshness gate for a client, returning (stale, freshness_detail).

    Checks whether the client's local default branch is behind origin.  When
    ``auto_ff`` is set and the branch is safely behind, attempts a
    fast-forward and clears the stale flag on success.  On any freshness-check
    error, logs and treats the client as fresh so a transient network issue
    never blocks the whole loop.

    Returns ``(False, None)`` when fresh (or successfully fast-forwarded).
    Returns ``(True, "non_main_head")`` when the dispatch repo's HEAD is on a
    non-default branch — ``fast_forward_main`` is skipped entirely to avoid a
    spurious WorktreeError.  Returns ``(True, "main_behind_origin")`` for all
    other stale conditions.
    """
    try:
        stale, local_sha, origin_sha, behind_count = is_main_behind_origin(
            client, warned_fetch_fail=warned_fetch_fail
        )
    except Exception:  # noqa: BLE001
        # Defense-in-depth: _fetch_default_branch now handles
        # FileNotFoundError/PermissionError internally; this catches
        # other unexpected OS errors (e.g., git not on PATH, network
        # issues raising RuntimeError from the adapter).
        _log.warning(
            "dispatch_tick: freshness check failed for %s; proceeding",
            client.name,
        )
        return (False, None)

    if stale:
        # Guard: detect non-default HEAD before attempting auto-ff.
        # When HEAD != default_branch, fast_forward_main would raise WorktreeError
        # and log a confusing message. Bail early with a distinct detail key so
        # the operator WARN can surface the specific remedy.
        head_branch = get_head_branch(client)
        if head_branch is not None and head_branch != client.default_branch:
            return (True, FRESHNESS_NON_MAIN_HEAD)

    if stale and auto_ff:
        ff_safety = check_main_ff_safety(client)
        if ff_safety == "detached":
            return (True, FRESHNESS_MAIN_DETACHED)
        # "ahead" is theoretically unreachable here: stale=True requires
        # is_main_behind_origin to return behind_count>0, which means local
        # is behind origin — not ahead. The guard is kept for defensive
        # completeness (worktree.py:check_main_ff_safety documents this).
        if ff_safety in ("ahead", "diverged"):
            return (True, FRESHNESS_MAIN_DIVERGED)
        if ff_safety == "behind" and is_main_checkout_dirty(client):
            return (True, FRESHNESS_MAIN_DIRTY_CHECKOUT)
        if ff_safety == "behind":
            try:
                fast_forward_main(client, ignore_untracked=True)
                # Why: double-fetch accepted — is_main_behind_origin fetches
                # and git pull --ff-only fetches again. Acceptable for a
                # single-user tool.
                _log.info(
                    "auto-ff: %s/main: %s..%s (%d commits)",
                    client.name,
                    local_sha[:8],
                    origin_sha[:8],
                    behind_count,
                )
                stale = False
            except (WorktreeError, MissingWorkspaceError) as exc:
                _log.warning(
                    "auto-ff: fast-forward failed for %s: %s",
                    client.name,
                    exc,
                )
            # Why: no git-level lock — concurrent dispatch loops are safe;
            # git pull --ff-only is idempotent when already current.
    return (stale, FRESHNESS_MAIN_BEHIND if stale else None)


def _emit_stale_skip(
    client: ClientConfig,
    queue_snapshot: DevQueueStore,
    *,
    pending_count: int,
    running_count: int,
    cap: int,
    emit: Callable[[str], None] | None,
    warned_stale: set[tuple[str, str]] | None,
    freshness_detail: str | None = None,
) -> None:
    """Emit TICKET_NEEDS_SYNC + dispatch.tick for a freshness-gated client.

    Records one TICKET_NEEDS_SYNC per pending task (de-duplicating the
    operator WARN via ``warned_stale``), then a single dispatch.tick with
    ``skip_reason=FRESHNESS_GATE`` and ``freshness_detail`` set to the
    provided value (``"non_main_head"``, ``"main_behind_origin"``,
    ``"main_dirty_checkout"``, ``"main_diverged_from_origin"``, or
    ``"main_detached_head"``).
    """
    stale_tasks = [
        {"ticket_id": t.ticket_id, "client": client.name, "lane": t.lane}
        for t in queue_snapshot.tasks
        if t.client == client.name and t.status == QueueItemStatus.PENDING
    ]
    # Fetch branch name once for the non-main-head WARN (not per ticket).
    non_main_branch: str | None = None
    if freshness_detail == FRESHNESS_NON_MAIN_HEAD:
        non_main_branch = get_head_branch(client)
    for payload in stale_tasks:
        record_event(OrchestratorEventType.TICKET_NEEDS_SYNC, payload)
        if emit is not None:
            ticket_key = (client.name, payload["ticket_id"])
            if warned_stale is None or ticket_key not in warned_stale:
                if freshness_detail == FRESHNESS_NON_MAIN_HEAD:
                    branch_str = non_main_branch or "(detached)"
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        f" repo HEAD is on '{branch_str}',"
                        f" expected '{client.default_branch}'"
                        f" — run: git -C {client.workspace_path}"
                        f" checkout {client.default_branch}"
                    )
                elif freshness_detail == FRESHNESS_MAIN_DIRTY_CHECKOUT:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main checkout has uncommitted changes, ticket skipped"
                        f" — commit or stash changes in {client.workspace_path}"
                    )
                elif freshness_detail == FRESHNESS_MAIN_DETACHED:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main checkout has a detached HEAD, ticket skipped"
                        f" — run: git -C {client.workspace_path}"
                        f" checkout {client.default_branch}"
                    )
                elif freshness_detail == FRESHNESS_MAIN_DIVERGED:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main has diverged from origin, ticket skipped"
                        " — inspect before touching it: git -C"
                        f" {client.workspace_path} log origin/"
                        f"{client.default_branch}..HEAD --oneline —"
                        " do NOT auto-rebase or reset; stray commits"
                        " may need manual triage"
                    )
                else:
                    emit(
                        f"WARN {client.name}/{payload['ticket_id']}:"
                        " main behind origin, ticket skipped"
                    )
                if warned_stale is not None:
                    warned_stale.add(ticket_key)
    lane_occupants = _lane_occupants_for_client(client, queue_snapshot)
    record_event(
        OrchestratorEventType.DISPATCH_TICK,
        {
            "client": client.name,
            "claimed": 0,
            "pending": pending_count,
            "running": running_count,
            "cap": cap,
            "skip_reason": DispatchSkipReason.FRESHNESS_GATE,
            "freshness_detail": freshness_detail,
            "blocked_branch": non_main_branch,
            "lanes": _lane_stats_for_client(
                client, queue_snapshot, occupants=lane_occupants
            ),
            "lane_occupants": lane_occupants,
            "occupied": sum(len(v) for v in lane_occupants.values()),
        },
    )
