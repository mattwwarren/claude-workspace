"""The scope-drift decision view for ``cw dev-queue show`` (#3772).

A row parked ``plan_scope_drift`` at IMPL is waiting on one decision: approve
the new paths. This module names who makes it (the operator session by default,
a human only on an explicit opt-in gate, ADR-0020), which paths a verified
grant already carries, which paths are new, and the command that records the
decision. It runs no git and never raises on a malformed park.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Literal

from cw.dev_queue.next_commands import (
    SCOPE_DRIFT_PATHS_PLACEHOLDER,
    dev_queue_command,
    row_carries_human_opt_in,
)
from cw.dev_queue.scope_drift_guard import is_scope_drift_park, scope_park_snapshot
from cw.models.ticket_view import ScopeDecisionView
from cw.scope_grants import parse_drift_extra_files

if TYPE_CHECKING:
    from cw.models import ClientConfig, Session
    from cw.models.tasks.ticket_task import TicketTask

_HUMAN_PREFIX = "escalate to the human: "


def _carried_paths(task: TicketTask) -> list[str]:
    """Sorted union of the paths of this row's own grants (display only)."""
    own = [
        grant
        for grant in task.scope_grants
        if grant.client == task.client
        and grant.ticket_id == task.ticket_id
        and grant.row_created_at == task.created_at
    ]
    return sorted({path for grant in own for path in grant.paths})


def build_scope_decision(
    task: TicketTask,
    *,
    human_owner: bool,
    details: str | None,
    client_cfg: ClientConfig | None = None,
    session: Session | None = None,
) -> ScopeDecisionView | None:
    """The decision view for a scope-drift park, or ``None`` outside one.

    ``human_owner`` is the opt-in ``show`` already resolved for this row; it is
    not resolved again here. The owner is ``"human"`` when that opt-in or the
    row's own opt-in holds, otherwise ``"operator"``.
    """
    if not is_scope_drift_park(task):
        return None
    owner: Literal["operator", "human"] = (
        "human" if human_owner or row_carries_human_opt_in(task) else "operator"
    )
    new_paths = parse_drift_extra_files(details or "")
    paths_arg = shlex.quote(",".join(new_paths)) if new_paths else None
    command = dev_queue_command(
        "approve", task, "--scope-drift", paths_arg or SCOPE_DRIFT_PATHS_PLACEHOLDER
    )
    next_action = f"{_HUMAN_PREFIX}{command}" if owner == "human" else command
    snapshot = scope_park_snapshot(
        task, client_cfg, human_owner=owner == "human", session=session
    )
    return ScopeDecisionView(
        owner=owner,
        carried_paths=_carried_paths(task),
        new_paths=new_paths,
        next_action=next_action,
        park_token=None if snapshot is None else snapshot.token,
    )
