"""The one seam that names a ticket's git branch (GitHub #2948, RFC 0016 D-2).

Every site that needs a ticket's branch (worktree path, open-PR probe, merged-PR
fallback, occupancy screen, repair push, ...) asks :func:`resolve_feature_branch`
instead of deriving ``<feature_branch_prefix>/<ticket_id>`` itself, so a row
that records an explicit :attr:`cw.models.TicketTask.branch` (an adopted PR on
any other branch, #2943) is honored everywhere at once.

A leaf of the ``cw.models`` DAG: it imports ``ClientConfig`` and ``TicketTask``
only for type checking, so it adds no runtime edge to ``client`` or ``tasks``.
``tests/test_branch_seam_guard.py`` keeps every other ``src/`` module from
reading ``feature_branch_prefix`` directly. The earlier reconcile-level helper
was deleted rather than wrapped, with every call site migrated (ARCHITECTURE.md
§7 principle 11): there is one resolver and no alias.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cw.models.client import ClientConfig
    from cw.models.tasks import TicketTask

# The prefix used when the client is unknown, matching ClientConfig's default.
_DEFAULT_FEATURE_BRANCH_PREFIX = "dev"


def resolve_feature_branch(
    ticket_id: str,
    client: ClientConfig | None,
    task: TicketTask | None = None,
) -> str:
    """Return the git branch for *ticket_id*.

    *task*'s recorded ``branch`` when a row is given and has one; otherwise
    ``<prefix>/<ticket_id>`` with *client*'s ``feature_branch_prefix``, or
    ``dev`` when *client* is ``None``. Pass the row whenever it is in scope: a
    caller that omits it silently ignores a recorded branch.
    """
    if task is not None and task.branch:
        return task.branch
    prefix = (
        client.feature_branch_prefix
        if client is not None
        else _DEFAULT_FEATURE_BRANCH_PREFIX
    )
    return f"{prefix}/{ticket_id}"
